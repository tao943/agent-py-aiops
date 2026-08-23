"""Safe serialization and stable identity helpers for durable chat runs."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256
from typing import cast

from super_ai.memory.repositories import JsonDict

_FORBIDDEN_KEYS = frozenset(
    {
        "reasoning",
        "reasoning_content",
        "prompt",
        "secret",
        "password",
        "api_key",
        "apikey",
        "oracle",
        "ground_truth",
    }
)
_MAX_PUBLIC_EVENT_BYTES = 64 * 1024


class PublicRunEventError(ValueError):
    """A persisted event cannot be exposed through the public replay stream."""


def tool_call_key(
    run_id: str,
    logical_step: str,
    tool_name: str,
    arguments: Mapping[str, object],
) -> str:
    canonical = json.dumps(
        arguments,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    material = f"{run_id}\0{logical_step}\0{tool_name}\0{canonical}"
    return sha256(material.encode("utf-8")).hexdigest()


def public_run_event(
    *,
    sequence: int,
    event_type: str,
    payload: Mapping[str, object],
    timestamp: datetime,
) -> JsonDict:
    _validate_public_value(payload)
    event: JsonDict = {
        "id": str(sequence),
        "type": event_type,
        "channel": "chat",
        "timestamp": timestamp.isoformat(),
        **dict(payload),
    }
    encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_PUBLIC_EVENT_BYTES:
        raise PublicRunEventError("public run event exceeds the size limit")
    return event


def encode_run_sse(event: Mapping[str, object]) -> str:
    sequence = str(event["id"])
    event_type = str(event["type"])
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"id: {sequence}\nevent: {event_type}\ndata: {data}\n\n"


def _validate_public_value(value: object) -> None:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        for key, item in mapping.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in _FORBIDDEN_KEYS:
                raise PublicRunEventError(f"forbidden public event field: {normalized}")
            _validate_public_value(item)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in cast(Sequence[object], value):
            _validate_public_value(item)
        return
    if not isinstance(value, str | int | float | bool | None):
        raise PublicRunEventError("public run event contains a non-JSON value")
