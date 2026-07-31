"""Redis Streams publisher for durable background-job Outbox records."""

from __future__ import annotations

import hashlib
import json
import re
from typing import cast

from redis.asyncio import Redis

from super_ai.memory.repositories import OutboxEventRecord
from super_ai.redis_runtime.config import RedisRuntimeSettings

_PUBLISH_SCRIPT = """
if redis.call('EXISTS', KEYS[2]) == 1 then
    return false
end
local stream_id = redis.call('XADD', KEYS[1], 'MAXLEN', '~', ARGV[2], '*',
    'event_id', ARGV[3],
    'owner_id_hash', ARGV[4],
    'job_id', ARGV[5],
    'sequence', ARGV[6],
    'event_type', ARGV[7],
    'payload', ARGV[8],
    'created_at', ARGV[9])
redis.call('SET', KEYS[2], '1', 'EX', ARGV[1])
return stream_id
"""

_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "apikey",
        "api_key",
        "authorization",
        "access_key_id",
        "client_secret",
        "credential",
        "cookie",
        "header",
        "password",
        "private_key",
        "secret",
        "secret_id",
        "secret_key",
        "token",
    }
)


class RedisStreamJobEventPublisher:
    """Publish deduplicated, redacted Outbox events to one bounded Redis stream."""

    def __init__(self, client: Redis, settings: RedisRuntimeSettings) -> None:
        self._client = client
        self._stream_key = f"{settings.stream_prefix}:aiops:events"
        self._dedupe_prefix = f"{self._stream_key}:dedupe"
        self._stream_maxlen = settings.stream_maxlen
        self._dedupe_ttl_seconds = settings.event_dedupe_ttl_seconds

    async def publish(self, event: OutboxEventRecord) -> None:
        """Atomically deduplicate and append a sanitized event to Redis Streams."""
        event_type = event.event_type.strip()
        if not event_type:
            raise ValueError("event_type must be a non-empty string")
        payload = json.dumps(
            _redact_payload(event.payload),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        await self._client.eval(
            _PUBLISH_SCRIPT,
            2,
            self._stream_key,
            f"{self._dedupe_prefix}:{event.id}",
            self._dedupe_ttl_seconds,
            self._stream_maxlen,
            event.id,
            hashlib.sha256(event.owner_user_id.encode()).hexdigest(),
            event.aggregate_id,
            event.sequence,
            event_type,
            payload,
            event.created_at.isoformat(),
        )


def _redact_payload(value: object, *, field_name: str | None = None) -> object:
    """Recursively replace credential and HTTP-header values before transport."""
    if field_name is not None and _is_sensitive_field(field_name):
        return "[REDACTED]"
    if isinstance(value, dict):
        typed_mapping = cast(dict[str, object], value)
        return {
            key: _redact_payload(item, field_name=key) for key, item in typed_mapping.items()
        }
    if isinstance(value, list):
        typed_list = cast(list[object], value)
        return [_redact_payload(item) for item in typed_list]
    return value


def _is_sensitive_field(field_name: str) -> bool:
    normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", field_name).lower().replace("-", "_")
    if normalized in _SENSITIVE_FIELD_NAMES:
        return True
    return any(
        part in normalized
        for part in (
            "authorization",
            "credential",
            "cookie",
            "header",
            "password",
            "secret",
            "token",
        )
    )
