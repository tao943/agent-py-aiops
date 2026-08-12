from pathlib import Path

import pytest

KNOWLEDGE = Path(__file__).resolve().parents[3] / "docs" / "knowledge-candidates"
DIFFERENTIAL_CARDS = ("postgres-pool-exhaustion.md", "redis-unavailable.md")
REQUIRED_HEADINGS = (
    "## 适用现象",
    "## 候选原因",
    "## 建议证据",
    "## 如何区分",
    "## 安全恢复边界",
    "## 恢复后验证",
    "## 来源",
)
FORBIDDEN_TOKENS = (
    "APY-",
    "ground_truth",
    "evidence_id",
    "benchmark_container",
    "slow_transaction_pool_exhaustion",
    "borrowed_connection_not_returned",
    "service_process_stopped",
    "stale_connections_retained_after_recovery",
)


@pytest.mark.parametrize("filename", DIFFERENTIAL_CARDS)
def test_differential_card_has_reviewed_structure_and_no_benchmark_answers(
    filename: str,
) -> None:
    text = (KNOWLEDGE / filename).read_text(encoding="utf-8")

    assert all(heading in text for heading in REQUIRED_HEADINGS)
    assert not any(token in text for token in FORBIDDEN_TOKENS)
    assert "https://" in text
