from pathlib import Path

import pytest

from super_ai.evaluation import SnapshotMcpClient
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
