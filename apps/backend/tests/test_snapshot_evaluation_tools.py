from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema.validators import validator_for

from super_ai.evaluation import SnapshotMcpClient
from super_ai.mcp.tool_arguments import normalize_tool_arguments
from super_ai.mcp_client import McpClientError

SCENARIOS = Path(__file__).resolve().parents[3] / "benchmarks" / "agentpy" / "scenarios"
APY_003 = SCENARIOS / "APY-003"


async def test_snapshot_client_replays_typed_observations() -> None:
    client = SnapshotMcpClient.from_yaml(APY_003 / "snapshot" / "tool_responses.yaml")

    names = {tool.name for tool in await client.discover_tools()}
    assert names == {"InspectContainer", "InspectNginx"}

    result = await client.call_tool("InspectContainer", {"service": "checkout-service"})
    assert result["status"] == "exited"
    assert result["exitCode"] == 137
    assert result["benchmarkEvidenceId"] == "container-status-exited"
    assert client.observations[0].evidence_id == "container-status-exited"
    assert client.observations[0].sequence == 1


async def test_snapshot_client_returns_defensive_result_copies() -> None:
    client = SnapshotMcpClient.from_yaml(APY_003 / "snapshot" / "tool_responses.yaml")

    first = await client.call_tool("InspectContainer", {"service": "checkout-service"})
    first["status"] = "tampered"
    second = await client.call_tool("InspectContainer", {"service": "checkout-service"})

    assert second["status"] == "exited"
    assert client.observations[0].result["status"] == "exited"


async def test_snapshot_client_rejects_unknown_tool_and_arguments() -> None:
    client = SnapshotMcpClient.from_yaml(APY_003 / "snapshot" / "tool_responses.yaml")

    with pytest.raises(McpClientError, match="not available"):
        await client.call_tool("ReadGroundTruth", {})
    with pytest.raises(McpClientError, match="arguments"):
        await client.call_tool("InspectContainer", {"service": "other-service"})


async def test_snapshot_client_excludes_declared_ground_truth_tool(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "malicious-tool-responses.yaml"
    fixture.write_text(
        """
tools:
  - name: InspectSafeState
    description: Inspect public state.
    input_schema: {type: object}
    calls:
      - arguments: {}
        evidence_id: safe-state
        result: {status: healthy}
  - name: ReadGroundTruth
    description: Expose the evaluator answer.
    input_schema: {type: object}
    calls:
      - arguments: {}
        evidence_id: oracle-secret
        result: {secret: leaked}
""".lstrip(),
        encoding="utf-8",
    )

    client = SnapshotMcpClient.from_yaml(fixture)

    assert {tool.name for tool in await client.discover_tools()} == {"InspectSafeState"}
    with pytest.raises(McpClientError, match="not available"):
        await client.call_tool("ReadGroundTruth", {})
    assert client.observations == ()


async def test_snapshot_client_does_not_read_fixture_after_construction(tmp_path: Path) -> None:
    fixture = tmp_path / "tool_responses.yaml"
    fixture.write_text(
        (APY_003 / "snapshot" / "tool_responses.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    client = SnapshotMcpClient.from_yaml(fixture)
    fixture.unlink()

    result = await client.call_tool("InspectNginx", {"route": "checkout"})

    assert result["responseStatus"] == 502


async def test_snapshot_exposes_answer_free_defensive_argument_contracts() -> None:
    client = SnapshotMcpClient.from_yaml(
        SCENARIOS / "APY-013" / "snapshot" / "tool_responses.yaml"
    )

    contracts = client.tool_argument_contracts

    assert contracts["InspectPostgresErrors"].registered_calls == (
        {"service": "order-service", "windowMinutes": 15},
    )
    assert all("evidence" not in repr(contract).lower() for contract in contracts.values())
    copied_call = dict(contracts["InspectPostgresErrors"].registered_calls[0])
    copied_call["windowMinutes"] = 60
    assert client.tool_argument_contracts[
        "InspectPostgresErrors"
    ].registered_calls == ({"service": "order-service", "windowMinutes": 15},)


async def test_all_snapshot_calls_satisfy_schema_and_normalize_to_themselves() -> None:
    scenario_dirs = sorted(
        path
        for path in SCENARIOS.iterdir()
        if (path / "snapshot" / "tool_responses.yaml").is_file()
    )
    assert len(scenario_dirs) == 10

    for scenario_dir in scenario_dirs:
        client = SnapshotMcpClient.from_yaml(
            scenario_dir / "snapshot" / "tool_responses.yaml"
        )
        definitions = {tool.name: tool for tool in await client.discover_tools()}
        contracts = client.tool_argument_contracts
        assert set(contracts) == set(definitions)
        for name, contract in contracts.items():
            assert contract.registered_calls
            schema = definitions[name].input_schema
            validator_class = validator_for(schema)
            validator_class.check_schema(schema)
            validator = validator_class(schema)
            for registered_call in contract.registered_calls:
                validator.validate(cast(Any, dict(registered_call)))
                assert normalize_tool_arguments(
                    name,
                    registered_call,
                    contracts,
                ) == dict(registered_call)


def test_snapshot_contracts_keep_the_two_registered_multi_call_tools() -> None:
    upstream = SnapshotMcpClient.from_yaml(
        SCENARIOS / "APY-015" / "snapshot" / "tool_responses.yaml"
    ).tool_argument_contracts["ProbeUpstreamHealth"]
    retry_policy = SnapshotMcpClient.from_yaml(
        SCENARIOS / "APY-016" / "snapshot" / "tool_responses.yaml"
    ).tool_argument_contracts["InspectClientRetryPolicy"]

    assert {call["service"] for call in upstream.registered_calls} == {
        "checkout-upstream",
        "checkout-slow-endpoint",
    }
    assert retry_policy.fixed_arguments == {"client": "checkout-client"}
    assert {call["view"] for call in retry_policy.registered_calls} == {
        "effective-policy",
        "sampled-timeline",
    }


def test_snapshot_loader_rejects_a_registered_call_that_violates_schema(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "invalid-call.yaml"
    fixture.write_text(
        """
tools:
  - name: InspectErrors
    description: Inspect bounded errors.
    input_schema:
      type: object
      required: [windowMinutes]
      additionalProperties: false
      properties:
        windowMinutes: {type: integer}
    calls:
      - arguments: {windowMinutes: fifteen}
        evidence_id: invalid-call
        result: {count: 1}
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="InspectErrors.*schema") as captured:
        SnapshotMcpClient.from_yaml(fixture)

    assert "fifteen" not in str(captured.value)
