from __future__ import annotations

import shutil
from pathlib import Path
from typing import cast

import pytest
import yaml

from super_ai.evaluation.live.scenarios import (
    load_live_oracle,
    load_live_scenario,
    resolve_live_scenario_directory,
    validate_run_id,
)

LIVE_SCENARIOS = Path(__file__).resolve().parents[3] / "benchmarks" / "agentpy" / "live"


def test_repository_contains_exactly_four_live_scenarios() -> None:
    assert sorted(path.name for path in LIVE_SCENARIOS.iterdir() if path.is_dir()) == [
        "APY-LIVE-NGINX-TIMEOUT-001",
        "APY-LIVE-PG-DEADLOCK-001",
        "APY-LIVE-PG-LOCK-001",
        "APY-LIVE-REDIS-MAXCLIENTS-001",
    ]
    assert all(
        (path / "scenario.yaml").is_file()
        and (path / "ground_truth.yaml").is_file()
        and (path / "provenance.yaml").is_file()
        for path in LIVE_SCENARIOS.iterdir()
        if path.is_dir()
    )


@pytest.mark.parametrize(
    ("scenario_id", "driver", "mechanism", "expectation"),
    [
        (
            "APY-LIVE-PG-DEADLOCK-001",
            "postgres_deadlock",
            "opposite_order_transaction_deadlock",
            "executed_recovery",
        ),
        (
            "APY-LIVE-REDIS-MAXCLIENTS-001",
            "redis_maxclients",
            "benchmark_clients_exhausted_maxclients",
            "executed_recovery",
        ),
        (
            "APY-LIVE-NGINX-TIMEOUT-001",
            "nginx_timeout",
            "upstream_response_exceeded_proxy_read_timeout",
            "proposal_only",
        ),
    ],
)
def test_loads_expansion_live_scenarios_with_private_recovery_policy(
    scenario_id: str,
    driver: str,
    mechanism: str,
    expectation: str,
) -> None:
    root = LIVE_SCENARIOS / scenario_id
    public = load_live_scenario(root)
    oracle = load_live_oracle(root)

    assert public.id == scenario_id
    assert public.driver == driver
    assert oracle.primary_cause.mechanism == mechanism
    assert oracle.recovery_expectation == expectation
    assert oracle.root_cause_semantics is not None
    assert len(oracle.root_cause_semantics.causal_milestones) == 3
    assert "recovery_expectation" not in root.joinpath("scenario.yaml").read_text(
        encoding="utf-8"
    )


def test_loads_answer_free_postgres_lock_live_scenario() -> None:
    scenario = load_live_scenario(LIVE_SCENARIOS / "APY-LIVE-PG-LOCK-001")

    assert scenario.id == "APY-LIVE-PG-LOCK-001"
    assert scenario.modes == ("live",)
    assert [item.id for item in scenario.hypotheses] == [
        "postgres_lock_blocking",
        "postgres_slow_query_without_lock",
        "postgres_connectivity_failure",
    ]
    assert scenario.driver == "postgres_lock_wait"


def test_oracle_is_loaded_only_by_the_evaluator_boundary() -> None:
    oracle = load_live_oracle(LIVE_SCENARIOS / "APY-LIVE-PG-LOCK-001")

    assert oracle.primary_cause.component == "postgresql"
    assert oracle.primary_cause.mechanism == "row_lock_blocking"
    assert {item.id for item in oracle.required_evidence} == {
        "postgres-lock-wait-event",
        "postgres-blocking-graph",
    }
    assert oracle.root_cause_semantics is not None
    assert oracle.root_cause_semantics.trigger.all_of == ("lock_holder", "row_lock")
    assert [
        item.id for item in oracle.root_cause_semantics.causal_milestones
    ] == ["lock_held", "update_waits", "probe_times_out"]
    assert oracle.recovery_expectation == "executed_recovery"


def test_live_oracle_requires_a_known_recovery_expectation(tmp_path: Path) -> None:
    scenario_dir = _copy_live_scenario(tmp_path)
    payload = cast(
        dict[str, object],
        yaml.safe_load(
            scenario_dir.joinpath("ground_truth.yaml").read_text(encoding="utf-8")
        ),
    )
    payload["recovery_expectation"] = "automatic_restart"
    _write_oracle(scenario_dir, payload)

    with pytest.raises(ValueError, match="recovery_expectation"):
        load_live_oracle(scenario_dir)


def test_live_oracle_rejects_missing_recovery_expectation(tmp_path: Path) -> None:
    scenario_dir = _copy_live_scenario(tmp_path)
    payload = cast(
        dict[str, object],
        yaml.safe_load(
            scenario_dir.joinpath("ground_truth.yaml").read_text(encoding="utf-8")
        ),
    )
    payload.pop("recovery_expectation", None)
    _write_oracle(scenario_dir, payload)

    with pytest.raises(ValueError, match="recovery_expectation"):
        load_live_oracle(scenario_dir)


def _copy_live_scenario(tmp_path: Path) -> Path:
    source = LIVE_SCENARIOS / "APY-LIVE-PG-LOCK-001"
    destination = tmp_path / source.name
    shutil.copytree(source, destination)
    return destination


def _write_oracle(scenario_dir: Path, payload: object) -> None:
    scenario_dir.joinpath("ground_truth.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("semantics", "message"),
    (
        (
                {
                    "concepts": {"row_lock": ["row lock"]},
                    "trigger": {"all_of": ["missing"]},
                    "causal_milestones": [
                        {"id": "first", "all_of": ["row_lock"]},
                        {"id": "second", "all_of": ["row_lock"]},
                        {"id": "third", "all_of": ["row_lock"]},
                    ],
            },
            "unknown concept",
        ),
        (
            {
                "concepts": {"row_lock": [""]},
                "trigger": {"all_of": ["row_lock"]},
                "causal_milestones": [{"id": "lock", "all_of": ["row_lock"]}],
            },
            "aliases",
        ),
        (
            {
                "concepts": {"row_lock": ["row lock"]},
                "trigger": {"all_of": ["row_lock"]},
                    "causal_milestones": [
                        {"id": "lock", "all_of": ["row_lock"]},
                        {"id": "lock", "all_of": ["row_lock"]},
                        {"id": "other", "all_of": ["row_lock"]},
                    ],
            },
            "unique",
        ),
        (
            {
                "concepts": {"row_lock": ["row lock"]},
                "causal_milestones": [{"id": "lock", "all_of": ["row_lock"]}],
            },
            "trigger",
        ),
        (
            {
                "concepts": {"row_lock": ["row lock"]},
                "trigger": {"all_of": ["row_lock"]},
                "causal_milestones": [],
            },
            "causal_milestones",
        ),
        (
            {
                "concepts": {"row_lock": ["row lock"]},
                "trigger": {"all_of": ["row_lock"]},
                "causal_milestones": [
                    {"id": "first", "all_of": ["row_lock"]},
                    {"id": "second", "all_of": ["row_lock"]},
                ],
            },
            "exactly three",
        ),
    ),
)
def test_rejects_invalid_live_root_cause_semantics(
    tmp_path: Path, semantics: dict[str, object], message: str
) -> None:
    scenario_dir = _copy_live_scenario(tmp_path)
    payload = cast(
        dict[str, object],
        yaml.safe_load(
            scenario_dir.joinpath("ground_truth.yaml").read_text(encoding="utf-8")
        ),
    )
    payload["root_cause_semantics"] = semantics
    _write_oracle(scenario_dir, payload)

    with pytest.raises(ValueError, match=message):
        load_live_oracle(scenario_dir)


def test_requires_root_cause_semantics_for_live_oracle(tmp_path: Path) -> None:
    scenario_dir = _copy_live_scenario(tmp_path)
    payload = cast(
        dict[str, object],
        yaml.safe_load(
            scenario_dir.joinpath("ground_truth.yaml").read_text(encoding="utf-8")
        ),
    )
    payload.pop("root_cause_semantics", None)
    _write_oracle(scenario_dir, payload)

    with pytest.raises(ValueError, match="root_cause_semantics"):
        load_live_oracle(scenario_dir)


@pytest.mark.parametrize(
    "run_id",
    (
        "../run-1",
        "run_1",
        "run:1",
        "-run-1",
        "",
        "r" * 65,
    ),
)
def test_rejects_unsafe_run_ids(run_id: str) -> None:
    with pytest.raises(ValueError, match="run ID"):
        validate_run_id(run_id)


def test_validated_run_identity_has_stable_non_input_table_token() -> None:
    identity = validate_run_id("run-PG-001")

    assert identity.run_id == "run-PG-001"
    assert identity.run_token == validate_run_id("run-PG-001").run_token
    assert identity.run_token != "run-pg-001"
    assert len(identity.run_token) == 16
    assert identity.blocker_application_name == "agentpy-live:run-PG-001:blocker"
    assert identity.waiter_application_name == "agentpy-live:run-PG-001:waiter"


@pytest.mark.parametrize(
    "scenario_id",
    ("../APY-LIVE-PG-LOCK-001", "..\\APY-LIVE-PG-LOCK-001", "/tmp/x", "C:\\tmp\\x"),
)
def test_rejects_live_scenario_path_traversal(scenario_id: str) -> None:
    with pytest.raises(ValueError, match="single directory name"):
        resolve_live_scenario_directory(LIVE_SCENARIOS, scenario_id)


def test_rejects_nested_oracle_key_in_public_live_scenario(tmp_path: Path) -> None:
    scenario_dir = tmp_path / "APY-LIVE-TEST-001"
    scenario_dir.mkdir()
    scenario_dir.joinpath("scenario.yaml").write_text(
        "id: APY-LIVE-TEST-001\n"
        "title: test\n"
        "symptom_family: postgresql\n"
        "difficulty: medium\n"
        "modes: [live]\n"
        "driver: postgres_lock_wait\n"
        "alert:\n"
        "  nested:\n"
        "    primary_cause: leaked\n"
        "hypotheses:\n"
        "  - id: h1\n"
        "    description: candidate\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="ground-truth"):
        load_live_scenario(scenario_dir)


def test_rejects_duplicate_live_hypothesis_ids(tmp_path: Path) -> None:
    scenario_dir = tmp_path / "APY-LIVE-TEST-001"
    scenario_dir.mkdir()
    scenario_dir.joinpath("scenario.yaml").write_text(
        "id: APY-LIVE-TEST-001\n"
        "title: test\n"
        "symptom_family: postgresql\n"
        "difficulty: medium\n"
        "modes: [live]\n"
        "driver: postgres_lock_wait\n"
        "alert: {name: lock-wait}\n"
        "hypotheses:\n"
        "  - {id: duplicate, description: first}\n"
        "  - {id: duplicate, description: second}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unique"):
        load_live_scenario(scenario_dir)
