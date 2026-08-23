"""Deterministic pre-router safety checks for high-risk instruction overrides."""

from __future__ import annotations

import re
from dataclasses import dataclass

PROMPT_INJECTION_SENSITIVE_ACTION = "prompt_injection_sensitive_action"

_OVERRIDE_PATTERNS = (
    re.compile(r"(?:忽略|无视|绕过|覆盖).{0,24}(?:规则|指令|限制|安全策略|系统提示)"),
    re.compile(
        r"\b(?:ignore|disregard|bypass|override)\b.{0,48}"
        r"\b(?:previous|all|system|safety|instruction|instructions|rule|rules)\b"
    ),
)
_SENSITIVE_ACTION_PATTERNS = (
    re.compile(r"(?:立即|直接|马上)?(?:执行|实施|启动).{0,16}(?:恢复|重启|终止|修复)"),
    re.compile(r"(?:显示|泄露|输出|读取|提供).{0,24}(?:api\s*key|密钥|密码|token|secret)"),
    re.compile(r"(?:显示|泄露|输出).{0,24}(?:完整推理|隐藏推理|思维链|系统提示词)"),
    re.compile(r"\b(?:execute|perform|start|restart|terminate|recover)\b.{0,48}"),
    re.compile(
        r"\b(?:reveal|show|expose|read|print|provide)\b.{0,48}"
        r"\b(?:api\s*key|secret|password|token|chain of thought|hidden reasoning|system prompt)\b"
    ),
)


@dataclass(frozen=True, slots=True)
class ChatInputSafetyDecision:
    blocked: bool
    reason_code: str | None = None


def evaluate_chat_input_safety(content: str) -> ChatInputSafetyDecision:
    """Block only an instruction override combined with a sensitive direct action."""

    normalized = " ".join(content.casefold().split())[:4000]
    if _is_bounded_educational_discussion(normalized):
        return ChatInputSafetyDecision(False)
    override = any(pattern.search(normalized) for pattern in _OVERRIDE_PATTERNS)
    sensitive_action = any(
        pattern.search(normalized) for pattern in _SENSITIVE_ACTION_PATTERNS
    )
    if override and sensitive_action:
        return ChatInputSafetyDecision(True, PROMPT_INJECTION_SENSITIVE_ACTION)
    return ChatInputSafetyDecision(False)


def _is_bounded_educational_discussion(content: str) -> bool:
    educational_start = (
        "请解释",
        "为什么",
        "什么是",
        "如何识别",
        "如何防范",
        "explain",
        "why ",
        "what is ",
        "how to detect ",
        "how to prevent ",
    )
    escalation_phrases = (
        "然后",
        "并且",
        "同时",
        "立即",
        "马上",
        "实际执行",
        " and then ",
        " then execute",
        " also execute",
    )
    return (
        content.startswith(educational_start)
        and ("prompt injection" in content or "提示词注入" in content)
        and not any(phrase in content for phrase in escalation_phrases)
    )
