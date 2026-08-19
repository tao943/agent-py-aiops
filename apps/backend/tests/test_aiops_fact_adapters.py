from __future__ import annotations

from super_ai.aiops.adjudication import EvidencePredicate
from super_ai.aiops.facts import (
    PublicToolObservation,
    evaluate_predicate,
    extract_public_facts,
)


def _observation(tool: str, evidence_id: str, output: dict[str, object]) -> PublicToolObservation:
    return PublicToolObservation(tool_name=tool, evidence_id=evidence_id, output=output)


def test_public_facts_can_compare_nginx_port_with_container_config() -> None:
    facts = extract_public_facts(
        observations=(
            _observation(
                "InspectContainer",
                "e1",
                {"status": "exited", "configuredPorts": [8080]},
            ),
            _observation(
                "InspectNginx",
                "e2",
                {"upstreamPort": 8080, "resolvedAddresses": ["172.30.0.12"]},
            ),
        )
    )

    assert evaluate_predicate(
        facts,
        EvidencePredicate(
            left_fact="InspectNginx.upstreamPort",
            operator="in",
            right_fact="InspectContainer.configuredPorts",
        ),
    ) is True


def test_secret_shaped_keys_are_removed_at_every_depth() -> None:
    facts = extract_public_facts(
        observations=(
            _observation(
                "InspectContainer",
                "e1",
                {
                    "status": "running",
                    "apiKey": "secret-a",
                    "nested": {
                        "Authorization": "secret-b",
                        "safe": "visible",
                        "PASSWORD": "secret-c",
                    },
                },
            ),
        )
    )

    keys = {fact.key for fact in facts}
    values = {fact.value for fact in facts}
    assert keys == {"InspectContainer.nested.safe", "InspectContainer.status"}
    assert not {"secret-a", "secret-b", "secret-c"} & values


def test_fact_expansion_is_depth_and_count_bounded() -> None:
    output: dict[str, object] = {f"field{index:02d}": index for index in range(80)}
    output["nested"] = {"level2": {"level3": {"tooDeep": "hidden"}}}

    facts = extract_public_facts(
        observations=(_observation("InspectContainer", "e1", output),)
    )

    assert len(facts) == 64
    assert all("tooDeep" not in fact.key for fact in facts)


def test_fact_order_and_list_values_are_canonical() -> None:
    facts = extract_public_facts(
        observations=(
            _observation(
                "InspectNginx",
                "e2",
                {
                    "z": True,
                    "ports": [8081, 8080],
                    "a": "first",
                },
            ),
        )
    )

    assert [fact.key for fact in facts] == [
        "InspectNginx.a",
        "InspectNginx.ports",
        "InspectNginx.z",
    ]
    assert facts[1].value == (8081, 8080)


def test_object_array_fields_are_projected_as_bounded_scalar_facts() -> None:
    facts = extract_public_facts(
        observations=(
            _observation(
                "SearchLog",
                "e-cls",
                {
                    "recordCount": 3,
                    "records": [
                        {"event": "request_received", "level": "INFO"},
                        {"event": "database_contention", "level": "ERROR"},
                        {"event": "alert_fired", "level": "WARN"},
                    ],
                },
            ),
        )
    )

    projected = {fact.key: fact.value for fact in facts}
    assert projected["SearchLog.records.event"] == (
        "request_received",
        "database_contention",
        "alert_fired",
    )
    assert projected["SearchLog.records.level"] == ("INFO", "ERROR", "WARN")


def test_unknown_public_field_creates_context_fact_without_a_disposition() -> None:
    facts = extract_public_facts(
        observations=(
            _observation("NewDiagnosticTool", "e-new", {"novelSignal": "value"}),
        )
    )

    assert len(facts) == 1
    assert facts[0].key == "NewDiagnosticTool.novelSignal"
    assert facts[0].quality == "context"
