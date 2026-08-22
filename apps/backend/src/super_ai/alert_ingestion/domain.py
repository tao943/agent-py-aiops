"""Immutable domain values for safe alert ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

AlertDeliveryStatus = Literal["firing", "resolved"]


class AlertPayloadError(ValueError):
    """Raised when an untrusted Alertmanager delivery is invalid."""


@dataclass(frozen=True, slots=True)
class NormalizedAlert:
    """One minimized Alertmanager alert safe for persistence and diagnosis."""

    labels: dict[str, str]
    annotations: dict[str, str]
    starts_at: str | None
    ends_at: str | None
    generator_origin: str | None
    truncated: bool


@dataclass(frozen=True, slots=True)
class AlertmanagerDelivery:
    """A validated and minimized Alertmanager Webhook v4 delivery."""

    status: AlertDeliveryStatus
    receiver: str
    group_key_hash: str
    payload_sha256: str
    external_origin: str | None
    truncated_alerts: int
    alerts: tuple[NormalizedAlert, ...]
    normalized_payload: dict[str, object]
    query: str
    truncated: bool
