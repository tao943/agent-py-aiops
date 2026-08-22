from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from super_ai.evaluation.live.auto_closure import (
    PersistedDiagnosticOutcome,
    authorize_order_pool_recovery,
)
from super_ai.evaluation.live.cli import safe_output
from super_ai.evaluation.live.scenarios import validate_run_id
from test_live_auto_closure import _artifact, _observation
from test_live_order_pool_contracts import _driver


@pytest.mark.parametrize(
    "run_id",
    ("../APY-LIVE-ORDER-POOL-LEAK-001", "..\\oracle", "run/ground_truth"),
)
def test_auto_closure_rejects_path_traversal_run_ids(run_id: str) -> None:
    with pytest.raises(ValueError):
        validate_run_id(run_id)


@pytest.mark.parametrize("forbidden_key", ("faultToken", "runToken", "recoveryTarget"))
def test_driver_restore_rejects_secret_or_authority_fields(forbidden_key: str) -> None:
    driver, _, _ = _driver()
    identity = validate_run_id("secure-resume")
    state: dict[str, object] = {
        "originalGeneration": "generation-1",
        "unrelatedSessionFingerprints": ["a" * 64],
        forbidden_key: "must-not-persist",
    }

    with pytest.raises(ValueError, match="resume_state_invalid"):
        driver.restore(identity, state)


@pytest.mark.parametrize(
    ("artifact", "expected_code"),
    (
        (replace(_artifact(), scenario_id="other"), "scenario_mismatch"),
        (
            replace(
                _artifact(),
                decision=replace(_artifact().decision, component="backend"),
            ),
            "component_mismatch",
        ),
        (
            replace(
                _artifact(),
                decision=replace(_artifact().decision, mechanism="connectivity_failure"),
            ),
            "mechanism_mismatch",
        ),
    ),
)
def test_recovery_authority_requires_code_owned_diagnostic_predicates(
    artifact,
    expected_code: str,
) -> None:
    authorization = authorize_order_pool_recovery(
        PersistedDiagnosticOutcome(artifact, "sufficient"),
        _observation(),
        driver_owns_identity=True,
        expected_task_id="diagnostic-1",
    )
    assert not authorization.execution_permitted
    assert authorization.code == expected_code


def test_public_output_drops_oracle_and_untrusted_recovery_target() -> None:
    payload = safe_output(
        command="run",
        scenario_id="APY-LIVE-ORDER-POOL-LEAK-001",
        run_id="secure-output",
        status="failed",
        result={
            "validity": "VALID_FAIL",
            "oracle": {"primaryCause": "answer"},
            "recoveryTarget": "backend",
        },
    )

    serialized = str(payload)
    assert "primaryCause" not in serialized
    assert "recoveryTarget" not in serialized


def test_acceptance_implementation_has_no_manual_alert_publisher() -> None:
    root = Path(__file__).resolve().parents[3]
    implementation = (
        root
        / "apps"
        / "backend"
        / "src"
        / "super_ai"
        / "evaluation"
        / "live"
        / "cli.py"
    ).read_text(encoding="utf-8")
    assert "publish_alertmanager" not in implementation
