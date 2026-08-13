"""Pure recovery planning and authorization for Docker Live PostgreSQL evals."""

from __future__ import annotations

from dataclasses import dataclass

from super_ai.aiops import RootCauseDecision
from super_ai.evaluation.live.domain import LiveRunIdentity


@dataclass(frozen=True, slots=True)
class LiveRecoveryIntent:
    """A bounded action proposed after a structured Agent decision."""

    action: str
    target_pid: int
    reason_code: str


@dataclass(frozen=True, slots=True)
class PostgresSessionState:
    """Fresh database facts used by the recovery policy."""

    pid: int
    database: str
    application_name: str
    backend_type: str
    blocked_waiter_pids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class RecoveryAuthorization:
    """Auditable recovery policy result."""

    allowed: bool
    code: str


class PostgresRecoveryPlanner:
    """Convert a supported lock diagnosis and unique live blocker into an intent."""

    def plan(
        self,
        *,
        decision: RootCauseDecision | None,
        blocker_pids: tuple[int, ...],
    ) -> LiveRecoveryIntent | None:
        if (
            decision is None
            or decision.component != "postgresql"
            or decision.mechanism != "row_lock_blocking"
            or len(blocker_pids) != 1
        ):
            return None
        return LiveRecoveryIntent(
            action="terminate_postgres_backend",
            target_pid=blocker_pids[0],
            reason_code="unique_live_blocker_for_agent_diagnosis",
        )


class PostgresRecoveryPolicy:
    """Authorize only the current run's revalidated synthetic blocker."""

    def authorize(
        self,
        *,
        identity: LiveRunIdentity,
        intent: LiveRecoveryIntent,
        state: PostgresSessionState | None,
        injected_blocker_pid: int,
        waiter_pid: int,
        executor_pid: int,
    ) -> RecoveryAuthorization:
        if intent.action != "terminate_postgres_backend":
            return RecoveryAuthorization(False, "action_not_allowed")
        if state is None or state.pid != intent.target_pid:
            return RecoveryAuthorization(False, "target_missing")
        if state.database != "agent_py_live_eval":
            return RecoveryAuthorization(False, "wrong_database")
        if state.application_name == f"agentpy-live:{identity.run_id}:blocker":
            pass
        elif state.application_name.startswith("agentpy-live:"):
            return RecoveryAuthorization(False, "cross_run_target")
        else:
            return RecoveryAuthorization(False, "wrong_application")
        if state.pid == executor_pid:
            return RecoveryAuthorization(False, "executor_target")
        if state.pid == waiter_pid:
            return RecoveryAuthorization(False, "waiter_target")
        if state.backend_type != "client backend":
            return RecoveryAuthorization(False, "system_backend")
        if waiter_pid not in state.blocked_waiter_pids:
            return RecoveryAuthorization(False, "blocking_edge_missing")
        if state.pid != injected_blocker_pid:
            return RecoveryAuthorization(False, "injected_pid_mismatch")
        return RecoveryAuthorization(True, "authorized")
