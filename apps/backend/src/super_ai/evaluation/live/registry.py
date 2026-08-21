"""Explicit scenario-to-runtime composition for Docker Live evaluation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from super_ai.evaluation.live.cls_evidence import LiveClsRecordProvider
from super_ai.evaluation.live.domain import LiveFaultObservation
from super_ai.evaluation.live.evidence_client import LiveMcpClient
from super_ai.evaluation.live.runner import LiveRecoveryService, LiveScenarioDriver


@dataclass(frozen=True, slots=True)
class LiveScenarioComponents:
    """One fully constructed scenario runtime selected at the CLI boundary."""

    driver_name: str
    driver: LiveScenarioDriver
    recovery: LiveRecoveryService
    component_evidence_factory: Callable[[LiveFaultObservation], LiveMcpClient]
    cls_record_provider: LiveClsRecordProvider | None = None


class LiveScenarioRegistry:
    """Fail-closed registry with no default scenario fallback."""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[[], LiveScenarioComponents]] = {}

    def register(
        self,
        scenario_id: str,
        factory: Callable[[], LiveScenarioComponents],
    ) -> None:
        if scenario_id in self._factories:
            raise ValueError(f"Live scenario is already registered: {scenario_id}.")
        self._factories[scenario_id] = factory

    def resolve(self, scenario_id: str) -> LiveScenarioComponents:
        factory = self._factories.get(scenario_id)
        if factory is None:
            raise ValueError(f"Live scenario is not registered: {scenario_id}.")
        return factory()
