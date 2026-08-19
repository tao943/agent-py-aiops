from __future__ import annotations

import pytest

from super_ai.aiops.adjudication import (
    DiagnosticFact,
    Disposition,
    EvidencePredicate,
    FactQuality,
    HypothesisAssessment,
    HypothesisEvidenceRule,
    assess_sufficiency,
    reduce_hypotheses,
)


def _assessment(hypothesis_id: str) -> HypothesisAssessment:
    return HypothesisAssessment(
        hypothesis_id=hypothesis_id,
        disposition="unresolved",
        evidence_ids=(),
        reason_code="awaiting_public_evidence",
        assessment_source="deterministic",
    )


def _fact(
    key: str,
    value: object,
    evidence_id: str,
    *,
    public: bool = True,
    quality: FactQuality = "direct",
) -> DiagnosticFact:
    return DiagnosticFact(
        key=key,
        value=value,
        evidence_id=evidence_id,
        source_tool="InspectContainer",
        quality=quality,
        public=public,
    )


def _rule(
    hypothesis_id: str,
    key: str,
    expected: object,
    disposition: Disposition,
) -> HypothesisEvidenceRule:
    return HypothesisEvidenceRule.for_literal(
        template_id=f"test_{hypothesis_id}_{disposition}",
        hypothesis_id=hypothesis_id,
        predicate=EvidencePredicate(left_fact=key, operator="eq", expected=expected),
        when_true=disposition,
        reason_code=f"{hypothesis_id}_{disposition}",
    )


def test_complete_cause_does_not_close_unaddressed_competitor() -> None:
    result = reduce_hypotheses(
        assessments=(_assessment("process_down"), _assessment("port_mismatch")),
        facts=(_fact("container.status", "exited", "evidence-process"),),
        rules=(_rule("process_down", "container.status", "exited", "supported"),),
    )

    by_id = {item.hypothesis_id: item for item in result}
    assert by_id["process_down"].disposition == "supported"
    assert by_id["port_mismatch"].disposition == "unresolved"
    assert assess_sufficiency(result).status == "insufficient"


def test_causally_inactive_requires_cited_public_evidence() -> None:
    with pytest.raises(ValueError, match="public evidence"):
        HypothesisAssessment(
            hypothesis_id="port_mismatch",
            disposition="causally_inactive",
            evidence_ids=(),
            reason_code="inactive_for_failure_path",
            assessment_source="deterministic",
        )


def test_duplicate_fact_does_not_duplicate_evidence_or_transition() -> None:
    fact = _fact("container.status", "exited", "evidence-process")
    result = reduce_hypotheses(
        assessments=(_assessment("process_down"),),
        facts=(fact, fact),
        rules=(_rule("process_down", "container.status", "exited", "supported"),),
    )

    assert result[0].evidence_ids == ("evidence-process",)
    assert len(result[0].transitions) == 1
    assert result[0].transitions[0].previous_disposition == "unresolved"
    assert result[0].transitions[0].next_disposition == "supported"


def test_direct_refutation_closes_a_competitor() -> None:
    result = reduce_hypotheses(
        assessments=(_assessment("port_mismatch"),),
        facts=(_fact("nginx.upstream_matches", True, "evidence-route"),),
        rules=(_rule("port_mismatch", "nginx.upstream_matches", True, "refuted"),),
    )

    assert result[0].disposition == "refuted"
    assert result[0].evidence_ids == ("evidence-route",)


def test_two_supported_hypotheses_fail_sufficiency() -> None:
    assessments = tuple(
        HypothesisAssessment(
            hypothesis_id=hypothesis_id,
            disposition="supported",
            evidence_ids=(evidence_id,),
            reason_code="direct_support",
            assessment_source="deterministic",
        )
        for hypothesis_id, evidence_id in (("cause_a", "e-a"), ("cause_b", "e-b"))
    )

    decision = assess_sufficiency(assessments)

    assert decision.status == "insufficient"
    assert decision.supported_hypotheses == ("cause_a", "cause_b")
    assert "exactly one" in decision.summary


def test_conflicting_direct_rules_return_to_unresolved() -> None:
    result = reduce_hypotheses(
        assessments=(_assessment("process_down"),),
        facts=(
            _fact("container.status", "exited", "evidence-exited"),
            _fact("container.health", "healthy", "evidence-healthy"),
        ),
        rules=(
            _rule("process_down", "container.status", "exited", "supported"),
            _rule("process_down", "container.health", "healthy", "refuted"),
        ),
    )

    assert result[0].disposition == "unresolved"
    assert result[0].has_high_quality_conflict is True
    assert result[0].evidence_ids == ("evidence-exited", "evidence-healthy")


def test_rule_for_unknown_hypothesis_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown hypothesis"):
        reduce_hypotheses(
            assessments=(_assessment("known"),),
            facts=(_fact("container.status", "exited", "evidence-process"),),
            rules=(_rule("unknown", "container.status", "exited", "supported"),),
        )


def test_non_public_fact_cannot_close_hypothesis() -> None:
    result = reduce_hypotheses(
        assessments=(_assessment("process_down"),),
        facts=(
            _fact(
                "container.status",
                "exited",
                "ground-truth-evidence",
                public=False,
            ),
        ),
        rules=(_rule("process_down", "container.status", "exited", "supported"),),
    )

    assert result[0].disposition == "unresolved"
    assert result[0].evidence_ids == ()


def test_context_fact_cannot_deterministically_close_hypothesis() -> None:
    result = reduce_hypotheses(
        assessments=(_assessment("process_down"),),
        facts=(
            _fact(
                "container.status",
                "exited",
                "context-evidence",
                quality="context",
            ),
        ),
        rules=(_rule("process_down", "container.status", "exited", "supported"),),
    )

    assert result[0].disposition == "unresolved"
    assert result[0].evidence_ids == ()


def test_sufficiency_accepts_one_supported_and_grounded_closed_alternatives() -> None:
    assessments = (
        HypothesisAssessment(
            hypothesis_id="process_down",
            disposition="supported",
            evidence_ids=("e-process",),
            reason_code="process_exited",
            assessment_source="deterministic",
        ),
        HypothesisAssessment(
            hypothesis_id="port_mismatch",
            disposition="causally_inactive",
            evidence_ids=("e-route",),
            reason_code="route_not_on_failure_path",
            assessment_source="deterministic",
        ),
    )

    decision = assess_sufficiency(assessments)

    assert decision.status == "sufficient"
    assert decision.evidence_ids == ("e-process", "e-route")
    assert decision.supported_hypotheses == ("process_down",)
    assert decision.refuted_hypotheses == ("port_mismatch",)
