from __future__ import annotations

import json
from pathlib import Path

import pytest

from super_ai.recovery.compose import (
    ArgvRunRequest,
    ArgvRunResult,
    ComposeContainerIdentity,
    ComposeRecoveryExecutor,
)
from super_ai.recovery.config import ComposeRecoveryTarget, DiagnosticSelector


def _target() -> ComposeRecoveryTarget:
    return ComposeRecoveryTarget(
        target_key="live-eval-order-api",
        compose_file=Path("D:/project/infra/compose.yaml"),
        service="live-eval-order-api",
        automatic_recovery_enabled=True,
        health_url="http://127.0.0.1:18081/health",
        business_probe_url="http://127.0.0.1:18081/probe",
        diagnostic_selector=DiagnosticSelector("order-api", ("pool_leak",), ("Tool.fact",)),
    )


def _ps_result(
    *,
    service: str = "live-eval-order-api",
    container_id: str = "aaaaaaaaaaaa",
) -> ArgvRunResult:
    return ArgvRunResult(
        return_code=0,
        stdout=json.dumps(
            [
                {
                    "ID": container_id,
                    "Service": service,
                    "Project": "agentpy",
                    "CreatedAt": "2026-08-23T08:00:00Z",
                }
            ]
        ),
        stderr="",
        timed_out=False,
        process_started=True,
        duration_ms=12,
    )


class Runner:
    def __init__(self, results: list[ArgvRunResult]) -> None:
        self.results = results
        self.requests: list[ArgvRunRequest] = []

    async def run(self, request: ArgvRunRequest) -> ArgvRunResult:
        self.requests.append(request)
        return self.results.pop(0)


@pytest.mark.asyncio
async def test_preflight_and_restart_use_only_fixed_non_shell_argv() -> None:
    runner = Runner(
        [
            _ps_result(),
            ArgvRunResult(
                0,
                "2026-08-23T08:00:00.000000000Z\n",
                "",
                False,
                True,
                4,
            ),
            ArgvRunResult(0, "must-not-escape", "secret-stderr", False, True, 44),
        ]
    )
    executor = ComposeRecoveryExecutor(_target(), runner)  # type: ignore[arg-type]

    preflight = await executor.preflight()
    assert preflight.allowed is True
    assert preflight.identity is not None
    result = await executor.execute_once(preflight.identity)

    ps_request, identity_request, restart_request = runner.requests
    assert ps_request.argv == (
        "docker",
        "compose",
        "-f",
        str(_target().compose_file),
        "ps",
        "--format",
        "json",
        _target().service,
    )
    assert identity_request.argv == (
        "docker",
        "inspect",
        "--format",
        "{{.State.StartedAt}}",
        "aaaaaaaaaaaa",
    )
    assert restart_request.argv == (
        "docker",
        "compose",
        "-f",
        str(_target().compose_file),
        "restart",
        _target().service,
    )
    assert restart_request.shell is False
    assert restart_request.timeout_seconds == 30.0
    assert result.succeeded is True
    assert str(_target().compose_file) not in result.safe_summary
    assert "stdout" not in result.safe_summary
    assert "stderr" not in result.safe_summary


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ps_result",
    [
        ArgvRunResult(1, "", "private error", False, True, 10),
        ArgvRunResult(0, "[]", "", False, True, 10),
        _ps_result(service="other-service"),
        ArgvRunResult(0, "not-json", "", False, True, 10),
    ],
)
async def test_invalid_or_missing_identity_fails_before_side_effect(
    ps_result: ArgvRunResult,
) -> None:
    runner = Runner([ps_result])
    executor = ComposeRecoveryExecutor(_target(), runner)  # type: ignore[arg-type]

    preflight = await executor.preflight()

    assert preflight.allowed is False
    assert preflight.identity is None
    assert len(runner.requests) == 1
    assert preflight.safe_reason_code is not None
    assert str(_target().compose_file) not in preflight.safe_reason_code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity",
    [
        ComposeContainerIdentity(
            "aaaaaaaaaaaa", "other-service", "created", "agentpy"
        ),
        ComposeContainerIdentity("not-a-container", "live-eval-order-api", "created", "agentpy"),
        ComposeContainerIdentity("aaaaaaaaaaaa", "live-eval-order-api", "created", ""),
    ],
)
async def test_rejects_identity_not_bound_to_target_without_starting_restart(
    identity: ComposeContainerIdentity,
) -> None:
    runner = Runner([])
    executor = ComposeRecoveryExecutor(_target(), runner)  # type: ignore[arg-type]

    result = await executor.execute_once(identity)

    assert result.succeeded is False
    assert result.outcome_known is True
    assert result.safe_summary == "compose_preflight_identity_invalid"
    assert runner.requests == []


@pytest.mark.asyncio
async def test_timeout_after_restart_started_is_unknown_and_never_retried() -> None:
    runner = Runner([ArgvRunResult(None, "private", "private", True, True, 30_000)])
    executor = ComposeRecoveryExecutor(_target(), runner)  # type: ignore[arg-type]
    identity = ComposeContainerIdentity(
        "aaaaaaaaaaaa",
        "live-eval-order-api",
        "2026-08-23T08:00:00Z",
        "agentpy",
    )

    first = await executor.execute_once(identity)

    assert first.succeeded is False
    assert first.outcome_known is False
    assert first.safe_summary == "compose_restart_outcome_unknown"
    assert len(runner.requests) == 1
