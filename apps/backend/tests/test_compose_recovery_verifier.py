from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from super_ai.alert_ingestion.repositories import AlertIncidentRecord
from super_ai.recovery.compose import (
    ComposeContainerIdentity,
    ComposePreflightResult,
    ComposeRecoveryVerifier,
)
from super_ai.recovery.config import ComposeRecoveryTarget, DiagnosticSelector

NOW = datetime(2026, 8, 23, 8, 5, tzinfo=timezone.utc)


def _target() -> ComposeRecoveryTarget:
    return ComposeRecoveryTarget(
        "live-eval-order-api",
        Path("D:/project/infra/compose.yaml"),
        "live-eval-order-api",
        True,
        "http://127.0.0.1:18081/health",
        "http://127.0.0.1:18081/probe",
        DiagnosticSelector("order-api", ("pool_leak",), ("Tool.fact",)),
    )


class Inspector:
    def __init__(self, identity: ComposeContainerIdentity | None) -> None:
        self.identity = identity
        self.calls = 0

    async def preflight(self) -> ComposePreflightResult:
        self.calls += 1
        return ComposePreflightResult(
            self.identity is not None,
            self.identity,
            None if self.identity is not None else "compose_identity_unavailable",
        )


class Probes:
    def __init__(self, results: dict[str, bool]) -> None:
        self.results = results
        self.calls: list[str] = []

    async def succeeded(self, url: str) -> bool:
        self.calls.append(url)
        return self.results[url]


class Incidents:
    def __init__(self, status: str) -> None:
        self.status = status

    async def get_owned(
        self, *, owner_user_id: str, incident_id: str
    ) -> AlertIncidentRecord | None:
        return AlertIncidentRecord(
            incident_id,
            owner_user_id,
            self.status,
            "PoolExhausted",
            "order-api",
            "critical",
            NOW,
            "diagnostic-1",
        )


class ConvergingIncidents(Incidents):
    def __init__(self) -> None:
        super().__init__("active")
        self.calls = 0

    async def get_owned(
        self, *, owner_user_id: str, incident_id: str
    ) -> AlertIncidentRecord | None:
        self.calls += 1
        self.status = "resolved" if self.calls >= 2 else "active"
        return await super().get_owned(
            owner_user_id=owner_user_id,
            incident_id=incident_id,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("after", "health", "business", "incident_status", "expected"),
    [
        (
            ComposeContainerIdentity("bbbbbbbbbbbb", "live-eval-order-api", "started-after"),
            True,
            True,
            "resolved",
            True,
        ),
        (
            ComposeContainerIdentity("aaaaaaaaaaaa", "live-eval-order-api", "started-before"),
            True,
            True,
            "resolved",
            False,
        ),
        (
            ComposeContainerIdentity("aaaaaaaaaaaa", "live-eval-order-api", "started-after"),
            True,
            True,
            "resolved",
            True,
        ),
        (
            ComposeContainerIdentity("bbbbbbbbbbbb", "live-eval-order-api", "started-after"),
            False,
            True,
            "resolved",
            False,
        ),
        (
            ComposeContainerIdentity("bbbbbbbbbbbb", "live-eval-order-api", "started-after"),
            True,
            False,
            "resolved",
            False,
        ),
        (
            ComposeContainerIdentity("bbbbbbbbbbbb", "live-eval-order-api", "started-after"),
            True,
            True,
            "active",
            False,
        ),
    ],
)
async def test_verification_requires_all_four_independent_signals(
    after: ComposeContainerIdentity,
    health: bool,
    business: bool,
    incident_status: str,
    expected: bool,
) -> None:
    target = _target()
    probes = Probes({target.health_url: health, target.business_probe_url: business})
    verifier = ComposeRecoveryVerifier(
        target=target,
        inspector=Inspector(after),
        probes=probes,
        incidents=Incidents(incident_status),  # type: ignore[arg-type]
        now=lambda: NOW,
        verification_timeout_seconds=0,
    )

    result = await verifier.verify(
        owner_user_id="owner-1",
        incident_id="incident-1",
        before=ComposeContainerIdentity(
            "aaaaaaaaaaaa", "live-eval-order-api", "started-before"
        ),
    )

    assert result.passed is expected
    assert [check.key for check in result.checks] == [
        "container_identity_changed",
        "service_health",
        "business_probe",
        "incident_resolved",
    ]
    assert all(check.checked_at == NOW for check in result.checks)
    assert probes.calls == [target.health_url, target.business_probe_url]
    assert "restart" not in result.safe_summary


@pytest.mark.asyncio
async def test_verification_waits_for_independent_incident_resolution() -> None:
    target = _target()
    incidents = ConvergingIncidents()
    verifier = ComposeRecoveryVerifier(
        target=target,
        inspector=Inspector(
            ComposeContainerIdentity(
                "bbbbbbbbbbbb", "live-eval-order-api", "started-after"
            )
        ),
        probes=Probes({target.health_url: True, target.business_probe_url: True}),
        incidents=incidents,
        now=lambda: NOW,
        verification_timeout_seconds=0.02,
        poll_interval_seconds=0.01,
    )

    result = await verifier.verify(
        owner_user_id="owner-1",
        incident_id="incident-1",
        before=ComposeContainerIdentity(
            "aaaaaaaaaaaa", "live-eval-order-api", "started-before"
        ),
    )

    assert result.passed is True
    assert incidents.calls == 2
