"""Owner-scoped creation of immutable production recovery intents."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import cast
from uuid import uuid4

from super_ai.alert_ingestion.repositories import AlertIncidentQueryRepository
from super_ai.memory.repositories import (
    DiagnosticEvidenceRecord,
    DiagnosticMemoryRepository,
    DiagnosticReportRecord,
)
from super_ai.recovery.config import ProductionRecoverySettings
from super_ai.recovery.contracts import RecoveryIntentRecord, proposal_fingerprint
from super_ai.recovery.policy import RecoveryCreationFacts, RecoveryPolicy
from super_ai.recovery.proposal_adapter import (
    RecoveryProposalAdapter,
    ValidatedDiagnosticDecision,
)
from super_ai.recovery.repository import (
    RecoveryIntentCreate,
    RecoveryIntentCreateResult,
    RecoveryIntentRepository,
)


class RecoveryIntentNotEligible(LookupError):
    """The owner-scoped diagnostic cannot produce a governed intent."""


class RecoveryIntentService:
    def __init__(
        self,
        *,
        diagnostics: DiagnosticMemoryRepository,
        incidents: AlertIncidentQueryRepository,
        intents: RecoveryIntentRepository,
        settings: ProductionRecoverySettings,
        adapter: RecoveryProposalAdapter | None = None,
        policy: RecoveryPolicy | None = None,
        now: Callable[[], datetime] | None = None,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._diagnostics = diagnostics
        self._incidents = incidents
        self._intents = intents
        self._settings = settings
        self._adapter = adapter or RecoveryProposalAdapter()
        self._policy = policy or RecoveryPolicy()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._id_factory: Callable[[str], str] = id_factory or _new_public_id

    async def create(
        self,
        *,
        owner_user_id: str,
        diagnostic_task_id: str,
        note: str | None,
    ) -> RecoveryIntentRecord:
        return (
            await self.create_result(
                owner_user_id=owner_user_id,
                diagnostic_task_id=diagnostic_task_id,
                note=note,
            )
        ).intent

    async def create_result(
        self,
        *,
        owner_user_id: str,
        diagnostic_task_id: str,
        note: str | None,
    ) -> RecoveryIntentCreateResult:
        del note  # Human prose is intentionally outside the trusted proposal identity.
        task = await self._diagnostics.get_task(
            owner_user_id=owner_user_id,
            task_id=diagnostic_task_id,
        )
        if task is None:
            raise RecoveryIntentNotEligible("recovery_not_eligible")
        incident = await self._incidents.get_by_diagnostic_task(
            owner_user_id=owner_user_id,
            diagnostic_task_id=diagnostic_task_id,
        )
        reports = await self._diagnostics.list_reports(
            owner_user_id=owner_user_id,
            task_id=diagnostic_task_id,
        )
        if incident is None or not reports:
            raise RecoveryIntentNotEligible("recovery_not_eligible")
        report = reports[0]
        evidence = await self._linked_evidence(
            owner_user_id=owner_user_id,
            diagnostic_task_id=diagnostic_task_id,
            report=report,
        )
        decision = validated_diagnostic_decision(report.payload)
        proposal = (
            self._adapter.resolve(decision, evidence, self._settings)
            if decision is not None
            else None
        )
        creation = self._policy.evaluate_creation(
            RecoveryCreationFacts(
                diagnostic_succeeded=(
                    task.status == "succeeded"
                    and report.payload.get("status") == "succeeded"
                ),
                report_available=True,
                incident_active=incident.status == "active",
                evidence_sufficient=bool(
                    decision is not None and decision.evidence_sufficient
                ),
                deterministic_validation_passed=bool(
                    decision is not None and decision.deterministic_checks_passed
                ),
                proposal=proposal,
            ),
            self._settings,
        )
        if proposal is None:
            raise RecoveryIntentNotEligible(
                creation.safe_reason_code or "recovery_not_eligible"
            )
        fingerprint = proposal_fingerprint(
            owner_user_id=owner_user_id,
            incident_id=incident.id,
            diagnostic_task_id=diagnostic_task_id,
            report_id=report.id,
            action=proposal.action,
            target_key=proposal.target_key,
            canonical_arguments=proposal.canonical_arguments,
            evidence_ids=proposal.evidence_ids,
        )
        authorization_code = (
            creation.safe_reason_code
            or (
                "automatic_compose_authorized"
                if creation.next_status == "queued"
                else "owner_approval_required"
            )
        )
        request = RecoveryIntentCreate(
            id=self._id_factory("intent"),
            owner_user_id=owner_user_id,
            incident_id=incident.id,
            diagnostic_task_id=diagnostic_task_id,
            report_id=report.id,
            action=proposal.action,
            target_key=proposal.target_key,
            canonical_arguments=proposal.canonical_arguments,
            proposal_fingerprint=fingerprint,
            evidence_ids=proposal.evidence_ids,
            validator_origin=proposal.validator_origin,
            policy_authorization_code=authorization_code,
            risk_tier=("high" if proposal.action == "terminate_postgres_blocker" else "low"),
            automatic_eligible=creation.automatic_eligible,
            approval_required=creation.approval_required,
            status=creation.next_status,
            trusted_snapshot=proposal.trusted_snapshot,
        )
        return await self._intents.create_intent_with_job_and_event(
            request,
            background_job_id=(
                self._id_factory("job") if creation.next_status == "queued" else None
            ),
            event_id=self._id_factory("event"),
            now=self._now(),
        )

    async def _linked_evidence(
        self,
        *,
        owner_user_id: str,
        diagnostic_task_id: str,
        report: DiagnosticReportRecord,
    ) -> tuple[DiagnosticEvidenceRecord, ...]:
        links = await self._diagnostics.list_report_evidence_links(
            owner_user_id=owner_user_id,
            task_id=diagnostic_task_id,
        )
        linked_ids = {
            item.evidence_id for item in links if item.report_id == report.id
        }
        evidence = await self._diagnostics.list_evidence(
            owner_user_id=owner_user_id,
            task_id=diagnostic_task_id,
        )
        return tuple(item for item in evidence if item.id in linked_ids)


def validated_diagnostic_decision(
    payload: Mapping[str, object],
) -> ValidatedDiagnosticDecision | None:
    root = _mapping(payload.get("rootCauseDecision"))
    validation = _mapping(payload.get("decisionValidation"))
    if root is None or validation is None or validation.get("status") != "valid":
        return None
    component = root.get("component")
    mechanism = root.get("mechanism")
    origin = validation.get("validationOrigin")
    evidence_ids = _string_sequence(root.get("evidenceIds"))
    checks = _mapping_sequence(validation.get("deterministicChecks"))
    checks_passed = bool(checks and all(item.get("passed") is True for item in checks))
    if not all(isinstance(item, str) and item for item in (component, mechanism, origin)):
        return None
    return ValidatedDiagnosticDecision(
        component=cast(str, component),
        mechanism=cast(str, mechanism),
        evidence_ids=evidence_ids,
        validator_origin=cast(str, origin),
        evidence_sufficient=_evidence_sufficient(payload.get("evidenceSufficiency")),
        deterministic_checks_passed=checks_passed,
    )


def _evidence_sufficient(value: object) -> bool:
    if value == "sufficient":
        return True
    mapping = _mapping(value)
    return mapping is not None and mapping.get("status") == "sufficient"


def _mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    mapping = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        return None
    return cast(Mapping[str, object], mapping)


def _mapping_sequence(value: object) -> tuple[Mapping[str, object], ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return ()
    result: list[Mapping[str, object]] = []
    for item in cast(Sequence[object], value):
        mapping = _mapping(item)
        if mapping is None:
            return ()
        result.append(mapping)
    return tuple(result)


def _string_sequence(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return ()
    items = tuple(cast(Sequence[object], value))
    if not all(isinstance(item, str) and item for item in items):
        return ()
    return tuple(dict.fromkeys(cast(tuple[str, ...], items)))


def _new_public_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"
