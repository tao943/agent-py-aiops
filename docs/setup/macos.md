# macOS 安装指南

本指南用于在 macOS 上完整安装并启动 Agent Py。应用服务在本机运行，Docker 运行 PostgreSQL、etcd、MinIO、Milvus、Attu 和 Alertmanager。

## 1. 安装基础工具

安装 Homebrew 后执行：

```bash
brew install git node uv
brew install --cask docker
```

启动 Docker Desktop，并确认命令可用：

```bash
git --version
node --version
npm --version
uv --version
docker --version
```

## 2. 安装官方 CLS MCP Server

```bash
npm install -g cls-mcp-server@1.0.4
cls-mcp-server --help
```

## 3. 获取并配置项目

```bash
git clone <你的私有仓库地址> agent_py
cd agent_py
```

从安全模板创建被 Git 忽略的本地配置：

```bash
cp config/project.template.json config/project.json
cp config/user.project.template.json config/user.project.json
```

在本地配置中填写或核对 Qwen API Key、CLS `secretId`/`secretKey`、`region`、`logsetId`、`topicId` 和本机地址。字段说明见[配置与运维教程](../operations-and-monitoring.md)。

## 4. 启动 PostgreSQL 后再启动应用

`start-local.sh` 不会启动 PostgreSQL。先启动数据库，并确认 `ps` 输出中的状态为 `healthy`：

```bash
docker compose -f infra/compose.yaml up -d postgres
docker compose -f infra/compose.yaml ps postgres
./scripts/start-local.sh
```

首次启动会安装项目依赖、执行 PostgreSQL Alembic 迁移、启动其余基础设施容器，并在本机启动 MCP、后端和前端。打开 `http://127.0.0.1:5173` 开始使用。
