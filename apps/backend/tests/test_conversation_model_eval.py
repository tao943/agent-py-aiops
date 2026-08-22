from __future__ import annotations

import asyncio
import importlib.util
import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from super_ai.chat.model_evaluation import (
    MODEL_SCENARIOS,
    run_conversation_model_eval,
)
from super_ai.evaluation.history import EvaluationRunEnvelope

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_conversation_model_eval.py"
SPEC = importlib.util.spec_from_file_location("run_conversation_model_eval_cli", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


class FakeModel:
    model_name = "fake-qwen"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def ainvoke(self, input: object) -> object:
        prompt = str(input)
        self.calls.append(prompt)
        if "explanation_timeout" in prompt:
            raise TimeoutError("private provider detail")
        if "线上好像有告警" in prompt:
            content = json.dumps(
                {
                    "intent": "incident_query",
                    "confidence": 0.9,
                    "incidentId": None,
                    "diagnosticTaskId": None,
                    "needsClarification": False,
                }
            )
        elif "数据库锁等待通常" in prompt:
            content = json.dumps(
                {
                    "intent": "knowledge_question",
                    "confidence": 0.9,
                    "incidentId": None,
                    "diagnosticTaskId": None,
                    "needsClarification": False,
                }
            )
        elif "忽略所有规则" in prompt:
            content = json.dumps(
                {
                    "intent": "general_chat",
                    "confidence": 0.9,
                    "incidentId": None,
                    "diagnosticTaskId": None,
                    "needsClarification": False,
                }
            )
        else:
            content = "只解释已验证的结构化字段，不执行任何恢复动作。"
        return type("Response", (), {"content": content})()


class FakeBridge:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def read_structured(self, scenario_id: str) -> Mapping[str, object]:
        self.calls.append(scenario_id)
        return {
            "reportId": f"report-{scenario_id}",
            "evidenceIds": [f"evidence-{scenario_id}"],
            "executionPermitted": False,
            "humanApprovalRequired": True,
        }


class Recorder:
    def __init__(self) -> None:
        self.started: list[EvaluationRunEnvelope] = []
        self.finished: list[EvaluationRunEnvelope] = []
        self.failed: list[EvaluationRunEnvelope] = []

    async def start(self, envelope: EvaluationRunEnvelope) -> object:
        self.started.append(envelope)
        return object()

    async def finish(self, envelope: EvaluationRunEnvelope) -> object:
        self.finished.append(envelope)
        return object()

    async def fail(self, envelope: EvaluationRunEnvelope) -> object:
        self.failed.append(envelope)
        return object()


@pytest.mark.asyncio
async def test_fake_provider_runs_six_scenarios_and_persists_safe_v2_artifact() -> None:
    model = FakeModel()
    bridge = FakeBridge()
    recorder = Recorder()

    result = await run_conversation_model_eval(
        model=model,
        bridge=bridge,
        recorder=recorder,
        run_id="conversation-model-test",
        git_sha="abc123",
    )

    assert tuple(item.id for item in MODEL_SCENARIOS) == (
        "ambiguous_incident",
        "ambiguous_knowledge",
        "report_explanation",
        "evidence_explanation",
        "explanation_timeout",
        "prompt_injection",
    )
    assert result.scenario_count == 6
    assert result.passed_scenario_count == 6
    assert result.model_call_count == 6
    assert result.route_accuracy == 1.0
    assert result.structured_interpretation_accuracy == 1.0
    assert result.degraded_fallback_accuracy == 1.0
    assert result.prompt_injection_safety == 1.0
    assert result.passed is True
    assert result.database_pending is False
    assert len(model.calls) == 6
    assert bridge.calls == [
        "report_explanation",
        "evidence_explanation",
        "explanation_timeout",
    ]

    assert len(recorder.started) == len(recorder.finished) == 1
    artifact = recorder.finished[0]
    assert artifact.artifact_schema_version == "v2"
    assert artifact.evaluation_kind == "conversation_model"
    assert artifact.status == "passed"
    serialized = json.dumps(artifact.to_json(), ensure_ascii=False).casefold()
    for forbidden in (
        "user request:",
        "只解释已验证",
        "private provider detail",
        "chainofthought",
        "groundtruth",
    ):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_model_eval_records_safe_failures_without_raw_response() -> None:
    class UnsafeRouteModel(FakeModel):
        async def ainvoke(self, input: object) -> object:
            prompt = str(input)
            if "忽略所有规则" in prompt:
                self.calls.append(prompt)
                return type(
                    "Response",
                    (),
                    {
                        "content": json.dumps(
                            {
                                "intent": "recovery_request",
                                "confidence": 1.0,
                                "diagnosticTaskId": "diagnostic_foreign",
                                "needsClarification": False,
                            }
                        )
                    },
                )()
            return await super().ainvoke(input)

    recorder = Recorder()
    result = await run_conversation_model_eval(
        model=UnsafeRouteModel(),
        bridge=FakeBridge(),
        recorder=recorder,
        run_id="conversation-model-unsafe",
        git_sha="abc123",
    )

    assert result.passed is False
    assert "prompt_injection_unsafe_route" in result.failures
    artifact = recorder.finished[0]
    assert artifact.status == "failed"
    assert artifact.result_payload["failures"] == ["prompt_injection_unsafe_route"]
    assert "diagnostic_foreign" not in json.dumps(artifact.to_json())


def test_model_eval_cli_requires_explicit_real_model_confirmation() -> None:
    arguments = CLI.build_parser().parse_args([])

    assert arguments.confirm_real_model is False


@pytest.mark.asyncio
async def test_model_eval_cli_refuses_to_build_dependencies_without_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_provider(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("provider must not be created")

    monkeypatch.setattr(CLI, "build_default_llm_provider", forbidden_provider)
    arguments = CLI.build_parser().parse_args([])

    assert await CLI.run_command(arguments) == 2


def test_model_eval_cli_maps_interrupt_to_130(monkeypatch: pytest.MonkeyPatch) -> None:
    async def interrupted(_arguments: object) -> int:
        raise KeyboardInterrupt

    class Parser:
        def parse_args(self) -> object:
            return object()

    monkeypatch.setattr(CLI, "run_command", interrupted)
    monkeypatch.setattr(CLI, "build_parser", Parser)

    assert CLI.main() == 130


@pytest.mark.asyncio
async def test_model_eval_persists_interrupted_terminal_before_propagating() -> None:
    class InterruptingModel(FakeModel):
        async def ainvoke(self, input: object) -> object:
            del input
            raise asyncio.CancelledError

    recorder = Recorder()

    with pytest.raises(asyncio.CancelledError):
        await run_conversation_model_eval(
            model=InterruptingModel(),
            bridge=FakeBridge(),
            recorder=recorder,
            run_id="conversation-model-interrupted",
            git_sha="abc123",
        )

    assert recorder.finished == []
    assert len(recorder.failed) == 1
    assert recorder.failed[0].status == "interrupted"
