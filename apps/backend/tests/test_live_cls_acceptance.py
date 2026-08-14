from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from super_ai.evaluation.live.cli import build_live_evidence_runtime
from super_ai.evaluation.live.domain import LiveFaultObservation
from super_ai.evaluation.live.scenarios import load_live_scenario, validate_run_id

pytestmark = pytest.mark.live_cls

LIVE_SCENARIO = (
    Path(__file__).resolve().parents[3]
    / "benchmarks"
    / "agentpy"
    / "live"
    / "APY-LIVE-PG-LOCK-001"
)


@pytest.mark.asyncio
async def test_real_cls_upload_and_search_are_run_scoped() -> None:
    config_path = os.getenv("LIVE_CLS_CONFIG")
    if not config_path:
        raise AssertionError("LIVE_CLS_CONFIG must point to the real ignored project config.")
    preparer, cls_client = build_live_evidence_runtime(
        evidence_source="cls",
        config_path=config_path,
    )
    assert cls_client is not None
    run_id = f"live-cls-contract-{uuid4().hex[:12]}"
    context = await preparer.prepare(
        identity=validate_run_id(run_id),
        scenario=load_live_scenario(LIVE_SCENARIO),
        observation=LiveFaultObservation(101, 102, True, True),
    )

    assert context.source == "cls"
    assert context.cls_scope is not None
    assert context.cls_scope.run_id == run_id
    assert context.readiness is not None
    assert context.readiness.indexed_log_count >= context.readiness.expected_log_count
    assert context.readiness.expected_log_count == 3
