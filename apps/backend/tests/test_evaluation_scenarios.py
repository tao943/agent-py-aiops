import asyncio
from dataclasses import asdict
from pathlib import Path

import pytest
import yaml

from super_ai.evaluation import (
    ScenarioBundle,
    SnapshotMcpClient,
    load_public_scenario,
    load_scenario_oracle,
    validate_scenario_bundle,
)

SCENARIOS = Path(__file__).resolve().parents[3] / "benchmarks" / "agentpy" / "scenarios"
NEW_PAIRS = (("APY-002", "APY-011"), ("APY-007", "APY-012"))
NEW_SCENARIOS = tuple(item for pair in NEW_PAIRS for item in pair)


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


@pytest.mark.parametrize(
    "answer_key",
    (
        "oracle",
        "Primary Cause",
        "Primary   Cause",
        "required-evidence",
        "required-rule-outs",
    ),
)
def test_loader_rejects_nested_normalized_answer_keys(
    valid_scenario_dir: Path,
    answer_key: str,
) -> None:
    scenario_file = valid_scenario_dir / "scenario.yaml"
    scenario_file.write_text(
        scenario_file.read_text(encoding="utf-8")
        + f'extensions:\n  nested:\n    "{answer_key}": leaked\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="ground-truth keys"):
        load_public_scenario(valid_scenario_dir)


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


def test_paired_502_cases_have_same_symptom_and_different_mechanisms() -> None:
    process_down = load_public_scenario(SCENARIOS / "APY-003")
    port_mismatch = load_public_scenario(SCENARIOS / "APY-006")
    process_oracle = load_scenario_oracle(SCENARIOS / "APY-003")
    port_oracle = load_scenario_oracle(SCENARIOS / "APY-006")

    assert process_down.symptom_family == port_mismatch.symptom_family
    assert process_down.alert["alertname"] == port_mismatch.alert["alertname"]
    assert process_down.title == port_mismatch.title
    assert process_down.title == "Checkout requests through the gateway are returning HTTP 502."
    assert process_down.title == process_down.alert["summary"]
    assert port_mismatch.title == port_mismatch.alert["summary"]
    assert process_oracle.primary_cause.mechanism == "process_unavailable"
    assert port_oracle.primary_cause.mechanism == "upstream_port_mismatch"

    validate_scenario_bundle(
        ScenarioBundle(
            public=process_down,
            oracle=process_oracle,
            root=SCENARIOS / "APY-003",
        )
    )
    validate_scenario_bundle(
        ScenarioBundle(
            public=port_mismatch,
            oracle=port_oracle,
            root=SCENARIOS / "APY-006",
        )
    )


@pytest.mark.parametrize(("left_id", "right_id"), NEW_PAIRS)
def test_new_pairs_share_public_inputs_and_differ_in_oracle(
    left_id: str,
    right_id: str,
) -> None:
    left = load_public_scenario(SCENARIOS / left_id)
    right = load_public_scenario(SCENARIOS / right_id)
    left_oracle = load_scenario_oracle(SCENARIOS / left_id)
    right_oracle = load_scenario_oracle(SCENARIOS / right_id)

    assert left.title == right.title == left.alert["summary"] == right.alert["summary"]
    assert left.alert == right.alert
    assert left.hypotheses == right.hypotheses
    assert left_oracle.primary_cause.mechanism != right_oracle.primary_cause.mechanism


@pytest.mark.parametrize("scenario_id", NEW_SCENARIOS)
def test_new_scenario_has_isolated_evidence_rule_out_and_four_tools(
    scenario_id: str,
) -> None:
    root = SCENARIOS / scenario_id
    public = load_public_scenario(root)
    oracle = load_scenario_oracle(root)
    client = SnapshotMcpClient.from_yaml(root / public.snapshot_file)
    serialized_public = repr(asdict(public))

    assert len(oracle.required_evidence) >= 2
    assert len(oracle.required_rule_outs) == 1
    assert len(asyncio.run(client.discover_tools())) == 4
    assert oracle.primary_cause.mechanism not in serialized_public
    assert oracle.primary_cause.trigger not in serialized_public
    assert all(item.id not in serialized_public for item in oracle.required_evidence)
    validate_scenario_bundle(
        ScenarioBundle(
            public=public,
            oracle=oracle,
            root=root,
        )
    )


@pytest.mark.parametrize("scenario_id", NEW_SCENARIOS)
def test_new_scenario_records_agentpy_synthetic_provenance(scenario_id: str) -> None:
    path = SCENARIOS / scenario_id / "provenance.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert payload["type"] == "agentpy-original"
    assert "project-synthesized" in payload["transformation"]
    assert str(payload["accessed"]) == "2026-08-12"
    assert payload["license_notes"]
    assert all(
        reference.startswith("https://") for reference in payload["validation_references"]
    )
