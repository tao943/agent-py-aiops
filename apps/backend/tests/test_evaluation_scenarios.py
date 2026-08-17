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
EXPANSION_SCENARIOS = ("APY-013", "APY-014", "APY-015", "APY-016")


def test_repository_contains_exactly_ten_snapshot_scenarios() -> None:
    scenario_dirs = sorted(path for path in SCENARIOS.iterdir() if path.is_dir())

    assert [path.name for path in scenario_dirs] == [
        "APY-002",
        "APY-003",
        "APY-006",
        "APY-007",
        "APY-011",
        "APY-012",
        "APY-013",
        "APY-014",
        "APY-015",
        "APY-016",
    ]
    assert all((path / "scenario.yaml").is_file() for path in scenario_dirs)
    assert all((path / "ground_truth.yaml").is_file() for path in scenario_dirs)
    assert all((path / "provenance.yaml").is_file() for path in scenario_dirs)
    assert all((path / "snapshot" / "tool_responses.yaml").is_file() for path in scenario_dirs)


def test_all_snapshot_candidates_declare_public_decision_labels() -> None:
    for scenario_dir in sorted(path for path in SCENARIOS.iterdir() if path.is_dir()):
        scenario = load_public_scenario(scenario_dir)

        assert scenario.hypotheses
        assert all(item.decision_label is not None for item in scenario.hypotheses)
        assert all(
            item.decision_label is not None and item.decision_label.component
            for item in scenario.hypotheses
        )
        assert all(
            item.decision_label is not None and item.decision_label.mechanism
            for item in scenario.hypotheses
        )


@pytest.mark.parametrize(
    ("scenario_id", "mechanism", "required_ids"),
    [
        ("APY-013", "opposite_order_transaction_deadlock", {"deadlock-error", "wait-cycle"}),
        (
            "APY-014",
            "benchmark_clients_exhausted_maxclients",
            {"maxclients-capacity", "scoped-client-set"},
        ),
        (
            "APY-015",
            "upstream_response_exceeded_proxy_read_timeout",
            {"connect-succeeded", "response-timeout"},
        ),
        (
            "APY-016",
            "retry_after_ignored_without_backoff",
            {"retry-amplification", "missing-backoff"},
        ),
    ],
)
def test_expansion_scenarios_require_discriminating_evidence(
    scenario_id: str,
    mechanism: str,
    required_ids: set[str],
) -> None:
    root = SCENARIOS / scenario_id
    public = load_public_scenario(root)
    oracle = load_scenario_oracle(root)
    client = SnapshotMcpClient.from_yaml(root / public.snapshot_file)

    assert public.id == scenario_id
    assert oracle.primary_cause.mechanism == mechanism
    assert required_ids <= {milestone.id for milestone in oracle.required_evidence}
    assert len(asyncio.run(client.discover_tools())) >= 3
    public_text = (root / "scenario.yaml").read_text(encoding="utf-8").casefold()
    assert any(
        item.decision_label is not None
        and item.decision_label.mechanism == mechanism
        for item in public.hypotheses
    )
    assert "primary_cause" not in public_text
    validate_scenario_bundle(ScenarioBundle(public=public, oracle=oracle, root=root))


def test_apy_013_loads_private_root_cause_semantics() -> None:
    public = load_public_scenario(SCENARIOS / "APY-013")
    oracle = load_scenario_oracle(SCENARIOS / "APY-013")

    assert oracle.root_cause_semantics is not None
    assert oracle.root_cause_semantics.trigger.all_of == (
        "transaction",
        "order_resource",
        "inventory_resource",
        "opposite_order",
    )
    assert tuple(
        item.id for item in oracle.root_cause_semantics.causal_milestones
    ) == (
        "opposite_resource_acquisition",
        "cyclic_lock_wait",
        "postgres_deadlock_abort",
    )
    serialized_public = repr(asdict(public)).casefold()
    assert "root_cause_semantics" not in serialized_public
    assert "sqlstate 40p01" not in serialized_public


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
    decision_label:
      component: checkout-service
      mechanism: process_unavailable
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
    assert scenario.hypotheses[0].decision_label is not None
    assert scenario.hypotheses[0].decision_label.mechanism == "process_unavailable"
    assert "benchmark_container_stopped" not in serialized


@pytest.mark.parametrize(
    "decision_label",
    (None, {"component": "checkout-service"}, {"mechanism": "process_unavailable"}),
)
def test_snapshot_loader_requires_complete_candidate_decision_label(
    valid_scenario_dir: Path,
    decision_label: object,
) -> None:
    scenario_file = valid_scenario_dir / "scenario.yaml"
    payload = yaml.safe_load(scenario_file.read_text(encoding="utf-8"))
    hypothesis = payload["hypotheses"][0]
    if decision_label is None:
        hypothesis.pop("decision_label")
    else:
        hypothesis["decision_label"] = decision_label
    scenario_file.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="decision_label|component|mechanism"):
        load_public_scenario(valid_scenario_dir)


def test_loader_rejects_oracle_nested_in_candidate_decision_label(
    valid_scenario_dir: Path,
) -> None:
    scenario_file = valid_scenario_dir / "scenario.yaml"
    payload = yaml.safe_load(scenario_file.read_text(encoding="utf-8"))
    payload["hypotheses"][0]["decision_label"]["oracle"] = "leaked"
    scenario_file.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="ground-truth keys"):
        load_public_scenario(valid_scenario_dir)


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
    assert any(
        item.decision_label is not None
        and item.decision_label.mechanism == oracle.primary_cause.mechanism
        for item in public.hypotheses
    )
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
