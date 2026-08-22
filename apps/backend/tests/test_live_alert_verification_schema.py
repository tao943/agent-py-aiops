from __future__ import annotations

from pathlib import Path

from super_ai.memory.models import AlertIncidentModel

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = (
    ROOT
    / "apps"
    / "backend"
    / "alembic"
    / "versions"
    / "202608220002_add_live_alert_verification.py"
)


def test_alert_incident_model_has_live_correlation_and_verification_contract() -> None:
    table = AlertIncidentModel.__table__

    assert {
        "run_id",
        "scenario_id",
        "verification_status",
        "verified_at",
        "verification_summary",
    } <= set(table.columns.keys())
    assert table.c.run_id.type.length == 80
    assert table.c.scenario_id.type.length == 96
    assert table.c.verification_status.type.length == 24
    assert table.c.verification_summary.type.length == 512
    assert any(
        index.name == "ix_aiops_alert_incidents_live_correlation"
        and tuple(column.name for column in index.columns)
        == ("owner_user_id", "source_id", "scenario_id", "run_id")
        for index in table.indexes
    )


def test_live_alert_verification_migration_is_reversible_and_constrained() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "202608220002"' in source
    assert 'down_revision: str | None = "202608220001"' in source
    assert "ck_alert_incidents_verification_status" in source
    assert "ix_aiops_alert_incidents_live_correlation" in source
    for column in (
        "run_id",
        "scenario_id",
        "verification_status",
        "verified_at",
        "verification_summary",
    ):
        assert f'"{column}"' in source
        assert f'op.drop_column("aiops_alert_incidents", "{column}")' in source
