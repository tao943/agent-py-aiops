"""Typed, owner-isolated Redis JSON cache primitives."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from typing import Generic, Literal, Protocol, TypeVar, cast

from redis.exceptions import RedisError

LOGGER = logging.getLogger(__name__)
_PURPOSE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_DEFAULT_MAX_VALUE_BYTES = 128 * 1024
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class CacheLookup(Generic[T]):
    """A cache read result that makes unavailable Redis observable to callers."""

    state: Literal["hit", "miss", "degraded"]
    value: T | None


class RuntimeCache(Protocol):
    """Cache boundary for DTO-only JSON storage."""

    async def get_json(self, key: str) -> CacheLookup[dict[str, object]]:
        """Fetch a cached JSON object without raising Redis failures."""
        ...

    async def set_json(self, key: str, value: Mapping[str, object], ttl_seconds: int) -> bool:
        """Store a JSON object, returning whether it was stored."""
        ...

    async def delete(self, key: str) -> None:
        """Delete a cached object without raising Redis failures."""
        ...


class RedisJsonClient(Protocol):
    """The small subset of redis-py used by :class:`RedisJsonCache`."""

    def get(self, key: str) -> Awaitable[str | bytes | None]:
        """Read a value by key."""
        ...

    def set(self, key: str, value: str, *, ex: int) -> Awaitable[bool | None]:
        """Write a value with a seconds TTL."""
        ...

    def delete(self, *keys: str) -> Awaitable[int]:
        """Delete a value by key."""
        ...


def build_cache_key(
    *, purpose: str, owner_id: object, version: object, input_value: object
) -> str:
    """Build a deterministic key without exposing owner or request data."""
    if not _PURPOSE_PATTERN.fullmatch(purpose):
        raise ValueError(
            "Cache purpose must contain only lowercase letters, digits, underscores, and hyphens."
        )
    return ":".join(
        (
            "agent-py",
            "cache",
            purpose,
            _sha256(owner_id),
            _sha256(version),
            _sha256(input_value),
        )
    )


class RedisJsonCache(RuntimeCache):
    """A non-authoritative, JSON-object cache backed by Redis."""

    def __init__(
        self, client: RedisJsonClient, *, max_value_bytes: int = _DEFAULT_MAX_VALUE_BYTES
    ) -> None:
        if max_value_bytes <= 0:
            raise ValueError("max_value_bytes must be positive.")
        self._client = client
        self._max_value_bytes = max_value_bytes

    async def get_json(self, key: str) -> CacheLookup[dict[str, object]]:
        """Return a hit, miss, or degraded result without leaking cached content."""
        try:
            raw = await self._client.get(key)
        except RedisError:
            self._log_degraded(key, "get")
            return CacheLookup(state="degraded", value=None)

        if raw is None:
            return CacheLookup(state="miss", value=None)

        try:
            decoded = cast(object, json.loads(raw))
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            await self.delete(key)
            return CacheLookup(state="miss", value=None)

        if not isinstance(decoded, dict):
            await self.delete(key)
            return CacheLookup(state="miss", value=None)
        return CacheLookup(state="hit", value=cast(dict[str, object], decoded))

    async def set_json(self, key: str, value: Mapping[str, object], ttl_seconds: int) -> bool:
        """Store compact deterministic JSON when it fits the configured byte budget."""
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive.")
        try:
            serialized = _compact_json(value)
        except (OverflowError, TypeError, ValueError):
            self._log_degraded(key, "serialize")
            return False
        if len(serialized.encode("utf-8")) > self._max_value_bytes:
            return False

        try:
            return bool(await self._client.set(key, serialized, ex=ttl_seconds))
        except RedisError:
            self._log_degraded(key, "set")
            return False

    async def delete(self, key: str) -> None:
        """Best-effort deletion used for invalid cache entries and explicit invalidation."""
        try:
            await self._client.delete(key)
        except RedisError:
            self._log_degraded(key, "delete")

    def _log_degraded(self, key: str, operation: str) -> None:
        LOGGER.warning(
            "Redis cache operation degraded.",
            extra={
                "cache_purpose": _purpose_from_key(key),
                "cache_state": "degraded",
                "cache_operation": operation,
            },
        )


def _compact_json(value: Mapping[str, object]) -> str:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _purpose_from_key(key: str) -> str:
    parts = key.split(":", maxsplit=3)
    if len(parts) >= 3 and parts[0] == "agent-py" and parts[1] == "cache":
        purpose = parts[2]
        if _PURPOSE_PATTERN.fullmatch(purpose):
            return purpose
    return "unknown"
