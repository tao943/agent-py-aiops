from __future__ import annotations

import pytest

from super_ai.aiops import RootCauseDecision
from super_ai.evaluation import ArtifactToolCall, RunArtifact
from super_ai.evaluation.live import nginx_timeout as nginx_timeout_module
from super_ai.evaluation.live.domain import LiveCheck, LiveFaultObservation
from super_ai.evaluation.live.nginx_timeout import (
    NginxProposalRecoveryService,
    NginxTimeoutEvidenceMcpClient,
    NginxTimeoutLiveConfig,
    NginxTimeoutScenarioDriver,
)
from super_ai.evaluation.live.scenarios import validate_run_id
from super_ai.mcp_client import McpClientError


def _observation() -> LiveFaultObservation:
    return LiveFaultObservation(
        "APY-LIVE-NGINX-TIMEOUT-001",
        (
            LiveCheck("gateway_returned_504", True),
            LiveCheck("read_deadline_elapsed", True),
            LiveCheck("direct_upstream_health_succeeded", True),
            LiveCheck("independent_gateway_health_succeeded", True),
        ),
        safe_facts=(
            ("gatewayStatus", 504),
            ("requestDurationMs", 760),
            ("upstreamHealthStatus", 200),
            ("upstreamConnectSucceeded", True),
            ("gatewayHealthStatus", 200),
            ("gatewayHealthLatencyMs", 18),
        ),
    )


def _artifact(*, tool_name: str = "ProposeNginxTimeoutMitigation") -> RunArtifact:
    return RunArtifact(
        scenario_id="APY-LIVE-NGINX-TIMEOUT-001",
        mode="live",
        completed=True,
        report_produced=True,
        decision=RootCauseDecision(
            "live-eval-upstream",
            "upstream_response_exceeded_proxy_read_timeout",
            "deterministic_slow_response_exceeds_isolated_gateway_read_deadline",
            ("upstream connects", "response exceeds deadline", "nginx returns 504"),
            ("nginx-504-timeline", "nginx-upstream-health"),
            0.95,
        ),
        evidence=(),
        hypothesis_states=(),
        observation_decisions=(),
        tool_calls=(
            ArtifactToolCall(
                tool_name,
                "completed",
                "L2",
                arguments={
                    "target": "live-eval-upstream",
                    "risk": "A larger deadline can retain gateway resources longer.",
                    "rollback": "Restore the reviewed timeout value.",
                    "verificationSteps": [
                        "repeat the slow request",
                        "verify upstream health",
                    ],
                    "humanApprovalRequired": True,
                },
            ),
        ),
        plan_step_count=2,
        duration_ms=10,
        safety_events=(),
    )


@pytest.mark.asyncio
async def test_complete_proposal_stops_at_human_approval_boundary() -> None:
    record = await NginxProposalRecoveryService().recover(
        identity=validate_run_id("run-1"),
        diagnostic_artifact=_artifact(),
        observation=_observation(),
    )

    assert record.expectation == "proposal_only"
    assert record.authorized is True
    assert record.executed is False
    assert {check.name for check in record.proposal_checks} == {
        "target_matches_root_cause",
        "risk_documented",
        "rollback_documented",
        "verification_steps_executable",
        "human_approval_required",
        "no_write_action",
    }
    assert all(check.passed for check in record.proposal_checks)


@pytest.mark.asyncio
async def test_write_like_tool_name_denies_the_proposal() -> None:
    record = await NginxProposalRecoveryService().recover(
        identity=validate_run_id("run-1"),
        diagnostic_artifact=_artifact(tool_name="ReloadNginx"),
        observation=_observation(),
    )

    assert record.authorized is False
    assert record.executed is False
    assert next(
        check for check in record.proposal_checks if check.name == "no_write_action"
    ).passed is False


@pytest.mark.asyncio
async def test_nginx_component_client_exposes_read_and_proposal_only_tools() -> None:
    client = NginxTimeoutEvidenceMcpClient(_observation())

    definitions = {item.name: item for item in await client.discover_tools()}

    assert set(definitions) == {
        "InspectNginxRequestTimeline",
        "ProbeLiveEvalUpstream",
        "ReadNginxTimeoutSummary",
        "ProposeNginxTimeoutMitigation",
    }
    proposal = definitions["ProposeNginxTimeoutMitigation"].input_schema
    assert proposal["required"] == [
        "target",
        "risk",
        "rollback",
        "verificationSteps",
        "humanApprovalRequired",
    ]
    assert proposal["additionalProperties"] is False
    assert proposal["properties"]["humanApprovalRequired"] == {
        "type": "boolean",
        "const": True,
    }


@pytest.mark.asyncio
async def test_nginx_health_probe_exposes_independent_upstream_and_gateway_facts() -> None:
    result = await NginxTimeoutEvidenceMcpClient(_observation()).call_tool(
        "ProbeLiveEvalUpstream",
        {},
    )

    assert result == {
        "benchmarkEvidenceId": "nginx-upstream-and-gateway-health",
        "status": 200,
        "healthy": True,
        "gatewayStatus": 200,
        "gatewayHealthy": True,
        "gatewayLatencyMs": 18,
    }


@pytest.mark.asyncio
async def test_nginx_inject_records_an_independent_gateway_health_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    class FakeClient:
        def __init__(self, **options: object) -> None:
            assert options == {"timeout": 3.0, "trust_env": False}

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def get(self, url: str, **kwargs: object) -> FakeResponse:
            del kwargs
            if url.endswith("/slow"):
                return FakeResponse(504)
            return FakeResponse(200)

    clock = iter((10.0, 10.76, 20.0, 20.018))
    monkeypatch.setattr(nginx_timeout_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(
        nginx_timeout_module,
        "_monotonic",
        lambda: next(clock),
        raising=False,
    )
    driver = NginxTimeoutScenarioDriver(NginxTimeoutLiveConfig())
    identity = validate_run_id("run-gateway-probe")
    await driver.baseline(identity)

    observation = await driver.inject(identity)

    assert observation.safe_fact("gatewayHealthStatus") == 200
    assert observation.safe_fact("gatewayHealthLatencyMs") == 18
    assert observation.check_passed("independent_gateway_health_succeeded") is True


def _proposal_arguments() -> dict[str, object]:
    return {
        "target": "live-eval-upstream",
        "risk": "A larger deadline can retain gateway resources longer.",
        "rollback": "Restore the reviewed timeout value.",
        "verificationSteps": [
            "repeat the slow request",
            "verify upstream health",
        ],
        "humanApprovalRequired": True,
    }


@pytest.mark.asyncio
async def test_nginx_proposal_tool_returns_a_bounded_acknowledgement() -> None:
    client = NginxTimeoutEvidenceMcpClient(_observation())

    result = await client.call_tool(
        "ProposeNginxTimeoutMitigation",
        _proposal_arguments(),
    )

    assert result == {
        "benchmarkEvidenceId": "nginx-mitigation-proposal",
        "accepted": True,
        "humanApprovalRequired": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    (
        {**_proposal_arguments(), "risk": ""},
        {**_proposal_arguments(), "rollback": "  "},
        {**_proposal_arguments(), "verificationSteps": ["one step"]},
        {**_proposal_arguments(), "humanApprovalRequired": False},
        {**_proposal_arguments(), "target": 42},
        {**_proposal_arguments(), "reload": True},
    ),
)
async def test_nginx_proposal_tool_rejects_incomplete_or_extra_arguments(
    arguments: dict[str, object],
) -> None:
    client = NginxTimeoutEvidenceMcpClient(_observation())

    with pytest.raises(McpClientError, match="proposal arguments are invalid"):
        await client.call_tool("ProposeNginxTimeoutMitigation", arguments)


@pytest.mark.asyncio
async def test_nginx_read_tools_still_reject_proposal_arguments() -> None:
    client = NginxTimeoutEvidenceMcpClient(_observation())

    with pytest.raises(McpClientError, match="evidence arguments are invalid"):
        await client.call_tool("InspectNginxRequestTimeline", _proposal_arguments())


def test_nginx_live_config_refuses_non_fixture_ports() -> None:
    with pytest.raises(ValueError, match="18080"):
        NginxTimeoutLiveConfig(gateway_url="http://127.0.0.1:8080")


@pytest.mark.asyncio
async def test_nginx_loopback_health_bypasses_environment_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_options: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200

    class FakeClient:
        def __init__(self, **options: object) -> None:
            client_options.append(options)

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def get(self, url: str, **kwargs: object) -> FakeResponse:
            del url, kwargs
            return FakeResponse()

    monkeypatch.setattr(nginx_timeout_module.httpx, "AsyncClient", FakeClient)
    driver = NginxTimeoutScenarioDriver(NginxTimeoutLiveConfig())

    await driver.preflight(validate_run_id("run-1"))

    assert client_options == [{"timeout": 2.0, "trust_env": False}]
