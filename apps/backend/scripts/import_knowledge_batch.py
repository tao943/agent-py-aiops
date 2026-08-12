"""Import reviewed Markdown troubleshooting cards through the existing API."""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast

import httpx

if __package__:
    from .knowledge_index_client import IndexPollingError, parse_created_task, wait_for_index_task
else:
    from knowledge_index_client import IndexPollingError, parse_created_task, wait_for_index_task

from super_ai.project_config import project_config_section, required_int, required_str

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_client_transport: httpx.BaseTransport | None = None


@dataclass(frozen=True)
class RuntimeSettings:
    source_dir: Path
    base_url: str
    email: str
    password: str
    display_name: str
    poll_interval_seconds: int
    index_wait_seconds: int


@dataclass(frozen=True)
class ImportResult:
    filename: str
    document_id: str | None
    task_id: str | None
    status: Literal["succeeded", "failed"]
    error: str | None = None


@dataclass(frozen=True)
class BatchSummary:
    results: tuple[ImportResult, ...]

    @property
    def succeeded(self) -> int:
        return sum(result.status == "succeeded" for result in self.results)

    @property
    def failed(self) -> int:
        return sum(result.status == "failed" for result in self.results)

    def payload(self) -> dict[str, object]:
        return {
            "succeeded": self.succeeded,
            "failed": self.failed,
            "results": [asdict(result) for result in self.results],
        }


def discover_markdown_files(source_dir: Path) -> list[Path]:
    """Return safe, deterministic, non-recursive Markdown candidates."""

    root = source_dir.resolve()
    if not root.is_dir():
        raise ValueError(f"Source must be an existing directory: {source_dir}")

    candidates: list[Path] = []
    for path in source_dir.iterdir():
        if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".md":
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            continue
        candidates.append(resolved)
    return sorted(candidates, key=lambda item: item.name.casefold())


def import_batch(
    client: httpx.Client,
    *,
    files: Sequence[Path],
    user_id: str,
    token: str,
    poll_interval_seconds: float,
    index_wait_seconds: int,
    continue_on_error: bool = False,
) -> BatchSummary:
    knowledge_base_id = f"kb_{user_id}"
    headers = {"Authorization": f"Bearer {token}"}
    results: list[ImportResult] = []
    for path in sorted(files, key=lambda item: item.name.casefold()):
        document_id: str | None = None
        task_id: str | None = None
        try:
            upload = client.post(
                f"/knowledge-bases/{knowledge_base_id}/documents",
                headers=headers,
                data={"overwrite": "true"},
                files={"file": (path.name, path.read_bytes(), "text/markdown")},
            )
            upload.raise_for_status()
            upload_data = _object(_object(upload.json(), "response").get("data"), "data")
            document = _object(upload_data.get("document"), "data.document")
            document_id = _required_string(document.get("id"), "data.document.id")

            task_response = client.post(
                f"/knowledge-bases/{knowledge_base_id}/documents/{document_id}/index-tasks",
                headers=headers,
            )
            task_response.raise_for_status()
            task = parse_created_task(task_response.json())
            task_id = _required_string(task.get("id"), "data.task.id")
            endpoint = (
                f"/knowledge-bases/{knowledge_base_id}/documents/{document_id}"
                f"/index-tasks/{task_id}"
            )
            wait_for_index_task(
                client,
                endpoint=endpoint,
                headers=headers,
                poll_interval_seconds=poll_interval_seconds,
                deadline=time.monotonic() + index_wait_seconds,
            )
        except (OSError, httpx.HTTPError, IndexPollingError, ValueError) as exc:
            results.append(
                ImportResult(
                    filename=path.name,
                    document_id=document_id,
                    task_id=task_id,
                    status="failed",
                    error=_safe_error(exc),
                )
            )
            if not continue_on_error:
                break
        else:
            results.append(
                ImportResult(
                    filename=path.name,
                    document_id=document_id,
                    task_id=task_id,
                    status="succeeded",
                )
            )
    return BatchSummary(tuple(results))


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        source_dir = _resolve_source_dir(args.source_dir)
        files = discover_markdown_files(source_dir)
    except (KeyError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2

    if not files:
        print(
            json.dumps(
                {"dryRun": bool(args.dry_run), "error": "No Markdown files found.", "total": 0},
                ensure_ascii=False,
            )
        )
        return 2
    if args.dry_run:
        print(
            json.dumps(
                {"dryRun": True, "files": [path.name for path in files], "total": len(files)},
                ensure_ascii=False,
            )
        )
        return 0

    try:
        settings = _load_runtime_settings(source_dir)
        with httpx.Client(
            base_url=settings.base_url,
            timeout=15,
            transport=_client_transport,
        ) as client:
            auth = _register_or_login(
                client,
                email=settings.email,
                password=settings.password,
                display_name=settings.display_name,
            )
            user = _object(auth.get("user"), "data.user")
            user_id = _required_string(user.get("id"), "data.user.id")
            token = _required_string(auth.get("accessToken"), "data.accessToken")
            summary = import_batch(
                client,
                files=files,
                user_id=user_id,
                token=token,
                poll_interval_seconds=settings.poll_interval_seconds,
                index_wait_seconds=settings.index_wait_seconds,
                continue_on_error=bool(args.continue_on_error),
            )
    except (httpx.HTTPError, ValueError) as exc:
        print(json.dumps({"error": _safe_error(exc)}, ensure_ascii=False))
        return 2

    print(json.dumps(summary.payload(), ensure_ascii=False))
    return 0 if summary.failed == 0 else 1


def main() -> None:
    raise SystemExit(run())


def _object(value: object, location: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"Expected an object at {location}.")
    return cast(dict[str, object], value)


def _required_string(value: object, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Expected a non-empty string at {location}.")
    return value


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.HTTPError):
        return type(exc).__name__
    return str(exc)


def _resolve_source_dir(source_override: Path | None) -> Path:
    if source_override is not None:
        return source_override.resolve()
    batch = project_config_section("knowledgeBatch")
    configured = Path(required_str(batch, "sourceDir"))
    return configured if configured.is_absolute() else REPOSITORY_ROOT / configured


def _load_runtime_settings(source_dir: Path) -> RuntimeSettings:
    demo = project_config_section("aiopsDemo")
    return RuntimeSettings(
        source_dir=source_dir,
        base_url=required_str(demo, "backendBaseUrl").rstrip("/"),
        email=required_str(demo, "email"),
        password=required_str(demo, "password"),
        display_name=required_str(demo, "displayName"),
        poll_interval_seconds=required_int(demo, "pollIntervalSeconds"),
        index_wait_seconds=required_int(demo, "indexWaitSeconds"),
    )


def _register_or_login(
    client: httpx.Client,
    *,
    email: str,
    password: str,
    display_name: str,
) -> dict[str, object]:
    response = client.post(
        "/auth/register",
        json={"email": email, "password": password, "displayName": display_name},
    )
    if response.status_code == 409:
        response = client.post("/auth/login", json={"email": email, "password": password})
    response.raise_for_status()
    envelope = _object(response.json(), "response")
    return _object(envelope.get("data"), "data")


if __name__ == "__main__":
    main()
