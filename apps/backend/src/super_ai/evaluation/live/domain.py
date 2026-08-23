"""Immutable public contracts for Docker Live benchmark scenarios."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

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


EvidenceSource = Literal["local", "cls"]
RecoveryExpectation = Literal["executed_recovery", "proposal_only"]


@dataclass(frozen=True, slots=True)
class LiveClsScope:
    """Run-scoped CLS query boundary prepared before Agent diagnosis."""

    region: str
    topic_id: str
    from_ms: int
    to_ms: int
    run_id: str
    scenario_id: str
    incident_id: str


@dataclass(frozen=True, slots=True)
class LiveEvidenceReadiness:
    """Non-secret audit proving that prepared evidence became searchable."""

    expected_log_count: int
    indexed_log_count: int
    attempts: int
    uploaded_at_ms: int
    searchable_at_ms: int


@dataclass(frozen=True, slots=True)
class LiveEvidenceContext:
    """Immutable evidence source and scope passed into Live diagnosis."""

    source: EvidenceSource
    incident_id: str
    cls_scope: LiveClsScope | None = None
    readiness: LiveEvidenceReadiness | None = None
    verified_events: tuple[str, ...] = ()

    @classmethod
    def local(cls, *, incident_id: str) -> LiveEvidenceContext:
        return cls(source="local", incident_id=incident_id)


class LiveInfrastructureError(RuntimeError):
    """Classified infrastructure invalidity with a credential-safe message."""

    def __init__(self, category: str) -> None:
        super().__init__("Live evidence infrastructure failed at a classified boundary.")
        self.category = category


@dataclass(frozen=True, slots=True)
class LiveCheck:
    """One named, auditable lifecycle assertion."""

    name: str
    passed: bool
    source: str = "driver"


@dataclass(frozen=True, slots=True)
class LiveFaultObservation:
    """Scenario-neutral safe facts proving whether the synthetic fault exists."""

    scenario_id: str
    checks: tuple[LiveCheck, ...]
    safe_facts: tuple[tuple[str, str | int | float | bool], ...] = ()

    @property
    def confirmed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    def check_passed(self, name: str) -> bool:
        """Return one named check without relying on scenario-specific fields."""
        return any(check.name == name and check.passed for check in self.checks)

    def safe_fact(self, name: str) -> str | int | float | bool | None:
        """Return one explicitly safe fact exposed by the driver."""
        return next((value for key, value in self.safe_facts if key == name), None)


@dataclass(frozen=True, slots=True)
class LiveRecoveryRecord:
    """Auditable result of the bounded recovery boundary."""

    action: str
    target_ref: str
    expectation: RecoveryExpectation
    authorized: bool
    executed: bool
    authorization_code: str
    proposal_checks: tuple[LiveCheck, ...] = ()


@dataclass(frozen=True, slots=True)
class LiveVerification:
    """Scenario-neutral independent post-recovery checks."""

    checks: tuple[LiveCheck, ...]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    def check_passed(self, name: str) -> bool:
        return any(check.name == name and check.passed for check in self.checks)


@dataclass(frozen=True, slots=True)
class LiveCleanupResult:
    """Fixture cleanup audit kept separate from Agent recovery credit."""

    checks: tuple[LiveCheck, ...]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)
