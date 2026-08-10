from pathlib import Path

import pytest

from super_ai.evaluation import (
    ScenarioBundle,
    load_public_scenario,
    load_scenario_oracle,
    validate_scenario_bundle,
)


@pytest.fixture
def valid_scenario_dir(tmp_path: Path) -> Path:
    scenario_dir = tmp_path / "APY-003"
    scenario_dir.mkdir()
    (scenario_dir / "scenario.yaml").write_text(
        """
id: APY-003
title: Checkout upstream returns 502
symptom_family: nginx_upstream_5xx
difficulty: medium
modes: [snapshot]
alert:
  alertname: CheckoutUpstream5xxHigh
  service: checkout-service
hypotheses:
  - id: upstream_process_down
    description: The upstream process is unavailable.
snapshot_file: snapshot/tool_responses.yaml
""".lstrip(),
        encoding="utf-8",
    )
    (scenario_dir / "ground_truth.yaml").write_text(
        """
primary_cause:
  component: checkout-service
  mechanism: process_unavailable
  trigger: benchmark_container_stopped
contributing_causes: []
causal_chain:
  - checkout container stopped
  - nginx connection refused
required_evidence:
  - id: container-not-running
    alternatives:
      - [container-status-exited]
required_rule_outs: [upstream_port_mismatch]
forbidden_claims: [dns_resolution_failure]
""".lstrip(),
        encoding="utf-8",
    )
    return scenario_dir


def test_public_scenario_excludes_ground_truth(valid_scenario_dir: Path) -> None:
    scenario = load_public_scenario(valid_scenario_dir)

    serialized = repr(scenario)
    assert scenario.id == "APY-003"
    assert scenario.symptom_family == "nginx_upstream_5xx"
    assert "process_unavailable" not in serialized
    assert "benchmark_container_stopped" not in serialized


def test_oracle_requires_primary_component_mechanism_and_trigger(
    valid_scenario_dir: Path,
) -> None:
    oracle = load_scenario_oracle(valid_scenario_dir)

    assert oracle.primary_cause.component == "checkout-service"
    assert oracle.primary_cause.mechanism == "process_unavailable"
    assert oracle.primary_cause.trigger == "benchmark_container_stopped"


def test_loader_rejects_public_file_with_answer_keys(tmp_path: Path) -> None:
    scenario_dir = tmp_path / "bad"
    scenario_dir.mkdir()
    (scenario_dir / "scenario.yaml").write_text(
        "id: BAD\nsymptom_family: x\nmetadata:\n  ground_truth: leaked\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="ground-truth keys"):
        load_public_scenario(scenario_dir)


def test_bundle_validation_rejects_missing_snapshot(
    valid_scenario_dir: Path,
) -> None:
    bundle = ScenarioBundle(
        public=load_public_scenario(valid_scenario_dir),
        oracle=load_scenario_oracle(valid_scenario_dir),
        root=valid_scenario_dir,
    )

    with pytest.raises(ValueError, match="snapshot"):
        validate_scenario_bundle(bundle)
