"""Live policy adapter for the shared evaluator-only semantic scorer."""

from super_ai.aiops import RootCauseDecision
from super_ai.evaluation.domain import ScenarioOracle
from super_ai.evaluation.semantic_scoring import (
    RootCauseSemanticScore,
)
from super_ai.evaluation.semantic_scoring import (
    score_root_cause_semantics as _score_root_cause_semantics,
)


def score_root_cause_semantics(
    decision: RootCauseDecision | None,
    oracle: ScenarioOracle,
) -> RootCauseSemanticScore:
    """Score Live milestones independently of narration order."""
    return _score_root_cause_semantics(
        decision,
        oracle,
        ordered_milestones=False,
    )

__all__ = ["RootCauseSemanticScore", "score_root_cause_semantics"]
