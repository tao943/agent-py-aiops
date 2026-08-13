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


@dataclass(frozen=True, slots=True)
class LiveFaultObservation:
    """Safe facts proving whether the synthetic fault exists."""

    blocker_pid: int
    waiter_pid: int
    waiter_has_lock_event: bool
    blocker_edge_confirmed: bool

    @property
    def confirmed(self) -> bool:
        return self.waiter_has_lock_event and self.blocker_edge_confirmed


@dataclass(frozen=True, slots=True)
class LiveRecoveryRecord:
    """Auditable result of the bounded recovery boundary."""

    action: str
    target_pid: int
    authorized: bool
    executed: bool
    authorization_code: str


@dataclass(frozen=True, slots=True)
class LiveVerification:
    """Independent post-recovery checks."""

    blocker_gone: bool
    waiter_unblocked: bool
    lock_graph_clear: bool
    probe_succeeded: bool
    postgres_healthy: bool
    unrelated_sessions_untouched: bool

    @property
    def passed(self) -> bool:
        return all(
            (
                self.blocker_gone,
                self.waiter_unblocked,
                self.lock_graph_clear,
                self.probe_succeeded,
                self.postgres_healthy,
                self.unrelated_sessions_untouched,
            )
        )
