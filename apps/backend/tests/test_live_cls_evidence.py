from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from super_ai.evaluation.live.cls_evidence import (
    LiveClsEvidencePreparer,
    LiveClsRecordProvider,
    McpClsSearcher,
    build_cls_search_arguments,
    build_live_cls_records,
)
from super_ai.evaluation.live.domain import (
    LiveCheck,
    LiveClsScope,
    LiveFaultObservation,
    LiveInfrastructureError,
    LiveRunIdentity,
    LiveScenario,
)
from super_ai.evaluation.live.scenarios import load_live_scenario, validate_run_id
from super_ai.mcp_client import McpToolDefinition

LIVE_SCENARIOS = Path(__file__).resolve().parents[3] / "benchmarks" / "agentpy" / "live"
SCENARIO = load_live_scenario(LIVE_SCENARIOS / "APY-LIVE-PG-LOCK-001")
IDENTITY = validate_run_id("run-1")
OBSERVATION = LiveFaultObservation(
    "APY-LIVE-PG-LOCK-001",
    (LiveCheck("waiter_has_lock_event", True), LiveCheck("blocker_edge_confirmed", True)),
)
NOW = datetime(2026, 8, 14, 8, 0, tzinfo=timezone.utc)


def test_cls_search_arguments_are_derived_from_the_exact_live_scope() -> None:
    scope = LiveClsScope(
        region="ap-guangzhou",
        topic_id="topic-live",
        from_ms=1_700_000_000_000,
        to_ms=1_700_000_090_000,
        run_id="run-1",
        scenario_id="APY-LIVE-PG-LOCK-001",
        incident_id="APY-LIVE-PG-LOCK-001-run-1",
    )

    assert build_cls_search_arguments(scope) == {
        "Region": "ap-guangzhou",
        "TopicId": "topic-live",
        "From": 1_700_000_000_000,
        "To": 1_700_000_090_000,
        "Query": (
            'run_id:"run-1" AND scenario_id:"APY-LIVE-PG-LOCK-001" '
            'AND incident_id:"APY-LIVE-PG-LOCK-001-run-1"'
        ),
        "Limit": 20,
    }


@pytest.mark.parametrize("limit", (0, 101))
def test_cls_search_arguments_reject_an_unbounded_limit(limit: int) -> None:
    scope = LiveClsScope(
        region="ap-guangzhou",
        topic_id="topic-live",
        from_ms=1_700_000_000_000,
        to_ms=1_700_000_090_000,
        run_id="run-1",
        scenario_id="APY-LIVE-PG-LOCK-001",
        incident_id="APY-LIVE-PG-LOCK-001-run-1",
    )

    with pytest.raises(ValueError, match="between 1 and 100"):
        build_cls_search_arguments(scope, limit=limit)


class RecordingUploader:
    def __init__(
        self,
        *,
        error: BaseException | None = None,
        uploaded_count: int | None = None,
    ) -> None:
        self.error = error
        self.uploaded_count = uploaded_count
        self.records: tuple[Mapping[str, str], ...] = ()

    async def put(
        self, records: Sequence[Mapping[str, str]], *, filename: str
    ) -> int:
        assert filename == "agentpy-live-evidence.log"
        if self.error is not None:
            raise self.error
        self.records = tuple(records)
        return self.uploaded_count if self.uploaded_count is not None else len(records)


class SequenceSearcher:
    def __init__(
        self,
        responses: Sequence[Sequence[Mapping[str, object]]],
        *,
        error: BaseException | None = None,
    ) -> None:
        self.responses = list(responses)
        self.error = error
        self.scopes: list[LiveClsScope] = []

    async def search(self, scope: LiveClsScope) -> Sequence[Mapping[str, object]]:
        self.scopes.append(scope)
        if self.error is not None:
            raise self.error
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


class FakeClock:
    def __init__(self) -> None:
        self.elapsed = 0.0

    def monotonic(self) -> float:
        return self.elapsed

    def now(self) -> datetime:
        return NOW

    async def sleep(self, seconds: float) -> None:
        self.elapsed += seconds


def _record(*, run_id: str = "run-1") -> dict[str, object]:
    return {
        "run_id": run_id,
        "scenario_id": SCENARIO.id,
        "incident_id": f"{SCENARIO.id}-{run_id}",
        "event": "order_update_timeout",
    }


def _preparer(
    *,
    uploader: RecordingUploader,
    searcher: SequenceSearcher,
    clock: FakeClock,
    record_provider: LiveClsRecordProvider | None = None,
) -> LiveClsEvidencePreparer:
    return LiveClsEvidencePreparer(
        region="ap-guangzhou",
        topic_id="topic-live",
        uploader=uploader,
        searcher=searcher,
        timeout_seconds=2.0,
        poll_interval_seconds=1.0,
        now=clock.now,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        record_provider=record_provider,
    )


class RecordingOrderPoolProvider:
    def __init__(
        self,
        *,
        invalid_key: str | None = None,
        record_run_id: str | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.invalid_key = invalid_key
        self.record_run_id = record_run_id

    async def records(
        self,
        *,
        identity: LiveRunIdentity,
        scenario: LiveScenario,
        observation: LiveFaultObservation,
        now: datetime,
    ) -> Sequence[Mapping[str, str]]:
        del observation, now
        run_id = identity.run_id
        scenario_id = scenario.id
        self.calls.append(run_id)
        common = {
            "run_id": self.record_run_id or run_id,
            "scenario_id": scenario_id,
            "incident_id": f"{scenario_id}-{run_id}",
            "service": "order-api",
            "component": "order-api",
            "generation": "gen-1",
            "timestamp": "2026-08-20T12:00:00+00:00",
            "environment": "live-eval",
            "trace": f"{run_id}-request-1",
        }
        first = {
            **common,
            "request_id": "request-1",
            "event": "connection_checkout",
            "level": "INFO",
        }
        if self.invalid_key is not None:
            first[self.invalid_key] = "forbidden"
        return (
            first,
            {
                **common,
                "request_id": "request-1",
                "event": "order_update_failed",
                "level": "ERROR",
            },
            {
                **common,
                "request_id": "business-probe",
                "event": "pool_acquire_timeout",
                "level": "ERROR",
            },
        )


@pytest.mark.asyncio
async def test_order_pool_preparer_uploads_actual_provider_records() -> None:
    provider = RecordingOrderPoolProvider()
    scenario = replace(SCENARIO, id="APY-LIVE-ORDER-POOL-LEAK-001")
    observation = replace(OBSERVATION, scenario_id=scenario.id)
    uploader = RecordingUploader()
    searcher = SequenceSearcher(
        ([
            {
                "run_id": IDENTITY.run_id,
                "scenario_id": scenario.id,
                "incident_id": f"{scenario.id}-{IDENTITY.run_id}",
            }
        ] * 3,)
    )

    context = await _preparer(
        uploader=uploader,
        searcher=searcher,
        clock=FakeClock(),
        record_provider=provider,
    ).prepare(identity=IDENTITY, scenario=scenario, observation=observation)

    assert provider.calls == ["run-1"]
    assert uploader.records[0]["event"] == "connection_checkout"
    assert context.readiness is not None
    assert context.readiness.expected_log_count == 3


@pytest.mark.asyncio
async def test_order_pool_preparer_rejects_oracle_shaped_provider_keys() -> None:
    scenario = replace(SCENARIO, id="APY-LIVE-ORDER-POOL-LEAK-001")
    observation = replace(OBSERVATION, scenario_id=scenario.id)
    with pytest.raises(LiveInfrastructureError) as captured:
        await _preparer(
            uploader=RecordingUploader(),
            searcher=SequenceSearcher(([_record()],)),
            clock=FakeClock(),
            record_provider=RecordingOrderPoolProvider(invalid_key="primary_cause"),
        ).prepare(identity=IDENTITY, scenario=scenario, observation=observation)
    assert captured.value.category == "cls_records_invalid"


@pytest.mark.asyncio
async def test_order_pool_preparer_rejects_cross_run_provider_records() -> None:
    scenario = replace(SCENARIO, id="APY-LIVE-ORDER-POOL-LEAK-001")
    observation = replace(OBSERVATION, scenario_id=scenario.id)
    with pytest.raises(LiveInfrastructureError) as captured:
        await _preparer(
            uploader=RecordingUploader(),
            searcher=SequenceSearcher(([_record()],)),
            clock=FakeClock(),
            record_provider=RecordingOrderPoolProvider(record_run_id="other-run"),
        ).prepare(identity=IDENTITY, scenario=scenario, observation=observation)
    assert captured.value.category == "cls_records_invalid"


@pytest.mark.parametrize(
    ("scenario_id", "events"),
    (
        (
            "APY-LIVE-PG-LOCK-001",
            {"request_received", "database_contention", "alert_fired"},
        ),
        (
            "APY-LIVE-PG-DEADLOCK-001",
            {"request_received", "database_contention", "alert_fired"},
        ),
        (
            "APY-LIVE-REDIS-MAXCLIENTS-001",
            {"request_received", "connection_rejected", "alert_fired"},
        ),
        (
            "APY-LIVE-NGINX-TIMEOUT-001",
            {"request_received", "upstream_timeout", "alert_fired"},
        ),
    ),
)
def test_live_cls_records_are_scenario_scoped_without_revealing_oracle(
    scenario_id: str, events: set[str]
) -> None:
    records = build_live_cls_records(
        run_id=IDENTITY.run_id,
        scenario_id=scenario_id,
        incident_id=f"{scenario_id}-{IDENTITY.run_id}",
        now=NOW,
    )

    assert len(records) == 3
    assert {record["run_id"] for record in records} == {"run-1"}
    assert {record["scenario_id"] for record in records} == {scenario_id}
    assert {record["incident_id"] for record in records} == {
        f"{scenario_id}-run-1"
    }
    assert {record["event"] for record in records} == events
    assert all(
        {
            "run_id",
            "scenario_id",
            "incident_id",
            "service",
            "environment",
            "event",
            "level",
            "trace",
            "component",
            "timestamp",
        }
        <= set(record)
        for record in records
    )
    serialized = json.dumps(records)
    assert "row_lock_blocking" not in serialized
    assert "blocker" not in serialized
    assert "benchmark_clients_exhausted_maxclients" not in serialized
    assert "upstream_response_exceeded_proxy_read_timeout" not in serialized


@pytest.mark.asyncio
async def test_preparer_polls_until_every_uploaded_record_is_searchable() -> None:
    clock = FakeClock()
    uploader = RecordingUploader()
    matching = _record()
    searcher = SequenceSearcher(([matching], [matching, matching, matching]))

    context = await _preparer(
        uploader=uploader, searcher=searcher, clock=clock
    ).prepare(identity=IDENTITY, scenario=SCENARIO, observation=OBSERVATION)

    assert len(uploader.records) == 3
    assert context.source == "cls"
    assert context.cls_scope is not None
    assert context.cls_scope.run_id == "run-1"
    assert context.readiness is not None
    assert context.readiness.expected_log_count == 3
    assert context.readiness.indexed_log_count == 3
    assert context.readiness.attempts == 2


@pytest.mark.asyncio
async def test_preparer_ignores_foreign_run_records_and_times_out() -> None:
    clock = FakeClock()
    searcher = SequenceSearcher(([_record(run_id="run-2")],))

    with pytest.raises(LiveInfrastructureError) as captured:
        await _preparer(
            uploader=RecordingUploader(), searcher=searcher, clock=clock
        ).prepare(identity=IDENTITY, scenario=SCENARIO, observation=OBSERVATION)

    assert captured.value.category == "cls_index_timeout"
    assert len(searcher.scopes) == 3


@pytest.mark.asyncio
async def test_preparer_classifies_upload_failure_without_leaking_message() -> None:
    clock = FakeClock()
    uploader = RecordingUploader(error=RuntimeError("secret-key-value"))

    with pytest.raises(LiveInfrastructureError) as captured:
        await _preparer(
            uploader=uploader,
            searcher=SequenceSearcher(([_record()],)),
            clock=clock,
        ).prepare(identity=IDENTITY, scenario=SCENARIO, observation=OBSERVATION)

    assert captured.value.category == "cls_upload_failed"
    assert "secret" not in str(captured.value)


@pytest.mark.asyncio
async def test_preparer_rejects_partial_upload_confirmation() -> None:
    clock = FakeClock()

    with pytest.raises(LiveInfrastructureError) as captured:
        await _preparer(
            uploader=RecordingUploader(uploaded_count=2),
            searcher=SequenceSearcher(([_record()],)),
            clock=clock,
        ).prepare(identity=IDENTITY, scenario=SCENARIO, observation=OBSERVATION)

    assert captured.value.category == "cls_upload_incomplete"


@pytest.mark.asyncio
async def test_preparer_classifies_search_boundary_failure() -> None:
    clock = FakeClock()

    with pytest.raises(LiveInfrastructureError) as captured:
        await _preparer(
            uploader=RecordingUploader(),
            searcher=SequenceSearcher((), error=RuntimeError("secret-mcp-error")),
            clock=clock,
        ).prepare(identity=IDENTITY, scenario=SCENARIO, observation=OBSERVATION)

    assert captured.value.category == "cls_mcp_unavailable"
    assert "secret" not in str(captured.value)


@pytest.mark.asyncio
async def test_preparer_preserves_cancellation() -> None:
    clock = FakeClock()
    uploader = RecordingUploader(error=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await _preparer(
            uploader=uploader,
            searcher=SequenceSearcher(([_record()],)),
            clock=clock,
        ).prepare(identity=IDENTITY, scenario=SCENARIO, observation=OBSERVATION)


@pytest.mark.asyncio
async def test_mcp_searcher_builds_exact_run_scoped_query() -> None:
    class FakeMcpClient:
        def __init__(self) -> None:
            self.arguments: Mapping[str, object] = {}

        async def discover_tools(self) -> Sequence[McpToolDefinition]:
            return (McpToolDefinition("SearchLog", "Search logs", {}),)

        async def call_tool(
            self, name: str, arguments: Mapping[str, object]
        ) -> object:
            assert name == "SearchLog"
            self.arguments = arguments
            return {
                "records": [_record()],
                "benchmarkEvidenceId": "readiness-only",
            }

    mcp = FakeMcpClient()
    records = await McpClsSearcher(mcp, limit=20).search(
        LiveClsScope(
            region="ap-guangzhou",
            topic_id="topic-live",
            from_ms=1_000,
            to_ms=10_000,
            run_id="run-1",
            scenario_id=SCENARIO.id,
            incident_id=f"{SCENARIO.id}-run-1",
        )
    )

    assert records == (_record(),)
    assert mcp.arguments["Region"] == "ap-guangzhou"
    assert mcp.arguments["TopicId"] == "topic-live"
    assert mcp.arguments["From"] == 1_000
    assert mcp.arguments["To"] == 10_000
    assert mcp.arguments["Limit"] == 20
    assert mcp.arguments["Query"] == (
        f'run_id:"run-1" AND scenario_id:"{SCENARIO.id}" '
        f'AND incident_id:"{SCENARIO.id}-run-1"'
    )
