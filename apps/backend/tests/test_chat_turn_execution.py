from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest

from super_ai.chat.aiops_bridge import IncidentSummary, PublicDiagnosticReport
from super_ai.chat.execution import (
    ChatPostconditionFailed,
    ChatTurnExecutionRequest,
    ChatTurnExecutionService,
)
from super_ai.chat.intent import ChatRoute
from super_ai.chat.streaming import (
    ChatAgentEvent,
    ChatAgentRequest,
    ChatAgentStructuredResult,
    PolicyDispatchingChatAgentRunner,
)
from super_ai.memory.repositories import ChatMessageRecord


class FakeBridge:
    def __init__(self, *, unsafe_payload: bool = False) -> None:
        self.calls = 0
        self.unsafe_payload = unsafe_payload

    async def list_active_incidents(
        self, *, owner_user_id: str, limit: int = 10
    ) -> tuple[IncidentSummary, ...]:
        del owner_user_id, limit
        return ()

    async def get_incident(
        self, *, owner_user_id: str, incident_id: str
    ) -> IncidentSummary:
        del owner_user_id, incident_id
        raise AssertionError("incident read is not expected")

    async def get_diagnostic_report(
        self, *, owner_user_id: str, task_id: str
    ) -> PublicDiagnosticReport:
        assert owner_user_id == "owner_1"
        assert task_id == "diagnostic_1"
        self.calls += 1
        return PublicDiagnosticReport(
            id="report_1",
            task_id=task_id,
            title="数据库锁等待",
            content="结构化诊断已完成。",
            root_cause={"component": "postgresql", "mechanism": "lock_wait"},
            recovery_mode="manual_review",
            execution_permitted=self.unsafe_payload,
            human_approval_required=True,
            validator_status="deterministic_grounded_fallback",
            evidence_ids=("evidence_1",),
            created_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
        )


class ExplanationModel:
    async def ainvoke(self, input: object) -> object:
        assert "executionPermitted" in str(input)
        return type("Response", (), {"content": "该报告需要人工复核。"})()


class FailingExplanationModel:
    async def ainvoke(self, input: object) -> object:
        del input
        raise TimeoutError("private provider detail")


def _request() -> ChatTurnExecutionRequest:
    return ChatTurnExecutionRequest(
        owner_user_id="owner_1",
        content="查看 diagnostic_1 的报告",
        route=ChatRoute(
            "diagnostic_status",
            1.0,
            "rule",
            diagnostic_task_id="diagnostic_1",
        ),
        chat_run_id="run_1",
    )


@pytest.mark.asyncio
async def test_report_card_precedes_llm_explanation() -> None:
    events = [
        event
        async for event in ChatTurnExecutionService(
            bridge=FakeBridge(), explanation_model=ExplanationModel()
        ).execute(_request())
    ]

    assert [event.type for event in events[:2]] == [
        "execution.mode_selected",
        "structured.result",
    ]
    diagnostic = events[1].payload["diagnostic"]
    assert isinstance(diagnostic, dict)
    assert diagnostic["executionPermitted"] is False
    assert events[2].type == "explanation.delta"
    assert events[-1].type == "complete"
    assert events[-1].result is not None
    assert events[-1].result.status == "succeeded"


@pytest.mark.asyncio
async def test_model_failure_keeps_structured_success() -> None:
    bridge = FakeBridge()
    events = [
        event
        async for event in ChatTurnExecutionService(
            bridge=bridge, explanation_model=FailingExplanationModel()
        ).execute(_request())
    ]

    assert bridge.calls == 1
    assert [event.type for event in events] == [
        "execution.mode_selected",
        "structured.result",
        "explanation.degraded",
        "complete",
    ]
    result = events[-1].result
    assert result is not None
    assert result.status == "succeeded_with_degraded_explanation"
    assert result.structured_result is not None
    diagnostic = result.structured_result["diagnostic"]
    assert isinstance(diagnostic, dict)
    assert diagnostic["executionPermitted"] is False
    assert result.safe_error_code is None


@pytest.mark.asyncio
async def test_structured_postcondition_cannot_be_filled_by_model_text() -> None:
    with pytest.raises(ChatPostconditionFailed):
        async for _ in ChatTurnExecutionService(
            bridge=FakeBridge(unsafe_payload=True),
            explanation_model=ExplanationModel(),
        ).execute(_request()):
            pass


class FallbackRunner:
    def __init__(self) -> None:
        self.calls = 0

    def stream(self, request: ChatAgentRequest) -> AsyncIterator[ChatAgentEvent]:
        del request
        self.calls += 1

        async def events() -> AsyncIterator[ChatAgentEvent]:
            if False:
                yield ChatAgentStructuredResult({})

        return events()


@pytest.mark.asyncio
async def test_dispatcher_bypasses_react_for_direct_read() -> None:
    fallback = FallbackRunner()
    runner = PolicyDispatchingChatAgentRunner(
        fallback=fallback,
        direct_execution=ChatTurnExecutionService(
            bridge=FakeBridge(), explanation_model=ExplanationModel()
        ),
    )
    request = ChatAgentRequest(
        owner_user_id="owner_1",
        session_id="session_1",
        messages=(
            ChatMessageRecord(
                id="message_1",
                owner_user_id="owner_1",
                session_id="session_1",
                role="user",
                content="查看 diagnostic_1 的报告",
                metadata={},
                created_at=datetime(2026, 8, 22, tzinfo=timezone.utc),
            ),
        ),
        accessible_knowledge_base_ids=(),
        system_prompt="system",
        route=_request().route,
    )

    events = [event async for event in runner.stream(request)]

    assert fallback.calls == 0
    assert any(isinstance(event, ChatAgentStructuredResult) for event in events)
