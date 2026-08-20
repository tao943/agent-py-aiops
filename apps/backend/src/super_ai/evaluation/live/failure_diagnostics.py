"""Bounded answer-isolated diagnostics for classified Live failures."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

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

    def to_result_payload(self) -> dict[str, object]:
        return {
            "checkResults": [
                {"name": check.name, "passed": check.passed, "source": check.source}
                for check in self.checks
            ],
            "failedChecks": list(self.failed_checks),
            "safeFacts": dict(self.safe_facts),
        }

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


def validate_serialized_failure_diagnostics(payload: Mapping[str, object]) -> None:
    """Validate optional Live diagnostic fields as one consistent structure."""
    fields = frozenset({"checkResults", "failedChecks", "safeFacts"})
    present = fields.intersection(payload)
    if not present:
        return
    if present != fields:
        raise ValueError("Live failure diagnostic fields must be present together.")

    raw_checks = payload["checkResults"]
    if not isinstance(raw_checks, list) or not 1 <= len(raw_checks) <= _MAX_ITEMS:
        raise ValueError("Live failure diagnostic check results are invalid.")
    check_names: set[str] = set()
    failed_names: list[str] = []
    for raw_check in cast(list[object], raw_checks):
        if not isinstance(raw_check, Mapping) or set(raw_check) != {
            "name",
            "passed",
            "source",
        }:
            raise ValueError("Live failure diagnostic check result is invalid.")
        check = cast(Mapping[str, object], raw_check)
        name = check["name"]
        source = check["source"]
        passed = check["passed"]
        _validate_identifier(name)
        _validate_identifier(source)
        if not isinstance(passed, bool):
            raise ValueError("Live failure diagnostic check result is invalid.")
        typed_name = cast(str, name)
        if typed_name in check_names:
            raise ValueError("Live failure diagnostic check names must be unique.")
        check_names.add(typed_name)
        if not passed:
            failed_names.append(typed_name)

    normalized_failed = normalize_public_failed_checks(payload["failedChecks"])
    if normalized_failed is None or normalized_failed != failed_names:
        raise ValueError("Live failure diagnostic failed checks are inconsistent.")

    raw_facts = payload["safeFacts"]
    if not isinstance(raw_facts, Mapping) or len(raw_facts) > _MAX_ITEMS:
        raise ValueError("Live failure diagnostic safe facts are invalid.")
    for name, value in cast(Mapping[object, object], raw_facts).items():
        _validate_identifier(name)
        _validate_scalar(value)


def normalize_public_failed_checks(value: object) -> list[str] | None:
    """Return a bounded public failed-name list, or omit an invalid value."""
    if not isinstance(value, list) or not 1 <= len(value) <= _MAX_ITEMS:
        return None
    normalized: list[str] = []
    for item in cast(list[object], value):
        try:
            _validate_identifier(item)
        except ValueError:
            return None
        typed_item = cast(str, item)
        if typed_item in normalized:
            return None
        normalized.append(typed_item)
    return normalized


def reject_forbidden_artifact_keys(value: object) -> None:
    """Reject recursively nested answer, reasoning, and credential field names."""
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        for key, item in mapping.items():
            if _canonical_identifier(str(key)) in _FORBIDDEN_IDENTIFIER_TOKENS:
                raise ValueError(f"Evaluation artifact contains forbidden field: {key}")
            reject_forbidden_artifact_keys(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in cast(Sequence[object], value):
            reject_forbidden_artifact_keys(item)


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
