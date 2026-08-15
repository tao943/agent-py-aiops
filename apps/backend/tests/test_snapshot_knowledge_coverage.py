from pathlib import Path

import pytest

from super_ai.evaluation.knowledge_coverage import load_snapshot_knowledge_coverage

REPO_ROOT = Path(__file__).resolve().parents[3]
COVERAGE = REPO_ROOT / "benchmarks" / "agentpy" / "retrieval" / "snapshot_knowledge_coverage.yaml"
SCENARIOS = REPO_ROOT / "benchmarks" / "agentpy" / "scenarios"
KNOWLEDGE = REPO_ROOT / "docs" / "knowledge-candidates"


def test_coverage_maps_every_snapshot_to_existing_generic_cards() -> None:
    rows = load_snapshot_knowledge_coverage(
        COVERAGE,
        scenario_root=SCENARIOS,
        knowledge_root=KNOWLEDGE,
    )

    assert {row.snapshot_id for row in rows} == {
        path.name for path in SCENARIOS.iterdir() if path.is_dir()
    }
    assert all(row.documents for row in rows)
    assert len(list(KNOWLEDGE.glob("*.md"))) == 30


@pytest.mark.parametrize(
    "bad_document",
    ["../ground_truth.yaml", "APY-013.md", "missing.md"],
)
def test_coverage_rejects_paths_answers_and_missing_cards(
    tmp_path: Path,
    bad_document: str,
) -> None:
    scenario_root = tmp_path / "scenarios"
    knowledge_root = tmp_path / "knowledge"
    scenario_root.mkdir()
    knowledge_root.mkdir()
    (scenario_root / "APY-013").mkdir()
    path = tmp_path / "coverage.yaml"
    path.write_text(f"coverage:\n  APY-013: [{bad_document}]\n", encoding="utf-8")

    with pytest.raises(ValueError):
        load_snapshot_knowledge_coverage(
            path,
            scenario_root=scenario_root,
            knowledge_root=knowledge_root,
        )


def test_coverage_rejects_missing_or_extra_snapshot_ids(tmp_path: Path) -> None:
    path = tmp_path / "coverage.yaml"
    path.write_text(
        "coverage:\n  APY-013: [postgres-deadlock.md]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="match repository scenarios exactly"):
        load_snapshot_knowledge_coverage(
            path,
            scenario_root=SCENARIOS,
            knowledge_root=KNOWLEDGE,
        )
