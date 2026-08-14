from typing import Any, cast

import pytest

from super_ai.evaluation.live.registry import (
    LiveScenarioComponents,
    LiveScenarioRegistry,
)


def _components(driver_name: str) -> LiveScenarioComponents:
    return LiveScenarioComponents(
        driver_name=driver_name,
        driver=cast(Any, object()),
        recovery=cast(Any, object()),
        component_evidence_factory=cast(Any, object()),
    )


def test_registry_resolves_each_registered_scenario_without_fallback() -> None:
    registry = LiveScenarioRegistry()
    expected = {
        "APY-LIVE-PG-LOCK-001": "postgres_lock_wait",
        "APY-LIVE-PG-DEADLOCK-001": "postgres_deadlock",
        "APY-LIVE-REDIS-MAXCLIENTS-001": "redis_maxclients",
        "APY-LIVE-NGINX-TIMEOUT-001": "nginx_timeout",
    }
    for scenario_id, driver_name in expected.items():
        registry.register(scenario_id, lambda name=driver_name: _components(name))

    assert {
        scenario_id: registry.resolve(scenario_id).driver_name
        for scenario_id in expected
    } == expected
    with pytest.raises(ValueError, match="not registered"):
        registry.resolve("APY-LIVE-UNKNOWN-001")


def test_registry_rejects_duplicate_registration() -> None:
    registry = LiveScenarioRegistry()
    registry.register("APY-LIVE-PG-LOCK-001", lambda: _components("postgres_lock_wait"))

    with pytest.raises(ValueError, match="already registered"):
        registry.register("APY-LIVE-PG-LOCK-001", lambda: _components("postgres_lock_wait"))
