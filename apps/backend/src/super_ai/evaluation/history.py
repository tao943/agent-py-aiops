"""Safe, versioned contracts for persisted evaluation run artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, cast

from super_ai.memory.repositories import JsonDict

EvaluationKind = Literal["snapshot", "retrieval", "live"]
EvaluationStatus = Literal[
    "running",
    "passed",
    "failed",
    "agent_failed",
    "infra_invalid",
    "interrupted",
]
EvaluationProvenance = Literal["native", "imported", "reconstructed"]

ARTIFACT_SCHEMA_VERSION = "v1"

_KINDS = frozenset({"snapshot", "retrieval", "live"})
_STATUSES = frozenset(
    {"running", "passed", "failed", "agent_failed", "infra_invalid", "interrupted"}
)
_PROVENANCE = frozenset({"native", "imported", "reconstructed"})
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_FORBIDDEN_KEY_TOKENS = frozenset(
    {
        "apikey",
        "accesskey",
        "secret",
        "secretkey",
        "password",
        "token",
        "oracle",
        "groundtruth",
        "primarycause",
        "answerkey",
        "prompt",
        "chainofthought",
    }
)
_METADATA_KEYS: dict[EvaluationKind, frozenset[str]] = {
    "snapshot": frozenset(
        {
            "gitSha",
            "workflowVersion",
            "modelConfiguration",
            "ragMode",
            "acceptanceCampaignId",
            "investigationStrategy",
            "investigationPolicyVersion",
            "importSource",
        }
    ),
    "retrieval": frozenset(
        {
            "gitSha",
            "workflowVersion",
            "modelConfiguration",
            "datasetChecksum",
            "knowledgeBaseId",
            "ownerUserId",
            "importSource",
        }
    ),
    "live": frozenset(
        {
            "gitSha",
            "workflowVersion",
            "modelConfiguration",
            "evidenceSource",
            "acceptanceCampaignId",
            "investigationStrategy",
            "investigationPolicyVersion",
            "importSource",
        }
    ),
}
_METRIC_KEYS: dict[EvaluationKind, frozenset[str]] = {
    "snapshot": frozenset(
        {
            "outcome",
            "diagnosis",
            "evidence",
            "process",
            "safety",
            "efficiency",
            "total",
            "rawTotal",
            "rootCauseTop1Correct",
            "evidenceRecallBasisPoints",
            "durationMs",
            "modelCallCount",
            "duplicateEvidenceBasisPoints",
            "fallbackReason",
            "securityHardGatePassed",
        }
    ),
    "retrieval": frozenset(
        {
            "queryCount",
            "answerableQueryCount",
            "noAnswerProbeCount",
            "recallAt1",
            "recallAt3",
            "mrr",
            "forbiddenTopOneRate",
            "citationCompletenessRate",
            "vectorChannelCoverageRate",
            "bm25ChannelCoverageRate",
            "hybridChannelCoverageRate",
        }
    ),
    "live": frozenset(
        {
            "total",
            "rawTotal",
            "verificationPassed",
            "cleanupSucceeded",
            "rootCauseTop1Correct",
            "evidenceRecallBasisPoints",
            "durationMs",
            "modelCallCount",
            "duplicateEvidenceBasisPoints",
            "fallbackReason",
            "securityHardGatePassed",
        }
    ),
}
_RESULT_KEYS: dict[EvaluationKind, frozenset[str]] = {
    "snapshot": frozenset({"failures", "scoreReasons", "hardGate"}),
    "retrieval": frozenset({"failures", "queryResults"}),
    "live": frozenset(
        {
            "failures",
            "hardGate",
            "failureStage",
            "authorizationCode",
            "missingResultArtifact",
        }
    ),
}


@dataclass(frozen=True, slots=True)
class EvaluationRunEnvelope:
    """One safe evaluation run lifecycle snapshot."""

    artifact_schema_version: str
    run_id: str
    evaluation_kind: EvaluationKind
    scenario_id: str
    suite_version: str
    status: EvaluationStatus
    validity: str | None
    passed: bool | None
    metrics: JsonDict
    result_payload: JsonDict
    metadata: JsonDict
    provenance: EvaluationProvenance
    diagnostic_task_id: str | None
    failure_category: str | None
    created_at: datetime
    started_at: datetime
    completed_at: datetime | None

    def to_json(self) -> JsonDict:
        """Return the stable public JSON shape used by archive and database adapters."""
        return {
            "artifactSchemaVersion": self.artifact_schema_version,
            "runId": self.run_id,
            "evaluationKind": self.evaluation_kind,
            "scenarioId": self.scenario_id,
            "suiteVersion": self.suite_version,
            "status": self.status,
            "validity": self.validity,
            "passed": self.passed,
            "metrics": _json_copy(self.metrics),
            "resultPayload": _json_copy(self.result_payload),
            "metadata": _json_copy(self.metadata),
            "provenance": self.provenance,
            "diagnosticTaskId": self.diagnostic_task_id,
            "failureCategory": self.failure_category,
            "createdAt": _format_datetime(self.created_at),
            "startedAt": _format_datetime(self.started_at),
            "completedAt": (
                _format_datetime(self.completed_at) if self.completed_at is not None else None
            ),
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, object]) -> EvaluationRunEnvelope:
        """Parse and revalidate one untrusted archived JSON object."""
        expected = {
            "artifactSchemaVersion",
            "runId",
            "evaluationKind",
            "scenarioId",
            "suiteVersion",
            "status",
            "validity",
            "passed",
            "metrics",
            "resultPayload",
            "metadata",
            "provenance",
            "diagnosticTaskId",
            "failureCategory",
            "createdAt",
            "startedAt",
            "completedAt",
        }
        if set(payload) != expected:
            raise ValueError("Evaluation artifact contains unknown or missing fields.")
        kind = _required_literal(payload, "evaluationKind", _KINDS)
        status = _required_literal(payload, "status", _STATUSES)
        provenance = _required_literal(payload, "provenance", _PROVENANCE)
        metrics = _required_json_dict(payload, "metrics")
        result_payload = _required_json_dict(payload, "resultPayload")
        metadata = _required_json_dict(payload, "metadata")
        envelope = cls(
            artifact_schema_version=_required_text(payload, "artifactSchemaVersion"),
            run_id=_required_text(payload, "runId"),
            evaluation_kind=cast(EvaluationKind, kind),
            scenario_id=_required_text(payload, "scenarioId"),
            suite_version=_required_text(payload, "suiteVersion"),
            status=cast(EvaluationStatus, status),
            validity=_optional_text(payload, "validity"),
            passed=_optional_bool(payload, "passed"),
            metrics=metrics,
            result_payload=result_payload,
            metadata=metadata,
            provenance=cast(EvaluationProvenance, provenance),
            diagnostic_task_id=_optional_text(payload, "diagnosticTaskId"),
            failure_category=_optional_text(payload, "failureCategory"),
            created_at=_required_datetime(payload, "createdAt"),
            started_at=_required_datetime(payload, "startedAt"),
            completed_at=_optional_datetime(payload, "completedAt"),
        )
        _validate_envelope(envelope)
        return envelope


def running_envelope(
    *,
    run_id: str,
    evaluation_kind: EvaluationKind,
    scenario_id: str,
    suite_version: str,
    metadata: Mapping[str, object],
    created_at: datetime,
    started_at: datetime,
    provenance: EvaluationProvenance = "native",
) -> EvaluationRunEnvelope:
    """Create a validated running record before external evaluation work begins."""
    envelope = EvaluationRunEnvelope(
        artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
        run_id=run_id,
        evaluation_kind=evaluation_kind,
        scenario_id=scenario_id,
        suite_version=suite_version,
        status="running",
        validity=None,
        passed=None,
        metrics={},
        result_payload={},
        metadata=_mapping_copy(metadata),
        provenance=provenance,
        diagnostic_task_id=None,
        failure_category=None,
        created_at=created_at,
        started_at=started_at,
        completed_at=None,
    )
    _validate_envelope(envelope)
    return envelope


def running_from_terminal(envelope: EvaluationRunEnvelope) -> EvaluationRunEnvelope:
    """Recreate the stable running identity needed for idempotent database recovery."""
    return running_envelope(
        run_id=envelope.run_id,
        evaluation_kind=envelope.evaluation_kind,
        scenario_id=envelope.scenario_id,
        suite_version=envelope.suite_version,
        metadata=envelope.metadata,
        created_at=envelope.created_at,
        started_at=envelope.started_at,
        provenance=envelope.provenance,
    )


def terminal_envelope(
    *,
    running: EvaluationRunEnvelope,
    status: EvaluationStatus,
    validity: str | None,
    passed: bool | None,
    metrics: Mapping[str, object],
    result_payload: Mapping[str, object],
    diagnostic_task_id: str | None,
    failure_category: str | None,
    completed_at: datetime,
) -> EvaluationRunEnvelope:
    """Advance a running identity to one validated terminal state."""
    if running.status != "running":
        raise ValueError("Only a running evaluation can become terminal.")
    envelope = EvaluationRunEnvelope(
        artifact_schema_version=running.artifact_schema_version,
        run_id=running.run_id,
        evaluation_kind=running.evaluation_kind,
        scenario_id=running.scenario_id,
        suite_version=running.suite_version,
        status=status,
        validity=validity,
        passed=passed,
        metrics=_mapping_copy(metrics),
        result_payload=_mapping_copy(result_payload),
        metadata=_json_copy(running.metadata),
        provenance=running.provenance,
        diagnostic_task_id=diagnostic_task_id,
        failure_category=failure_category,
        created_at=running.created_at,
        started_at=running.started_at,
        completed_at=completed_at,
    )
    _validate_envelope(envelope)
    return envelope


def interrupted_envelope(
    running: EvaluationRunEnvelope,
    *,
    completed_at: datetime,
    failure_category: str = "operator_interrupt",
) -> EvaluationRunEnvelope:
    """Build the common safe terminal state for a captured cancellation."""
    return terminal_envelope(
        running=running,
        status="interrupted",
        validity="INFRA_INVALID",
        passed=None,
        metrics={},
        result_payload={},
        diagnostic_task_id=None,
        failure_category=failure_category,
        completed_at=completed_at,
    )


def artifact_checksum(envelope: EvaluationRunEnvelope) -> str:
    """Return SHA-256 for canonical UTF-8 JSON without self-referential fields."""
    canonical = json.dumps(
        envelope.to_json(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def validate_run_id(run_id: str) -> str:
    """Validate a run ID as a single safe filename component."""
    if (
        not _RUN_ID_RE.fullmatch(run_id)
        or ".." in run_id
        or Path(run_id).name != run_id
        or "/" in run_id
        or "\\" in run_id
    ):
        raise ValueError("Evaluation run ID is invalid.")
    return run_id


def _validate_envelope(envelope: EvaluationRunEnvelope) -> None:
    if envelope.artifact_schema_version != ARTIFACT_SCHEMA_VERSION:
        raise ValueError("Unsupported evaluation artifact schema version.")
    validate_run_id(envelope.run_id)
    if not envelope.scenario_id.strip() or not envelope.suite_version.strip():
        raise ValueError("Evaluation identity fields must be non-empty.")
    _require_aware_utc(envelope.created_at)
    _require_aware_utc(envelope.started_at)
    if envelope.completed_at is not None:
        _require_aware_utc(envelope.completed_at)
    if envelope.started_at < envelope.created_at:
        raise ValueError("Evaluation start time cannot precede creation time.")
    if envelope.completed_at is not None and envelope.completed_at < envelope.started_at:
        raise ValueError("Evaluation completion time cannot precede start time.")
    if envelope.status == "running":
        if any(
            value is not None
            for value in (
                envelope.validity,
                envelope.passed,
                envelope.diagnostic_task_id,
                envelope.failure_category,
                envelope.completed_at,
            )
        ) or envelope.metrics or envelope.result_payload:
            raise ValueError("Running evaluation contains terminal fields.")
    elif envelope.completed_at is None:
        raise ValueError("Terminal evaluation requires a completion time.")
    if envelope.status == "passed" and envelope.passed is not True:
        raise ValueError("Passed evaluation requires passed=true.")
    if envelope.status == "failed" and envelope.passed is not False:
        raise ValueError("Failed evaluation requires passed=false.")
    if envelope.status in {"agent_failed", "infra_invalid", "interrupted"} and envelope.passed:
        raise ValueError("Invalid evaluation state cannot be passed.")
    _validate_container(envelope.metadata, _METADATA_KEYS[envelope.evaluation_kind], "metadata")
    _validate_container(envelope.metrics, _METRIC_KEYS[envelope.evaluation_kind], "metrics")
    _validate_container(
        envelope.result_payload,
        _RESULT_KEYS[envelope.evaluation_kind],
        "result payload",
    )


def _validate_container(value: JsonDict, allowed: frozenset[str], label: str) -> None:
    _reject_forbidden_keys(value)
    unknown = set(value).difference(allowed)
    if unknown:
        raise ValueError(f"Evaluation {label} field is not allowed: {sorted(unknown)[0]}")


def _reject_forbidden_keys(value: object) -> None:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        for key, item in mapping.items():
            canonical = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if canonical in _FORBIDDEN_KEY_TOKENS:
                raise ValueError(f"Evaluation artifact contains forbidden field: {key}")
            _reject_forbidden_keys(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        sequence = cast(Sequence[object], value)
        for item in sequence:
            _reject_forbidden_keys(item)


def _require_aware_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Evaluation timestamps require timezone information.")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("Evaluation timestamps must use UTC.")


def _format_datetime(value: datetime) -> str:
    _require_aware_utc(value)
    return value.isoformat().replace("+00:00", "Z")


def _mapping_copy(value: Mapping[str, object]) -> JsonDict:
    return _json_copy(dict(value))


def _json_copy(value: JsonDict) -> JsonDict:
    serialized = json.dumps(value, ensure_ascii=False, allow_nan=False)
    parsed: object = json.loads(serialized)
    if not isinstance(parsed, dict):
        raise ValueError("Evaluation JSON value must be an object.")
    return cast(JsonDict, parsed)


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Evaluation field must be non-empty text: {key}")
    return value


def _optional_text(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Evaluation field must be text or null: {key}")
    return value


def _optional_bool(payload: Mapping[str, object], key: str) -> bool | None:
    value = payload.get(key)
    if value is None or isinstance(value, bool):
        return value
    raise ValueError(f"Evaluation field must be boolean or null: {key}")


def _required_literal(payload: Mapping[str, object], key: str, allowed: frozenset[str]) -> str:
    value = _required_text(payload, key)
    if value not in allowed:
        raise ValueError(f"Evaluation field has unsupported value: {key}")
    return value


def _required_json_dict(payload: Mapping[str, object], key: str) -> JsonDict:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"Evaluation field must be an object: {key}")
    return _json_copy(cast(JsonDict, value))


def _required_datetime(payload: Mapping[str, object], key: str) -> datetime:
    value = _required_text(payload, key)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require_aware_utc(parsed)
    return parsed


def _optional_datetime(payload: Mapping[str, object], key: str) -> datetime | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Evaluation field must be a timestamp or null: {key}")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require_aware_utc(parsed)
    return parsed
