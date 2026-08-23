"""Allowlisted Compose recovery execution and independent verification."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from typing import Literal, Protocol, cast

import httpx

from super_ai.alert_ingestion.repositories import AlertIncidentRecord
from super_ai.recovery.config import ComposeRecoveryTarget
from super_ai.recovery.contracts import (
    RecoveryCheck,
    RecoveryExecutionResult,
    RecoveryVerificationResult,
)

_COMMAND_TIMEOUT_SECONDS = 30.0
_PROBE_TIMEOUT_SECONDS = 3.0
_MAX_COMMAND_OUTPUT_CHARS = 100_000
_CONTAINER_ID = re.compile(r"^[a-f0-9]{12,64}$")


@dataclass(frozen=True, slots=True)
class ArgvRunRequest:
    argv: tuple[str, ...]
    timeout_seconds: float
    shell: Literal[False] = False


@dataclass(frozen=True, slots=True)
class ArgvRunResult:
    return_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    process_started: bool
    duration_ms: int


class ArgvRunner(Protocol):
    async def run(self, request: ArgvRunRequest) -> ArgvRunResult: ...


@dataclass(frozen=True, slots=True)
class ComposeContainerIdentity:
    container_id: str
    service: str
    started_at: str
    project: str = ""


@dataclass(frozen=True, slots=True)
class ComposePreflightResult:
    allowed: bool
    identity: ComposeContainerIdentity | None
    safe_reason_code: str | None


class ComposeInspectionBoundary(Protocol):
    async def preflight(self) -> ComposePreflightResult: ...


class RecoveryHttpProbe(Protocol):
    async def succeeded(self, url: str) -> bool: ...


class IncidentStatusReader(Protocol):
    async def get_owned(
        self, *, owner_user_id: str, incident_id: str
    ) -> AlertIncidentRecord | None: ...


class AsyncioArgvRunner:
    """Execute a fixed argv without shell interpretation."""

    async def run(self, request: ArgvRunRequest) -> ArgvRunResult:
        started_at = monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *request.argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            return ArgvRunResult(
                None,
                "",
                "",
                False,
                False,
                _duration_ms(started_at),
            )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=request.timeout_seconds,
            )
        except TimeoutError:
            await _terminate_process_tree(process)
            return ArgvRunResult(
                process.returncode,
                "",
                "",
                True,
                True,
                _duration_ms(started_at),
            )
        return ArgvRunResult(
            process.returncode,
            _decode_output(stdout),
            _decode_output(stderr),
            False,
            True,
            _duration_ms(started_at),
        )


class ComposeRecoveryExecutor:
    def __init__(
        self,
        target: ComposeRecoveryTarget,
        runner: ArgvRunner,
        *,
        command_timeout_seconds: float = _COMMAND_TIMEOUT_SECONDS,
    ) -> None:
        if command_timeout_seconds <= 0:
            raise ValueError("compose_command_timeout_invalid")
        self._target = target
        self._runner = runner
        self._command_timeout_seconds = command_timeout_seconds

    async def preflight(self) -> ComposePreflightResult:
        result = await self._runner.run(
            ArgvRunRequest(
                argv=(
                    "docker",
                    "compose",
                    "-f",
                    str(self._target.compose_file),
                    "ps",
                    "--format",
                    "json",
                    self._target.service,
                ),
                timeout_seconds=self._command_timeout_seconds,
            )
        )
        if result.timed_out:
            return ComposePreflightResult(False, None, "compose_preflight_timeout")
        if result.return_code != 0:
            return ComposePreflightResult(False, None, "compose_preflight_unavailable")
        partial = _parse_identity(result.stdout, self._target.service)
        if partial is None:
            return ComposePreflightResult(False, None, "compose_identity_invalid")
        started = await self._runner.run(
            ArgvRunRequest(
                argv=(
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.StartedAt}}",
                    partial.container_id,
                ),
                timeout_seconds=self._command_timeout_seconds,
            )
        )
        started_at = started.stdout.strip()
        if (
            started.timed_out
            or started.return_code != 0
            or not started_at
            or len(started_at) > 80
        ):
            return ComposePreflightResult(False, None, "compose_identity_invalid")
        identity = ComposeContainerIdentity(
            partial.container_id,
            partial.service,
            started_at,
            partial.project,
        )
        return ComposePreflightResult(True, identity, None)

    async def execute_once(
        self,
        identity: ComposeContainerIdentity,
    ) -> RecoveryExecutionResult:
        if (
            identity.service != self._target.service
            or not _CONTAINER_ID.fullmatch(identity.container_id)
            or not identity.container_id
            or not identity.started_at
            or not identity.project
        ):
            return RecoveryExecutionResult(
                False,
                True,
                "compose_preflight_identity_invalid",
                0,
            )
        result = await self._runner.run(
            ArgvRunRequest(
                argv=(
                    "docker",
                    "compose",
                    "-f",
                    str(self._target.compose_file),
                    "restart",
                    self._target.service,
                ),
                timeout_seconds=self._command_timeout_seconds,
            )
        )
        if result.timed_out and result.process_started:
            return RecoveryExecutionResult(
                False,
                False,
                "compose_restart_outcome_unknown",
                result.duration_ms,
            )
        if result.return_code != 0:
            return RecoveryExecutionResult(
                False,
                True,
                "compose_restart_failed",
                result.duration_ms,
            )
        return RecoveryExecutionResult(
            True,
            True,
            "compose_restart_completed",
            result.duration_ms,
        )


class HttpxRecoveryProbe:
    async def succeeded(self, url: str) -> bool:
        try:
            async with httpx.AsyncClient(
                timeout=_PROBE_TIMEOUT_SECONDS,
                trust_env=False,
            ) as client:
                response = await client.get(url)
            return 200 <= response.status_code < 300
        except httpx.HTTPError:
            return False


class ComposeRecoveryVerifier:
    def __init__(
        self,
        *,
        target: ComposeRecoveryTarget,
        inspector: ComposeInspectionBoundary,
        probes: RecoveryHttpProbe,
        incidents: IncidentStatusReader,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._target = target
        self._inspector = inspector
        self._probes = probes
        self._incidents = incidents
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def verify(
        self,
        *,
        owner_user_id: str,
        incident_id: str,
        before: ComposeContainerIdentity,
    ) -> RecoveryVerificationResult:
        after_result = await self._safe_preflight()
        health = await self._safe_probe(self._target.health_url)
        business = await self._safe_probe(self._target.business_probe_url)
        incident = await self._safe_incident(owner_user_id, incident_id)
        after = after_result.identity
        identity_changed = bool(
            after_result.allowed
            and after is not None
            and after.service == self._target.service
            and (
                after.container_id != before.container_id
                or after.started_at != before.started_at
            )
        )
        checked_at = self._now()
        checks = (
            RecoveryCheck(
                "container_identity_changed",
                "passed" if identity_changed else "failed",
                "Container identity or startup time changed."
                if identity_changed
                else "Container identity did not change as required.",
                checked_at,
            ),
            RecoveryCheck(
                "service_health",
                "passed" if health else "failed",
                "Service health probe passed."
                if health
                else "Service health probe failed.",
                checked_at,
            ),
            RecoveryCheck(
                "business_probe",
                "passed" if business else "failed",
                "Business probe passed."
                if business
                else "Business probe failed.",
                checked_at,
            ),
            RecoveryCheck(
                "incident_resolved",
                "passed" if incident is not None and incident.status == "resolved" else "failed",
                "Incident is resolved."
                if incident is not None and incident.status == "resolved"
                else "Incident is not resolved.",
                checked_at,
            ),
        )
        passed = all(check.status == "passed" for check in checks)
        return RecoveryVerificationResult(
            passed,
            checks,
            "compose_recovery_verified" if passed else "compose_verification_failed",
        )

    async def _safe_preflight(self) -> ComposePreflightResult:
        try:
            return await self._inspector.preflight()
        except (OSError, RuntimeError, ValueError):
            return ComposePreflightResult(False, None, "compose_identity_unavailable")

    async def _safe_probe(self, url: str) -> bool:
        try:
            return await self._probes.succeeded(url)
        except (OSError, RuntimeError, ValueError, httpx.HTTPError):
            return False

    async def _safe_incident(
        self,
        owner_user_id: str,
        incident_id: str,
    ) -> AlertIncidentRecord | None:
        try:
            return await self._incidents.get_owned(
                owner_user_id=owner_user_id,
                incident_id=incident_id,
            )
        except (OSError, RuntimeError, ValueError):
            return None


def _parse_identity(
    raw: str,
    expected_service: str,
) -> ComposeContainerIdentity | None:
    items = _json_objects(raw)
    if len(items) != 1:
        return None
    item = items[0]
    container_id = item.get("ID")
    service = item.get("Service")
    project = item.get("Project")
    if not all(
        isinstance(value, str) and value
        for value in (container_id, service, project)
    ):
        return None
    if service != expected_service or not _CONTAINER_ID.fullmatch(cast(str, container_id)):
        return None
    return ComposeContainerIdentity(
        cast(str, container_id),
        cast(str, service),
        "pending",
        cast(str, project),
    )


def _json_objects(raw: str) -> tuple[Mapping[str, object], ...]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    candidates: Sequence[object]
    if isinstance(value, list):
        candidates = cast(list[object], value)
    else:
        candidates = (value,)
    result: list[Mapping[str, object]] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            return ()
        mapping = cast(Mapping[object, object], candidate)
        if not all(isinstance(key, str) for key in mapping):
            return ()
        result.append(cast(Mapping[str, object], mapping))
    return tuple(result)


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    if os.name == "nt":
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=5.0)
        except (OSError, TimeoutError):
            pass
    try:
        process.kill()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except TimeoutError:
        pass


def _duration_ms(started_at: float) -> int:
    return max(0, round((monotonic() - started_at) * 1000))


def _decode_output(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")[:_MAX_COMMAND_OUTPUT_CHARS]
