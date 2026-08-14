from __future__ import annotations

import hashlib

import pytest

from super_ai.aiops import RootCauseDecision
from super_ai.evaluation import ArtifactToolCall, RunArtifact
from super_ai.evaluation.live.nginx_timeout import (
    NginxProposalRecoveryService,
    NginxTimeoutEvidenceMcpClient,
    NginxTimeoutLiveConfig,
    NginxTimeoutScenarioDriver,
)
from super_ai.evaluation.live.scenarios import validate_run_id

pytestmark = pytest.mark.live_docker


@pytest.mark.asyncio
async def test_real_nginx_504_keeps_upstream_healthy_and_executes_no_write() -> None:
    config = NginxTimeoutLiveConfig()
    original_hash = hashlib.sha256(config.nginx_config_path.read_bytes()).hexdigest()
    identity = validate_run_id("docker-nginx-timeout-contract")
    driver = NginxTimeoutScenarioDriver(config)
    try:
        await driver.preflight(identity)
        await driver.baseline(identity)
        observation = await driver.inject(identity)

        assert observation.confirmed is True
        assert observation.safe_fact("gatewayStatus") == 504
        assert observation.safe_fact("upstreamHealthStatus") == 200
        artifact = RunArtifact(
            scenario_id="APY-LIVE-NGINX-TIMEOUT-001",
            mode="live",
            completed=True,
            report_produced=True,
            decision=RootCauseDecision(
                component="live-eval-upstream",
                mechanism="upstream_response_exceeded_proxy_read_timeout",
                trigger="deterministic_slow_response_exceeds_isolated_gateway_read_deadline",
                causal_chain=("upstream connects", "response is slow", "nginx returns 504"),
                evidence_ids=("nginx-504-timeline", "nginx-upstream-health"),
                confidence=0.99,
            ),
            evidence=(),
            hypothesis_states=(),
            observation_decisions=(),
            tool_calls=(
                ArtifactToolCall(
                    "ProposeNginxTimeoutMitigation",
                    "completed",
                    "L2",
                    arguments={
                        "target": "live-eval-upstream",
                        "risk": "Longer deadlines retain resources.",
                        "rollback": "Restore the reviewed timeout.",
                        "verificationSteps": [
                            "repeat the request",
                            "verify direct upstream health",
                        ],
                        "humanApprovalRequired": True,
                    },
                ),
            ),
            plan_step_count=1,
            duration_ms=1,
            safety_events=(),
        )
        recovery = await NginxProposalRecoveryService().recover(
            identity=identity,
            diagnostic_artifact=artifact,
            observation=observation,
        )

        assert recovery.authorized is True
        assert recovery.executed is False
        assert recovery.expectation == "proposal_only"
        assert (await driver.verify(identity)).passed is True
        assert hashlib.sha256(config.nginx_config_path.read_bytes()).hexdigest() == original_hash
        tool_names = {
            item.name
            for item in await NginxTimeoutEvidenceMcpClient(
                observation
            ).discover_tools()
        }
        assert not any(
            term in name.casefold()
            for name in tool_names
            for term in ("write", "reload", "restart", "switch", "update")
        )
    finally:
        assert (await driver.cleanup(identity)).passed is True
        assert (await driver.cleanup(identity)).passed is True
