"""Durable, at-most-once orchestration for governed production recovery."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, cast
from uuid import uuid4

from super_ai.aiops.execution import (
    ExecutionCoordinator,
    ExecutionIdentity,
    ExecutionResult,
    UnsafeExecutionReplay,
)
from super_ai.alert_ingestion.repositories import AlertIncidentQueryRepository
from super_ai.jobs import BackgroundJobContext
from super_ai.memory.repositories import (
    AiopsExecutionRepository,
    DiagnosticMemoryRepository,
    JsonDict,
    MemoryRepositories,
)
from super_ai.recovery.compose import (
    AsyncioArgvRunner,
    ComposeContainerIdentity,
    ComposeRecoveryExecutor,
    ComposeRecoveryVerifier,
    HttpxRecoveryProbe,
)
from super_ai.recovery.config import (
    ProductionRecoverySettings,
    load_production_recovery_settings,
)
from super_ai.recovery.contracts import (
    RecoveryCheck,
    RecoveryExecutionResult,
    RecoveryIntentRecord,
    RecoveryStatus,
    RecoveryVerificationResult,
    proposal_fingerprint,
)
from super_ai.recovery.intent_service import validated_diagnostic_decision
from super_ai.recovery.policy import RecoveryExecutionFacts, RecoveryPolicy
from super_ai.recovery.postgres import (
    PostgresBlockerRelation,
    PostgresPreflightExpectation,
    PostgresRecoveryExecutor,
    SQLAlchemyPostgresRecoveryAdapter,
)
from super_ai.recovery.proposal_adapter import RecoveryProposalAdapter
from super_ai.recovery.repository import (
    RecoveryApprovalRecord,
    RecoveryIntentRepository,
    RecoveryStateConflict,
)

_GRAPH_VERSION = "production-recovery-v1"
_TERMINAL = frozenset(
    {
        "recovered",
        "denied",
        "rejected",
        "expired",
        "cancelled",
        "verification_failed",
        "manual_intervention",
    }
)


@dataclass(frozen=True, slots=True)
class RecoveryAuthorization:
    allowed: bool
    proposal_fingerprint: str
    safe_reason_code: str | None


@dataclass(frozen=True, slots=True)
class RecoveryDriverPreflight:
    allowed: bool
    verification_context: JsonDict
    safe_reason_code: str | None


class RecoveryAuthorizer(Protocol):
    async def authorize(
        self,
        intent: RecoveryIntentRecord,
        approval: RecoveryApprovalRecord | None,
    ) -> RecoveryAuthorization: ...


class RecoveryDriver(Protocol):
    async def preflight(
        self,
        intent: RecoveryIntentRecord,
        approval: RecoveryApprovalRecord | None,
    ) -> RecoveryDriverPreflight: ...

    async def execute(self, context: JsonDict) -> RecoveryExecutionResult: ...

    async def verify(self, context: JsonDict) -> RecoveryVerificationResult: ...


class RecoveryExecutionCoordinator(Protocol):
    async def run_once(
        self,
        identity: ExecutionIdentity,
        operation: Callable[[], Awaitable[JsonDict]],
        *,
        outcome_known_on_error: bool = True,
    ) -> ExecutionResult: ...


DriverFactory = Callable[[RecoveryIntentRecord], RecoveryDriver]
CoordinatorFactory = Callable[[RecoveryIntentRecord], RecoveryExecutionCoordinator]
SettingsLoader = Callable[[], ProductionRecoverySettings]


class _UnknownRecoveryOutcome(RuntimeError):
    pass


class _MissingCompletedExecution(RuntimeError):
    pass


class ProductionRecoveryWorker:
    """Move one RecoveryIntent through its durable, restart-safe state machine."""

    def __init__(
        self,
        *,
        intents: RecoveryIntentRepository,
        authorizer: RecoveryAuthorizer,
        driver_factory: DriverFactory,
        coordinator_factory: CoordinatorFactory,
        now: Callable[[], datetime] | None = None,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        self._intents = intents
        self._authorizer = authorizer
        self._driver_factory = driver_factory
        self._coordinator_factory = coordinator_factory
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or _new_id

    async def handle(self, context: BackgroundJobContext) -> None:
        owner_user_id = context.job.owner_user_id
        intent_id = context.job.resource_id
        intent = await self._intents.get_owned(
            owner_user_id=owner_user_id,
            intent_id=intent_id,
        )
        if intent is None:
            return
        if intent.status in _TERMINAL:
            return
        driver = self._driver_factory(intent)
        coordinator = self._coordinator_factory(intent)

        if intent.status == "verifying":
            result = await self._completed_execution_or_manual(intent, coordinator, context)
            if result is not None:
                await self._verify(intent, driver, result.output, context)
            return

        if intent.status == "executing":
            result = await self._completed_execution_or_manual(intent, coordinator, context)
            if result is None:
                return
            intent = await self._transition(
                intent,
                expected=("executing",),
                to_status="verifying",
                event_type="execution.completed",
                safe_summary=_safe_output_summary(result.output),
                duration_ms=_safe_duration(result.output),
                execution_summary=_safe_output_summary(result.output),
                context=context,
            )
            await self._verify(intent, driver, result.output, context)
            return

        if intent.status == "queued":
            intent = await self._transition(
                intent,
                expected=("queued",),
                to_status="revalidating",
                event_type="execution.revalidating",
                safe_summary="Recovery authorization is being revalidated.",
                context=context,
            )
        if intent.status != "revalidating":
            return

        approval = await self._intents.get_current_approval(
            owner_user_id=intent.owner_user_id,
            intent_id=intent.id,
        )
        authorization = await self._authorizer.authorize(intent, approval)
        if (
            not authorization.allowed
            or authorization.proposal_fingerprint != intent.proposal_fingerprint
        ):
            await self._manual(
                intent,
                authorization.safe_reason_code or "proposal_changed",
                "Recovery authorization changed before execution.",
                context=context,
            )
            return
        preflight = await driver.preflight(intent, approval)
        if not preflight.allowed:
            await self._manual(
                intent,
                preflight.safe_reason_code or "recovery_preflight_denied",
                "Recovery preflight did not authorize execution.",
                context=context,
            )
            return

        await context.raise_if_cancelled()
        identity = _execution_identity(intent)
        executing_intent = intent

        async def operation() -> JsonDict:
            nonlocal executing_intent
            executing_intent = await self._transition(
                executing_intent,
                expected=("revalidating",),
                to_status="executing",
                event_type="execution.claimed",
                safe_summary="Recovery execution claim acquired.",
                execution_key=identity.execution_key,
                context=context,
            )
            execution = await driver.execute(preflight.verification_context)
            if not execution.outcome_known:
                raise _UnknownRecoveryOutcome("recovery_outcome_unknown")
            return _execution_output(execution, preflight.verification_context)

        try:
            result = await coordinator.run_once(
                identity,
                operation,
                outcome_known_on_error=False,
            )
        except (UnsafeExecutionReplay, _UnknownRecoveryOutcome):
            current = await self._reload(executing_intent)
            if current.status not in _TERMINAL:
                await self._manual(
                    current,
                    "recovery_outcome_unknown",
                    "Recovery outcome is unknown and requires manual intervention.",
                    context=context,
                )
            return
        if not _safe_output_succeeded(result.output):
            await self._manual(
                await self._reload(intent),
                "recovery_execution_failed",
                _safe_output_summary(result.output),
                duration_ms=_safe_duration(result.output),
                context=context,
            )
            return
        current = await self._reload(executing_intent)
        if current.status == "executing":
            current = await self._transition(
                current,
                expected=("executing",),
                to_status="verifying",
                event_type="execution.completed",
                safe_summary=_safe_output_summary(result.output),
                duration_ms=_safe_duration(result.output),
                execution_summary=_safe_output_summary(result.output),
                context=context,
            )
        if current.status == "verifying":
            await self._verify(current, driver, result.output, context)

    async def _completed_execution_or_manual(
        self,
        intent: RecoveryIntentRecord,
        coordinator: RecoveryExecutionCoordinator,
        context: BackgroundJobContext,
    ) -> ExecutionResult | None:
        async def missing() -> JsonDict:
            raise _MissingCompletedExecution("completed_execution_missing")

        try:
            return await coordinator.run_once(
                _execution_identity(intent),
                missing,
                outcome_known_on_error=False,
            )
        except (UnsafeExecutionReplay, _MissingCompletedExecution):
            await self._manual(
                await self._reload(intent),
                "recovery_outcome_unknown",
                "Recovery outcome is unknown and requires manual intervention.",
                context=context,
            )
            return None

    async def _verify(
        self,
        intent: RecoveryIntentRecord,
        driver: RecoveryDriver,
        output: Mapping[str, object],
        context: BackgroundJobContext,
    ) -> None:
        raw_context = output.get("verificationContext")
        if not isinstance(raw_context, dict):
            await self._manual(
                intent,
                "verification_context_missing",
                "Recovery verification context is unavailable.",
                context=context,
            )
            return
        verification = await driver.verify(cast(JsonDict, raw_context))
        await self._transition(
            intent,
            expected=("verifying",),
            to_status="recovered" if verification.passed else "verification_failed",
            event_type=("verification.passed" if verification.passed else "verification.failed"),
            safe_reason_code=(None if verification.passed else verification.safe_summary),
            safe_summary=verification.safe_summary,
            verification_checks=verification.checks,
            context=context,
        )

    async def _manual(
        self,
        intent: RecoveryIntentRecord,
        reason: str,
        summary: str,
        *,
        duration_ms: int | None = None,
        context: BackgroundJobContext,
    ) -> None:
        if intent.status in _TERMINAL:
            return
        await self._transition(
            intent,
            expected=(intent.status,),
            to_status="manual_intervention",
            event_type="execution.manual_intervention",
            safe_reason_code=reason,
            safe_summary=summary,
            duration_ms=duration_ms,
            context=context,
        )

    async def _reload(self, intent: RecoveryIntentRecord) -> RecoveryIntentRecord:
        current = await self._intents.get_owned(
            owner_user_id=intent.owner_user_id,
            intent_id=intent.id,
        )
        return current or intent

    async def _transition(
        self,
        intent: RecoveryIntentRecord,
        *,
        expected: tuple[RecoveryStatus, ...],
        to_status: RecoveryStatus,
        event_type: str,
        safe_summary: str,
        safe_reason_code: str | None = None,
        duration_ms: int | None = None,
        execution_key: str | None = None,
        execution_summary: str | None = None,
        verification_checks: tuple[RecoveryCheck, ...] | None = None,
        context: BackgroundJobContext,
    ) -> RecoveryIntentRecord:
        try:
            updated = await self._intents.transition(
                owner_user_id=intent.owner_user_id,
                intent_id=intent.id,
                expected_statuses=expected,
                to_status=to_status,
                event_id=self._id_factory("event"),
                event_type=event_type,
                safe_reason_code=safe_reason_code,
                safe_summary=safe_summary,
                now=self._now(),
                duration_ms=duration_ms,
                execution_key=execution_key,
                execution_summary=execution_summary,
                verification_checks=verification_checks,
            )
        except RecoveryStateConflict:
            current = await self._reload(intent)
            if current.status == to_status or current.status in _TERMINAL:
                return current
            raise
        if updated is None:
            raise LookupError("recovery_intent_unavailable")
        await context.append_event(
            {
                "type": "recovery.transition",
                "intentId": updated.id,
                "status": updated.status,
                "eventType": event_type,
            }
        )
        return updated


class GroundedRecoveryAuthorizer:
    """Rebuild authorization exclusively from current owner-scoped persisted facts."""

    def __init__(
        self,
        *,
        diagnostics: DiagnosticMemoryRepository,
        incidents: AlertIncidentQueryRepository,
        settings_loader: SettingsLoader,
        policy: RecoveryPolicy | None = None,
        adapter: RecoveryProposalAdapter | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._diagnostics = diagnostics
        self._incidents = incidents
        self._settings_loader = settings_loader
        self._policy = policy or RecoveryPolicy()
        self._adapter = adapter or RecoveryProposalAdapter()
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def authorize(
        self,
        intent: RecoveryIntentRecord,
        approval: RecoveryApprovalRecord | None,
    ) -> RecoveryAuthorization:
        incident = await self._incidents.get_owned(
            owner_user_id=intent.owner_user_id,
            incident_id=intent.incident_id,
        )
        task = await self._diagnostics.get_task(
            owner_user_id=intent.owner_user_id,
            task_id=intent.diagnostic_task_id,
        )
        report = await self._diagnostics.get_report(
            owner_user_id=intent.owner_user_id,
            report_id=intent.report_id,
        )
        if (
            incident is None
            or task is None
            or report is None
            or report.task_id != intent.diagnostic_task_id
            or task.status != "succeeded"
            or report.payload.get("status") != "succeeded"
        ):
            return RecoveryAuthorization(False, "", "diagnostic_or_incident_changed")
        links = await self._diagnostics.list_report_evidence_links(
            owner_user_id=intent.owner_user_id,
            task_id=intent.diagnostic_task_id,
        )
        linked_ids = {item.evidence_id for item in links if item.report_id == intent.report_id}
        evidence = await self._diagnostics.list_evidence(
            owner_user_id=intent.owner_user_id,
            task_id=intent.diagnostic_task_id,
        )
        selected = tuple(item for item in evidence if item.id in linked_ids)
        decision = validated_diagnostic_decision(report.payload)
        settings = self._settings_loader()
        proposal = (
            self._adapter.resolve(decision, selected, settings) if decision is not None else None
        )
        if proposal is None:
            return RecoveryAuthorization(False, "", "proposal_not_grounded")
        fingerprint = proposal_fingerprint(
            owner_user_id=intent.owner_user_id,
            incident_id=intent.incident_id,
            diagnostic_task_id=intent.diagnostic_task_id,
            report_id=intent.report_id,
            action=proposal.action,
            target_key=proposal.target_key,
            canonical_arguments=proposal.canonical_arguments,
            evidence_ids=proposal.evidence_ids,
        )
        decision_result = self._policy.evaluate_execution(
            RecoveryExecutionFacts(
                incident_active=incident.status == "active",
                proposal_fingerprint=fingerprint,
            ),
            intent,
            settings,
            approval,
            now=self._now(),
        )
        unchanged = (
            proposal.action == intent.action
            and proposal.target_key == intent.target_key
            and proposal.evidence_ids == intent.evidence_ids
            and proposal.canonical_arguments == dict(intent.canonical_arguments)
            and proposal.trusted_snapshot == dict(intent.trusted_snapshot)
        )
        return RecoveryAuthorization(
            decision_result.allowed and unchanged,
            fingerprint,
            decision_result.safe_reason_code if unchanged else "proposal_changed",
        )


class ComposeRecoveryDriver:
    def __init__(
        self,
        *,
        intent: RecoveryIntentRecord,
        executor: ComposeRecoveryExecutor,
        verifier: ComposeRecoveryVerifier,
    ) -> None:
        self._intent = intent
        self._executor = executor
        self._verifier = verifier

    async def preflight(
        self,
        intent: RecoveryIntentRecord,
        approval: RecoveryApprovalRecord | None,
    ) -> RecoveryDriverPreflight:
        del intent, approval
        result = await self._executor.preflight()
        identity = result.identity
        if not result.allowed or identity is None:
            return RecoveryDriverPreflight(False, {}, result.safe_reason_code)
        return RecoveryDriverPreflight(
            True,
            {
                "kind": "compose",
                "containerId": identity.container_id,
                "service": identity.service,
                "startedAt": identity.started_at,
                "project": identity.project,
            },
            None,
        )

    async def execute(self, context: JsonDict) -> RecoveryExecutionResult:
        identity = _compose_identity(context)
        if identity is None:
            return RecoveryExecutionResult(False, True, "compose_context_invalid", 0)
        return await self._executor.execute_once(identity)

    async def verify(self, context: JsonDict) -> RecoveryVerificationResult:
        identity = _compose_identity(context)
        if identity is None:
            return RecoveryVerificationResult(False, (), "compose_context_invalid")
        return await self._verifier.verify(
            owner_user_id=self._intent.owner_user_id,
            incident_id=self._intent.incident_id,
            before=identity,
        )


class PostgresRecoveryDriver:
    def __init__(
        self,
        *,
        intent: RecoveryIntentRecord,
        executor: PostgresRecoveryExecutor,
    ) -> None:
        self._intent = intent
        self._executor = executor
        self._approval: RecoveryApprovalRecord | None = None

    async def preflight(
        self,
        intent: RecoveryIntentRecord,
        approval: RecoveryApprovalRecord | None,
    ) -> RecoveryDriverPreflight:
        self._approval = approval
        expectation = _postgres_expectation(intent)
        if expectation is None:
            return RecoveryDriverPreflight(False, {}, "postgres_context_invalid")
        result = await self._executor.preflight(expectation)
        relation = result.relation
        if not result.allowed or relation is None:
            return RecoveryDriverPreflight(False, {}, result.safe_reason_code)
        return RecoveryDriverPreflight(
            True,
            _postgres_context(expectation, relation),
            None,
        )

    async def execute(self, context: JsonDict) -> RecoveryExecutionResult:
        parsed = _postgres_context_values(context)
        if parsed is None:
            return RecoveryExecutionResult(False, True, "postgres_context_invalid", 0)
        expectation, relation = parsed
        from super_ai.recovery.postgres import PostgresPreflightResult

        return await self._executor.execute_once(
            expectation,
            PostgresPreflightResult(True, relation, expectation.relationship_fingerprint, None),
            self._approval,
        )

    async def verify(self, context: JsonDict) -> RecoveryVerificationResult:
        parsed = _postgres_context_values(context)
        if parsed is None:
            return RecoveryVerificationResult(False, (), "postgres_context_invalid")
        expectation, relation = parsed
        return await self._executor.verify(expectation, relation)


class DeniedRecoveryDriver:
    def __init__(self, reason: str) -> None:
        self._reason = reason

    async def preflight(
        self,
        intent: RecoveryIntentRecord,
        approval: RecoveryApprovalRecord | None,
    ) -> RecoveryDriverPreflight:
        del intent, approval
        return RecoveryDriverPreflight(False, {}, self._reason)

    async def execute(self, context: JsonDict) -> RecoveryExecutionResult:
        del context
        return RecoveryExecutionResult(False, True, self._reason, 0)

    async def verify(self, context: JsonDict) -> RecoveryVerificationResult:
        del context
        return RecoveryVerificationResult(False, (), self._reason)


def _execution_identity(intent: RecoveryIntentRecord) -> ExecutionIdentity:
    return ExecutionIdentity(
        task_id=intent.id,
        graph_version=_GRAPH_VERSION,
        node_name=f"execute:{intent.action}:{intent.target_key}",
        logical_iteration=0,
        input_payload={"proposalFingerprint": intent.proposal_fingerprint},
        execution_kind="recovery",
        side_effecting=True,
    )


def _execution_output(
    result: RecoveryExecutionResult,
    verification_context: JsonDict,
) -> JsonDict:
    return {
        "succeeded": result.succeeded,
        "outcomeKnown": result.outcome_known,
        "safeSummary": result.safe_summary[:512],
        "durationMs": max(0, result.duration_ms),
        "verificationContext": verification_context,
    }


def _safe_output_succeeded(output: Mapping[str, object]) -> bool:
    return output.get("succeeded") is True and output.get("outcomeKnown") is True


def _safe_output_summary(output: Mapping[str, object]) -> str:
    value = output.get("safeSummary")
    return value[:512] if isinstance(value, str) and value else "Recovery execution completed."


def _safe_duration(output: Mapping[str, object]) -> int | None:
    value = output.get("durationMs")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def build_production_recovery_handler(
    app: Any,
) -> Callable[[BackgroundJobContext], Awaitable[None]]:
    """Compose the production handler from existing application dependencies."""

    repositories = cast(MemoryRepositories, app.state.memory_repositories)
    intents = repositories.recovery_intents
    runtime_provider = repositories.aiops_runtime
    if intents is None or runtime_provider is None:
        raise RuntimeError("Production recovery repositories are required.")
    incidents = cast(
        AlertIncidentQueryRepository,
        app.state.alert_incident_repository,
    )
    config_path = cast(str | None, app.state.project_config_path)
    session_factory = app.state.memory_session_factory

    def settings_loader() -> ProductionRecoverySettings:
        return load_production_recovery_settings(config_path)

    authorizer = GroundedRecoveryAuthorizer(
        diagnostics=repositories.diagnostics,
        incidents=incidents,
        settings_loader=settings_loader,
    )
    runner = AsyncioArgvRunner()
    probes = HttpxRecoveryProbe()

    def driver_factory(intent: RecoveryIntentRecord) -> RecoveryDriver:
        settings = settings_loader()
        compose_target = settings.compose_targets.get(intent.target_key)
        if compose_target is not None and intent.action == "restart_compose_service":
            executor = ComposeRecoveryExecutor(compose_target, runner)
            return ComposeRecoveryDriver(
                intent=intent,
                executor=executor,
                verifier=ComposeRecoveryVerifier(
                    target=compose_target,
                    inspector=executor,
                    probes=probes,
                    incidents=incidents,
                ),
            )
        postgres_target = settings.postgres_targets.get(intent.target_key)
        if (
            postgres_target is not None
            and intent.action == "terminate_postgres_blocker"
            and postgres_target.database_config_key == "backend"
        ):
            executor = PostgresRecoveryExecutor(
                target=postgres_target,
                adapter=SQLAlchemyPostgresRecoveryAdapter(session_factory),
                incidents=incidents,
            )
            return PostgresRecoveryDriver(intent=intent, executor=executor)
        return DeniedRecoveryDriver("recovery_target_unavailable")

    worker_instance_id = f"recovery-worker:{uuid4().hex}"

    def coordinator_factory(intent: RecoveryIntentRecord) -> ExecutionCoordinator:
        repository = runtime_provider.execution_repository(
            owner_user_id=intent.owner_user_id,
            task_id=intent.diagnostic_task_id,
            graph_version=_GRAPH_VERSION,
        )
        return build_execution_coordinator(
            repository,
            worker_id=worker_instance_id,
        )

    return ProductionRecoveryWorker(
        intents=intents,
        authorizer=authorizer,
        driver_factory=driver_factory,
        coordinator_factory=coordinator_factory,
    ).handle


def build_execution_coordinator(
    repository: AiopsExecutionRepository,
    *,
    worker_id: str,
    executor_timeout_seconds: int = 30,
) -> ExecutionCoordinator:
    """Build a claim whose lease always outlives the bounded executor timeout."""

    return ExecutionCoordinator(
        repository,
        worker_id=worker_id,
        lease_seconds=executor_timeout_seconds + 30,
    )


def _compose_identity(context: Mapping[str, object]) -> ComposeContainerIdentity | None:
    values = (
        context.get("containerId"),
        context.get("service"),
        context.get("startedAt"),
        context.get("project"),
    )
    if context.get("kind") != "compose" or not all(
        isinstance(value, str) and value for value in values
    ):
        return None
    return ComposeContainerIdentity(
        cast(str, values[0]),
        cast(str, values[1]),
        cast(str, values[2]),
        cast(str, values[3]),
    )


def _postgres_expectation(
    intent: RecoveryIntentRecord,
) -> PostgresPreflightExpectation | None:
    relationship = intent.trusted_snapshot.get("lockRelationshipFingerprint")
    resource = intent.trusted_snapshot.get("lockResourceKey")
    if not (
        isinstance(relationship, str)
        and len(relationship) == 64
        and isinstance(resource, str)
        and resource
    ):
        return None
    return PostgresPreflightExpectation(
        owner_user_id=intent.owner_user_id,
        incident_id=intent.incident_id,
        intent_id=intent.id,
        proposal_fingerprint=intent.proposal_fingerprint,
        relationship_fingerprint=relationship,
        logical_resource=resource,
    )


def _postgres_context(
    expectation: PostgresPreflightExpectation,
    relation: PostgresBlockerRelation,
) -> JsonDict:
    # This payload is internal execution state and is never returned by public APIs.
    return {
        "kind": "postgres",
        "ownerUserId": expectation.owner_user_id,
        "incidentId": expectation.incident_id,
        "intentId": expectation.intent_id,
        "proposalFingerprint": expectation.proposal_fingerprint,
        "relationshipFingerprint": expectation.relationship_fingerprint,
        "logicalResource": expectation.logical_resource,
        "databaseIdentity": relation.database_identity,
        "blockerPid": relation.blocker_pid,
        "waiterPid": relation.waiter_pid,
        "observerPid": relation.observer_pid,
        "blockerBackendType": relation.blocker_backend_type,
        "waiterBackendType": relation.waiter_backend_type,
        "blockerApplicationName": relation.blocker_application_name,
        "waiterApplicationName": relation.waiter_application_name,
    }


def _postgres_context_values(
    context: Mapping[str, object],
) -> tuple[PostgresPreflightExpectation, PostgresBlockerRelation] | None:
    string_keys = (
        "ownerUserId",
        "incidentId",
        "intentId",
        "proposalFingerprint",
        "relationshipFingerprint",
        "logicalResource",
        "databaseIdentity",
        "blockerBackendType",
        "waiterBackendType",
        "blockerApplicationName",
        "waiterApplicationName",
    )
    if context.get("kind") != "postgres" or not all(
        isinstance(context.get(key), str) for key in string_keys
    ):
        return None
    pid_keys = ("blockerPid", "waiterPid", "observerPid")
    if not all(
        isinstance(context.get(key), int) and not isinstance(context.get(key), bool)
        for key in pid_keys
    ):
        return None
    expectation = PostgresPreflightExpectation(
        owner_user_id=cast(str, context["ownerUserId"]),
        incident_id=cast(str, context["incidentId"]),
        intent_id=cast(str, context["intentId"]),
        proposal_fingerprint=cast(str, context["proposalFingerprint"]),
        relationship_fingerprint=cast(str, context["relationshipFingerprint"]),
        logical_resource=cast(str, context["logicalResource"]),
    )
    relation = PostgresBlockerRelation(
        database_identity=cast(str, context["databaseIdentity"]),
        logical_resource=expectation.logical_resource,
        blocker_pid=cast(int, context["blockerPid"]),
        waiter_pid=cast(int, context["waiterPid"]),
        observer_pid=cast(int, context["observerPid"]),
        blocker_backend_type=cast(str, context["blockerBackendType"]),
        waiter_backend_type=cast(str, context["waiterBackendType"]),
        blocker_application_name=cast(str, context["blockerApplicationName"]),
        waiter_application_name=cast(str, context["waiterApplicationName"]),
    )
    return expectation, relation


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"
