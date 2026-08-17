"""Compatibility exports for the shared evaluator-only semantic scorer."""

from super_ai.evaluation.semantic_scoring import (
    RootCauseSemanticScore,
    score_root_cause_semantics,
)

__all__ = ["RootCauseSemanticScore", "score_root_cause_semantics"]
