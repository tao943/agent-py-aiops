"""Immutable public contracts for Docker Live benchmark scenarios."""

from __future__ import annotations

from dataclasses import dataclass

from super_ai.evaluation.domain import PublicHypothesis


@dataclass(frozen=True, slots=True)
class LiveScenario:
    """Answer-free Live scenario visible to the diagnostic Agent."""

    id: str
    title: str
    symptom_family: str
    difficulty: str
    modes: tuple[str, ...]
    driver: str
    alert: dict[str, object]
    hypotheses: tuple[PublicHypothesis, ...]


@dataclass(frozen=True, slots=True)
class LiveRunIdentity:
    """Validated run identity used to scope synthetic database resources."""

    run_id: str
    run_token: str

    @property
    def blocker_application_name(self) -> str:
        return f"agentpy-live:{self.run_id}:blocker"

    @property
    def waiter_application_name(self) -> str:
        return f"agentpy-live:{self.run_id}:waiter"

    @property
    def table_name(self) -> str:
        return f"lock_target_{self.run_token}"
