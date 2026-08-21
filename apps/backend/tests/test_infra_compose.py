from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
INFRA_DIR = REPO_ROOT / "infra"


def test_compose_declares_only_local_infrastructure_services() -> None:
    compose = _read("compose.yaml")

    for service in (
        "alertmanager:",
        "milvus:",
        "etcd:",
        "minio:",
        "attu:",
    ):
        assert service in compose

    for removed_service in ("\n  backend:\n", "\n  frontend:\n", "\n  cls-mcp-server:\n"):
        assert removed_service not in compose

    assert "agent-py-app:local" not in compose
    assert "dockerfile: infra/app.Dockerfile" not in compose
    assert "env_file:" not in compose
    assert "${" not in compose


def test_compose_configures_milvus_stack_without_mcp_runtime() -> None:
    compose = _read("compose.yaml")

    for expected in (
        '"19530:19530"',
        '"9091:9091"',
        '"9000:9000"',
        '"9001:9001"',
        '"8001:3000"',
    ):
        assert expected in compose

    assert "etcd:" in compose
    assert "minio:" in compose
    assert "condition: service_healthy" in compose
    assert "milvusdb/milvus:" in compose
    assert "zilliz/attu:" in compose
    assert "cls-mcp-server" not in compose


def test_compose_configures_local_alertmanager_without_a_full_monitoring_stack() -> None:
    compose = _read("compose.yaml")

    assert "alertmanager:" in compose
    assert "prom/alertmanager:v0.28.1" in compose
    assert '"9093:9093"' in compose
    assert "./alertmanager/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro" in compose
    assert (INFRA_DIR / "alertmanager" / "alertmanager.yml").is_file()


def test_compose_excludes_external_observability_and_upload_startup() -> None:
    compose = _read("compose.yaml").lower()

    for excluded in (
        "prometheus:",
        "grafana:",
        "jaeger:",
        "loki:",
        "elasticsearch:",
        "upload-doc",
        "ingest-document",
    ):
        assert excluded not in compose

    for script_suffix in ("*.sh", "*.bat"):
        assert list(INFRA_DIR.glob(script_suffix)) == []


def test_infrastructure_directory_excludes_application_runtime_assets() -> None:
    assert not (REPO_ROOT / "infra" / "app.Dockerfile").exists()
    assert not (REPO_ROOT / "config" / "project.compose.json").exists()


def test_postgres_live_eval_is_isolated_and_does_not_mount_docker_socket() -> None:
    compose = _read("compose.yaml")
    initialization = (INFRA_DIR / "postgres" / "init" / "001-create-test-database.sql").read_text(
        encoding="utf-8"
    )

    assert "CREATE DATABASE agent_py_live_eval OWNER agent_py;" in initialization
    assert "docker.sock" not in compose.lower()
    assert "./postgres/init:/docker-entrypoint-initdb.d:ro" in compose


def test_live_eval_profile_is_disabled_by_default_and_isolated() -> None:
    compose = _read("compose.yaml")

    for service in (
        "live-eval-redis:",
        "live-eval-upstream:",
        "live-eval-order-api:",
        "live-eval-nginx:",
    ):
        assert service in compose
    assert compose.count('profiles: ["live-eval"]') == 4
    assert '"127.0.0.1:16379:6379"' in compose
    assert '"127.0.0.1:18080:80"' in compose
    assert '"--maxclients", "16"' in compose
    assert "docker.sock" not in compose.lower()
    assert (INFRA_DIR / "live-eval" / "nginx.conf").is_file()
    assert (INFRA_DIR / "live-eval" / "upstream.py").is_file()


def test_compose_configures_isolated_order_api_for_live_eval_only() -> None:
    compose = _read("compose.yaml")

    assert "live-eval-order-api:" in compose
    assert 'profiles: ["live-eval"]' in compose
    assert '"127.0.0.1:18082:8082"' in compose
    assert "dockerfile: live-eval/order-api.Dockerfile" in compose
    assert "POSTGRES_DB: agent_py_live_eval" in compose
    assert "postgres:\n        condition: service_healthy" in compose
    assert "LIVE_ORDER_API_CONTROL_TOKEN: agentpy-live-eval-control" in compose
    assert "docker.sock" not in compose.lower()
    assert (INFRA_DIR / "live-eval" / "order-api.Dockerfile").is_file()


def test_infra_docs_define_manual_docker_live_operation_and_defer_cls() -> None:
    documentation = _read("README.md")

    assert "APY-LIVE-PG-LOCK-001" in documentation
    assert "-m live_docker" in documentation
    assert "agent_py_live_eval" in documentation
    assert "CLS" in documentation and "延后" in documentation


def test_infra_docs_describe_infrastructure_and_local_application_services() -> None:
    infra_readme = _read("README.md")
    root_readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    for service in ("etcd", "MinIO", "Milvus", "Attu", "Alertmanager"):
        assert service in infra_readme
    assert "CLS MCP Server" in root_readme


def _read(name: str) -> str:
    return (INFRA_DIR / name).read_text(encoding="utf-8")
