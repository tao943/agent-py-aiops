from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from scripts import import_knowledge_batch
from scripts.import_knowledge_batch import discover_markdown_files, import_batch, run

from super_ai.project_config import ProjectConfigurationError


def test_discover_markdown_files_is_sorted_non_recursive_and_markdown_only(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "knowledge"
    source_dir.mkdir()
    (source_dir / "b.md").write_text("b", encoding="utf-8")
    (source_dir / "a.md").write_text("a", encoding="utf-8")
    (source_dir / "notes.txt").write_text("ignored", encoding="utf-8")
    nested = source_dir / "nested"
    nested.mkdir()
    (nested / "nested.md").write_text("ignored", encoding="utf-8")

    files = discover_markdown_files(source_dir)

    assert [path.name for path in files] == ["a.md", "b.md"]


def test_discover_markdown_files_ignores_symlinks(tmp_path: Path) -> None:
    source_dir = tmp_path / "knowledge"
    source_dir.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    link = source_dir / "linked.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks are not available for this Windows test user.")

    assert discover_markdown_files(source_dir) == []


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_discover_markdown_files_rejects_invalid_source(tmp_path: Path, kind: str) -> None:
    source = tmp_path / "source"
    if kind == "file":
        source.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="directory"):
        discover_markdown_files(source)


def test_dry_run_prints_safe_summary_without_creating_an_http_client(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_dir = tmp_path / "knowledge"
    source_dir.mkdir()
    (source_dir / "runbook.md").write_text("secret body", encoding="utf-8")

    exit_code = run(["--source-dir", str(source_dir), "--dry-run"])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output == {"dryRun": True, "files": ["runbook.md"], "total": 1}
    assert "secret body" not in json.dumps(output)


def test_dry_run_rejects_empty_batch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source_dir = tmp_path / "knowledge"
    source_dir.mkdir()

    exit_code = run(["--source-dir", str(source_dir), "--dry-run"])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert output == {"dryRun": True, "error": "No Markdown files found.", "total": 0}


def test_run_reports_configuration_errors_without_traceback(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_to_resolve(_source_dir: Path | None) -> Path:
        raise ProjectConfigurationError(
            "Project config section must be an object: knowledgeBatch"
        )

    monkeypatch.setattr(import_knowledge_batch, "_resolve_source_dir", fail_to_resolve)

    exit_code = run([])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert output == {"error": "Project config section must be an object: knowledgeBatch"}


def test_import_batch_uploads_and_indexes_files_sequentially(tmp_path: Path) -> None:
    files = _markdown_files(tmp_path, ["b.md", "a.md"])
    requests: list[tuple[str, str]] = []
    responses = iter(
        [
            _upload_response("doc-a"),
            _create_task_response("task-a"),
            _query_task_response("task-a", "succeeded"),
            _upload_response("doc-b"),
            _create_task_response("task-b"),
            _query_task_response("task-b", "succeeded"),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        response = next(responses)
        response.request = request
        return response

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        summary = import_batch(
            client,
            files=files,
            user_id="user-a",
            token="redacted",
            poll_interval_seconds=0,
            index_wait_seconds=10,
        )

    assert summary.succeeded == 2
    assert summary.failed == 0
    actual_results = [
        (result.filename, result.document_id, result.task_id, result.status)
        for result in summary.results
    ]
    assert actual_results == [
        ("a.md", "doc-a", "task-a", "succeeded"),
        ("b.md", "doc-b", "task-b", "succeeded"),
    ]
    assert requests == [
        ("POST", "/knowledge-bases/kb_user-a/documents"),
        ("POST", "/knowledge-bases/kb_user-a/documents/doc-a/index-tasks"),
        ("GET", "/knowledge-bases/kb_user-a/documents/doc-a/index-tasks/task-a"),
        ("POST", "/knowledge-bases/kb_user-a/documents"),
        ("POST", "/knowledge-bases/kb_user-a/documents/doc-b/index-tasks"),
        ("GET", "/knowledge-bases/kb_user-a/documents/doc-b/index-tasks/task-b"),
    ]


def test_import_batch_stops_after_first_failure_by_default(tmp_path: Path) -> None:
    files = _markdown_files(tmp_path, ["a.md", "b.md"])
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(503, json={"error": {"code": "UNAVAILABLE"}}, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        summary = import_batch(
            client,
            files=files,
            user_id="user-a",
            token="redacted",
            poll_interval_seconds=0,
            index_wait_seconds=10,
        )

    assert summary.succeeded == 0
    assert summary.failed == 1
    assert [result.filename for result in summary.results] == ["a.md"]
    assert requests == ["/knowledge-bases/kb_user-a/documents"]


def test_import_batch_continues_after_failure_when_requested(tmp_path: Path) -> None:
    files = _markdown_files(tmp_path, ["a.md", "b.md"])
    responses = iter(
        [
            httpx.Response(503, json={"error": {"code": "UNAVAILABLE"}}),
            _upload_response("doc-b"),
            _create_task_response("task-b"),
            _query_task_response("task-b", "succeeded"),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        response = next(responses)
        response.request = request
        return response

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        summary = import_batch(
            client,
            files=files,
            user_id="user-a",
            token="redacted",
            poll_interval_seconds=0,
            index_wait_seconds=10,
            continue_on_error=True,
        )

    assert summary.succeeded == 1
    assert summary.failed == 1
    assert [(result.filename, result.status) for result in summary.results] == [
        ("a.md", "failed"),
        ("b.md", "succeeded"),
    ]


def test_live_run_authenticates_once_and_prints_safe_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_dir = tmp_path / "knowledge"
    source_dir.mkdir()
    (source_dir / "runbook.md").write_text("private body", encoding="utf-8")
    requests: list[str] = []
    responses = iter(
        [
            httpx.Response(409, json={"error": {"code": "CONFLICT"}}),
            httpx.Response(
                200,
                json={
                    "data": {
                        "user": {"id": "user-a"},
                        "accessToken": "server-token",
                    }
                },
            ),
            _upload_response("doc-a"),
            _create_task_response("task-a"),
            _query_task_response("task-a", "succeeded"),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        response = next(responses)
        response.request = request
        return response

    monkeypatch.setattr(
        import_knowledge_batch,
        "_client_transport",
        httpx.MockTransport(handler),
    )
    def load_settings(_source_dir: Path) -> import_knowledge_batch.RuntimeSettings:
        return import_knowledge_batch.RuntimeSettings(
            source_dir=source_dir,
            base_url="http://test",
            email="seed@example.com",
            password="not-printed",
            display_name="Seed User",
            poll_interval_seconds=0,
            index_wait_seconds=10,
        )

    monkeypatch.setattr(import_knowledge_batch, "_load_runtime_settings", load_settings)

    exit_code = run(["--source-dir", str(source_dir)])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["succeeded"] == 1
    assert output["failed"] == 0
    serialized = json.dumps(output)
    assert "server-token" not in serialized
    assert "not-printed" not in serialized
    assert "private body" not in serialized
    assert requests == [
        "/auth/register",
        "/auth/login",
        "/knowledge-bases/kb_user-a/documents",
        "/knowledge-bases/kb_user-a/documents/doc-a/index-tasks",
        "/knowledge-bases/kb_user-a/documents/doc-a/index-tasks/task-a",
    ]


def _markdown_files(tmp_path: Path, names: list[str]) -> list[Path]:
    source = tmp_path / "knowledge"
    source.mkdir()
    for name in names:
        (source / name).write_text(f"content for {name}", encoding="utf-8")
    return discover_markdown_files(source)


def _upload_response(document_id: str) -> httpx.Response:
    return httpx.Response(201, json={"data": {"document": {"id": document_id}}})


def _create_task_response(task_id: str) -> httpx.Response:
    return httpx.Response(
        202,
        json={"data": {"task": {"id": task_id, "status": "pending"}, "scheduled": True}},
    )


def _query_task_response(task_id: str, status: str) -> httpx.Response:
    return httpx.Response(200, json={"data": {"id": task_id, "status": status}})
