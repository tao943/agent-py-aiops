"""Bounded answer-isolated diagnostics for classified Live failures."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from super_ai.evaluation.live.domain import LiveCheck, LiveFaultObservation

_MAX_ITEMS = 64
_MAX_IDENTIFIER_LENGTH = 80
_MAX_STRING_LENGTH = 256
_MAX_INTEGER = 2**63 - 1
_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,79}$")
_FORBIDDEN_IDENTIFIER_TOKENS = frozenset(
    {
        "apikey",
        "accesskey",
        "secret",
        "secretkey",
        "password",
        "token",
        "oracle",
        "groundtruth",
        "primarycause",
        "answerkey",
        "prompt",
        "chainofthought",
    }
)


@dataclass(frozen=True, slots=True)
class LiveFailureDiagnostics:
    """Validated public check outcomes attached to one classified failure."""

    checks: tuple[LiveCheck, ...]
    safe_facts: tuple[tuple[str, str | int | float | bool], ...]

    @property
    def failed_checks(self) -> tuple[str, ...]:
        return tuple(check.name for check in self.checks if not check.passed)

    @classmethod
    def from_observation(
        cls, observation: LiveFaultObservation
    ) -> LiveFailureDiagnostics | None:
        try:
            _validate_checks(observation.checks)
            _validate_safe_facts(observation.safe_facts)
        except ValueError:
            return None
        return cls(observation.checks, observation.safe_facts)


def _validate_checks(checks: tuple[LiveCheck, ...]) -> None:
    if not 1 <= len(checks) <= _MAX_ITEMS:
        raise ValueError("Live failure diagnostic check count is invalid.")
    names: set[str] = set()
    for check in checks:
        _validate_identifier(check.name)
        _validate_identifier(check.source)
        if check.name in names:
            raise ValueError("Live failure diagnostic check names must be unique.")
        names.add(check.name)


def _validate_safe_facts(
    safe_facts: tuple[tuple[str, str | int | float | bool], ...],
) -> None:
    if len(safe_facts) > _MAX_ITEMS:
        raise ValueError("Live failure diagnostic fact count is invalid.")
    names: set[str] = set()
    for name, value in safe_facts:
        _validate_identifier(name)
        if name in names:
            raise ValueError("Live failure diagnostic fact names must be unique.")
        _validate_scalar(value)
        names.add(name)


def _validate_identifier(value: object) -> None:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError("Live failure diagnostic identifier is invalid.")
    if len(value) > _MAX_IDENTIFIER_LENGTH or _canonical_identifier(value) in (
        _FORBIDDEN_IDENTIFIER_TOKENS
    ):
        raise ValueError("Live failure diagnostic identifier is forbidden.")


def _validate_scalar(value: object) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        if abs(value) > _MAX_INTEGER:
            raise ValueError("Live failure diagnostic integer is out of range.")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Live failure diagnostic float must be finite.")
        return
    if isinstance(value, str):
        if len(value) > _MAX_STRING_LENGTH or any(char in value for char in "\x00\r\n"):
            raise ValueError("Live failure diagnostic string is invalid.")
        return
    raise ValueError("Live failure diagnostic fact must be a JSON scalar.")


def _canonical_identifier(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())
