import re
from pathlib import Path

import pytest

KNOWLEDGE = Path(__file__).resolve().parents[3] / "docs" / "knowledge-candidates"
DIFFERENTIAL_CARDS = tuple(sorted(path.name for path in KNOWLEDGE.glob("*.md")))
REQUIRED_HEADINGS = (
    "## 适用现象",
    "## 候选原因",
    "## 建议证据",
    "## 如何区分",
    "## 安全恢复边界",
    "## 恢复后验证",
    "## 来源",
    "## 验证状态",
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
    "relevant_documents",
    "forbidden_top_one",
    "expected_no_answer",
)
SENSITIVE_FIELD_PATTERN = re.compile(
    r"(?i)\b(ownerUserId|knowledgeBaseId|apiKey|password)\b\s*[:=]"
)


def test_expansion_does_not_create_snapshot_answer_cards() -> None:
    cards = sorted(KNOWLEDGE.glob("*.md"))

    assert len(cards) == 30
    assert not any(card.name.casefold().startswith("apy-") for card in cards)


@pytest.mark.parametrize("filename", DIFFERENTIAL_CARDS)
def test_differential_card_has_reviewed_structure_and_no_benchmark_answers(
    filename: str,
) -> None:
    text = (KNOWLEDGE / filename).read_text(encoding="utf-8")

    assert all(heading in text for heading in REQUIRED_HEADINGS)
    assert not any(token.casefold() in text.casefold() for token in FORBIDDEN_TOKENS)
    assert SENSITIVE_FIELD_PATTERN.search(text) is None
    assert text.count("https://") >= 2
    assert "content_type: agentpy-original-summary" in text
    assert "docker_validation: pending" in text
