from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from scripts.audit_knowledge_catalog import audit_catalog

KNOWLEDGE = Path(__file__).resolve().parents[3] / "docs" / "knowledge-candidates"
EXPECTED_CARD_FILENAMES = frozenset(
    {
        "host-cpu-load-pressure.md",
        "host-disk-capacity-pressure.md",
        "host-file-descriptor-exhaustion.md",
        "http-rate-limit-retry-storm.md",
        "kubernetes-dns-debugging.md",
        "kubernetes-memory-saturation.md",
        "kubernetes-pod-crashloop.md",
        "kubernetes-service-endpoint-mismatch.md",
        "microservice-timeout.md",
        "nginx-routing-service-discovery.md",
        "nginx-upstream-502.md",
        "nginx-upstream-timeout.md",
        "postgres-connectivity-auth.md",
        "postgres-deadlock.md",
        "postgres-disk-wal-pressure.md",
        "postgres-pool-exhaustion.md",
        "postgres-replication-lag.md",
        "postgres-slow-query-lock-wait.md",
        "queue-backlog.md",
        "queue-consumer-stalled.md",
        "queue-poison-message-dlq.md",
        "redis-failover-reconnect.md",
        "redis-maxclients-pressure.md",
        "redis-memory-eviction.md",
        "redis-slow-command-hot-key.md",
        "redis-unavailable.md",
        "service-circuit-breaker-degradation.md",
        "service-startup-config-failure.md",
        "service-thread-pool-saturation.md",
        "tls-certificate-handshake-failure.md",
    }
)
OPERATIONAL_HEADINGS = (
    "适用现象",
    "候选原因",
    "建议证据",
    "如何区分",
    "安全恢复边界",
    "恢复后验证",
)


def test_catalog_contains_exactly_the_approved_thirty_cards() -> None:
    actual = {path.name for path in KNOWLEDGE.glob("*.md")}

    assert actual == EXPECTED_CARD_FILENAMES
    assert len(actual) == 30


def test_catalog_audit_is_content_free_and_excludes_governance_chunks(tmp_path: Path) -> None:
    root = tmp_path / "cards"
    root.mkdir()
    for filename in ("a.md", "b.md"):
        (root / filename).write_text(_valid_card("secret body"), encoding="utf-8")

    report = audit_catalog(root)

    assert report["totalDocuments"] == 2
    raw_documents = report["documents"]
    assert isinstance(raw_documents, list)
    documents = cast(list[dict[str, object]], raw_documents)
    assert set(documents[0]) == {"filename", "chunkCount", "headingPaths", "reviewRequired"}
    serialized = json.dumps(report, ensure_ascii=False)
    assert "secret body" not in serialized
    assert "来源" not in serialized
    assert "验证状态" not in serialized
    assert documents[0]["chunkCount"] == 6
    assert documents[0]["reviewRequired"] is False


def test_catalog_audit_rejects_missing_operational_heading(tmp_path: Path) -> None:
    root = tmp_path / "cards"
    root.mkdir()
    text = _valid_card("body").replace("## 恢复后验证\nbody\n", "")
    (root / "invalid.md").write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="恢复后验证"):
        audit_catalog(root)


def test_catalog_audit_rejects_more_than_twelve_chunks(tmp_path: Path) -> None:
    root = tmp_path / "cards"
    root.mkdir()
    extras = "\n".join(f"\n### 额外证据 {index}\nbody" for index in range(7))
    text = _valid_card("body").replace("\n\n## 来源", f"{extras}\n\n## 来源")
    (root / "invalid.md").write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="12"):
        audit_catalog(root)


def _valid_card(body: str) -> str:
    sections = "\n\n".join(f"## {heading}\n{body}" for heading in OPERATIONAL_HEADINGS)
    return (
        f"# Test\n\n{sections}\n\n## 来源\n{body}\n\n"
        "## 验证状态\ncontent_type: agentpy-original-summary\n"
        "docker_validation: pending\n"
    )
