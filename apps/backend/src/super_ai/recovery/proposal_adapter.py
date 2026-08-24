"""Derive closed recovery proposals from persisted validated diagnostics."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from super_ai.aiops.facts import PublicToolObservation, extract_public_facts
from super_ai.memory.repositories import DiagnosticEvidenceRecord
from super_ai.recovery.config import ProductionRecoverySettings
from super_ai.recovery.contracts import RecoveryAction, canonical_json

_ALLOWED_VALIDATOR_ORIGINS = frozenset(
    {"deterministic", "llm_confirmed", "deterministic_grounded_fallback"}
)


@dataclass(frozen=True, slots=True)
class ValidatedDiagnosticDecision:
    component: str
    mechanism: str
    evidence_ids: tuple[str, ...]
    validator_origin: str
    evidence_sufficient: bool
    deterministic_checks_passed: bool


@dataclass(frozen=True, slots=True)
class RecoveryProposal:
    action: RecoveryAction
    target_key: str
    canonical_arguments: dict[str, object]
    evidence_ids: tuple[str, ...]
    validator_origin: str
    trusted_snapshot: dict[str, object]


class RecoveryProposalAdapter:
    """Map validated component/mechanism pairs to a server-owned target."""

    def resolve(
        self,
        decision: ValidatedDiagnosticDecision,
        evidence: Sequence[DiagnosticEvidenceRecord],
        settings: ProductionRecoverySettings,
    ) -> RecoveryProposal | None:
        if (
            not decision.evidence_sufficient
            or not decision.deterministic_checks_passed
            or decision.validator_origin not in _ALLOWED_VALIDATOR_ORIGINS
            or not decision.evidence_ids
        ):
            return None
        target_key = settings.selector_target(decision.component, decision.mechanism)
        if target_key is None:
            return None
        compose = settings.compose_targets.get(target_key)
        postgres = settings.postgres_targets.get(target_key)
        if (compose is None) == (postgres is None):
            return None
        target = compose or postgres
        assert target is not None
        linked_ids = frozenset(decision.evidence_ids)
        selected = tuple(item for item in evidence if item.id in linked_ids)
        if frozenset(item.id for item in selected) != linked_ids:
            return None
        facts = extract_public_facts(_public_observations(selected))
        fact_keys = frozenset(fact.key for fact in facts)
        required = target.diagnostic_selector.required_evidence_facts
        if not set(required).issubset(fact_keys):
            return None
        required_values: dict[str, object] = {}
        for key in required:
            values = [fact.value for fact in facts if fact.key == key]
            if len(values) != 1:
                return None
            required_values[key] = values[0]
        action: RecoveryAction = (
            "restart_compose_service"
            if compose is not None
            else "terminate_postgres_blocker"
        )
        trusted_snapshot: dict[str, object] = {
            "component": decision.component,
            "mechanism": decision.mechanism,
            "evidenceFactKeys": list(sorted(required)),
            "validatorOrigin": decision.validator_origin,
        }
        if postgres is not None:
            logical_resource = required_values.get(
                "InspectPostgresLockGraph.lockedResource"
            )
            if (
                not isinstance(logical_resource, str)
                or logical_resource not in postgres.lock_resource_mappings
            ):
                return None
            trusted_snapshot.update(
                {
                    "lockResourceKey": logical_resource,
                    "lockRelationshipFingerprint": lock_relationship_fingerprint(
                        target_key=target_key,
                        database_config_key=postgres.database_config_key,
                        database_identity=postgres.database_identity,
                        component=decision.component,
                        mechanism=decision.mechanism,
                        required_values=required_values,
                    ),
                }
            )
        return RecoveryProposal(
            action=action,
            target_key=target_key,
            canonical_arguments={},
            evidence_ids=tuple(sorted(linked_ids)),
            validator_origin=decision.validator_origin,
            trusted_snapshot=trusted_snapshot,
        )


def _public_observations(
    evidence: Sequence[DiagnosticEvidenceRecord],
) -> tuple[PublicToolObservation, ...]:
    observations: list[PublicToolObservation] = []
    for item in evidence:
        output = _string_mapping(item.payload.get("output"))
        if output is None:
            continue
        observations.append(
            PublicToolObservation(
                tool_name=item.source,
                evidence_id=item.id,
                output=output,
            )
        )
    return tuple(observations)


def _string_mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    mapping = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        return None
    return cast(Mapping[str, object], mapping)


def lock_relationship_fingerprint(
    *,
    target_key: str,
    database_config_key: str,
    database_identity: str,
    component: str,
    mechanism: str,
    required_values: Mapping[str, object],
) -> str:
    material = {
        "component": component,
        "databaseConfigKey": database_config_key,
        "databaseIdentity": database_identity,
        "mechanism": mechanism,
        "requiredFacts": dict(required_values),
        "targetKey": target_key,
    }
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()
