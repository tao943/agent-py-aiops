from __future__ import annotations

import pytest

from super_ai.evaluation.live.postgres import (
    PostgresConnectionConfig,
    rollback_transaction_if_connection_open,
    safe_postgres_evidence,
)
from super_ai.evaluation.live.scenarios import validate_run_id


def test_postgres_config_targets_only_live_eval_database() -> None:
    config = PostgresConnectionConfig(
        host="localhost",
        port=5432,
        user="agent_py",
        password="secret-sentinel",
        database="agent_py_live_eval",
    )

    assert config.database == "agent_py_live_eval"
    assert "secret-sentinel" not in repr(config)


def test_run_identity_generates_owned_table_and_application_names() -> None:
    identity = validate_run_id("run-pg-1")

    assert identity.table_name.startswith("lock_target_")
    assert identity.blocker_application_name.endswith(":blocker")
    assert identity.waiter_application_name.endswith(":waiter")


def test_safe_evidence_contains_no_credentials_sql_or_raw_logs() -> None:
    payload = safe_postgres_evidence(
        blocker_pid=101,
        waiter_pid=102,
        waiter_has_lock_event=True,
        blocker_edge_confirmed=True,
        probe_error="QueryCanceledError: secret SQL text",
        docker_log="password=secret-sentinel raw statement UPDATE live_eval.foo",
    )

    serialized = str(payload)
    assert payload["probe"]["errorCategory"] == "query_timeout"
    assert payload["dockerLog"]["categories"] == []
    assert "secret-sentinel" not in serialized
    assert "UPDATE" not in serialized
    assert "raw statement" not in serialized


@pytest.mark.asyncio
async def test_cleanup_skips_rollback_after_backend_termination() -> None:
    class ClosedConnection:
        def is_closed(self) -> bool:
            return True

    class Transaction:
        rollback_called = False

        async def rollback(self) -> None:
            self.rollback_called = True

    transaction = Transaction()

    await rollback_transaction_if_connection_open(ClosedConnection(), transaction)

    assert transaction.rollback_called is False
