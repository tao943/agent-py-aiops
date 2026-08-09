# Nginx 入口限流网关 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在当前本机前后端运行模式前增加 Compose 管理的 Nginx API 网关，提供分级 IP 限流、SSE 兼容代理、上传兼容和确定性 CI 验证。

**Architecture:** Vue/Vite 仍在宿主机 `127.0.0.1:5173` 运行，但把 API/SSE 请求发往 `127.0.0.1:8080`。Compose 中的官方 `nginx:1.30-alpine` 通过 `host.docker.internal:8000` 代理宿主机 FastAPI，并按普通 API、认证和流式建连三个区域限流；FastAPI 现有 Redis Token Bucket 保持用户级资源限流职责。

**Tech Stack:** Nginx Open Source 1.30 Alpine、Docker Compose、FastAPI、Vue/Vite、OpenSpec CLI、GitHub Actions、Python/pytest、VitePress。

## Global Constraints

- 所有任务在当前会话内联执行，不启动多 Agent。
- 后端、前端和 CLS MCP Server 继续作为宿主机进程运行；Compose 不构建应用镜像。
- Nginx 只发布 `127.0.0.1:8080:80`，上游固定为 `host.docker.internal:8000`，Linux 使用 `host-gateway`。
- 普通 API 为每 IP `20 r/s`、`burst=40`；登录/注册为每 IP `10 r/m`、`burst=5`；Chat/AIOps SSE 建连为每 IP `5 r/s`、`burst=10`。
- 超过 Nginx 限制返回 HTTP 429；不把网关错误伪装成 FastAPI JSON envelope。
- `/health`、`/ready` 和 Nginx 自身健康端点不受请求限流；`/metrics` 走普通 API 限流。
- SSE 必须使用 HTTP/1.1、关闭代理缓冲和缓存、读取超时 600 秒。
- `client_max_body_size` 固定为 `12m`，后端继续执行 10 MB 文件内容限制。
- 日志不得记录 Authorization、Cookie、请求体或展开后的查询参数。
- 不实现 HTTPS、多实例负载均衡、故障实验室、自动评分或 Agent Eval。
- 只使用官方 Nginx 核心模块，不增加 Python/Node 运行依赖或 Docker Socket 权限。

---

### Task 1: 建立 OpenSpec 变更与 WIKI 页面

**Files:**
- Create: `openspec/changes/add-nginx-rate-limit-gateway/.openspec.yaml`
- Create: `openspec/changes/add-nginx-rate-limit-gateway/proposal.md`
- Create: `openspec/changes/add-nginx-rate-limit-gateway/design.md`
- Create: `openspec/changes/add-nginx-rate-limit-gateway/tasks.md`
- Create: `openspec/changes/add-nginx-rate-limit-gateway/specs/edge-rate-limit-gateway/spec.md`
- Create: `openspec/changes/add-nginx-rate-limit-gateway/specs/docker-compose-startup/spec.md`
- Create/update via wiki-sync: `docs/changes/active/add-nginx-rate-limit-gateway/index.md`
- Modify via wiki-sync: `docs/changes/index.md`
- Modify via wiki-sync: `docs/.vitepress/config.mts`

**Interfaces:**
- Consumes: 已批准设计 `docs/superpowers/specs/2026-08-09-nginx-rate-limit-gateway-design.md`。
- Produces: OpenSpec change `add-nginx-rate-limit-gateway`，定义入口、限流、SSE、上传、Compose 边界和验收场景。

- [ ] **Step 1: 创建 OpenSpec change 骨架**

Run:

```powershell
& 'C:\Users\86135\AppData\Roaming\npm\openspec.ps1' new change add-nginx-rate-limit-gateway
```

Expected: `openspec/changes/add-nginx-rate-limit-gateway/` 存在，schema 为 `spec-driven`。

- [ ] **Step 2: 写入提案、设计、任务和 delta specs**

delta spec 必须至少包含以下可验证需求：

```markdown
## ADDED Requirements

### Requirement: Tiered edge request limits
系统 SHALL 在 FastAPI 前提供 Nginx 网关，并 SHALL 按客户端 IP 分别限制普通 API、认证入口和 Agent SSE 建连；超限请求 MUST 返回 HTTP 429。

#### Scenario: Authentication requests exceed the gateway allowance
- **WHEN** 同一客户端 IP 对 `/auth/login` 或 `/auth/register` 超过每分钟 10 次及 burst 5 的允许范围
- **THEN** Nginx MUST 返回 HTTP 429，且请求 MUST NOT 到达 FastAPI

### Requirement: Streaming and upload compatibility
网关 SHALL 禁用 Chat/AIOps SSE 的代理缓冲与缓存，使用 600 秒读取超时，并 SHALL 接受含 multipart 开销的 10 MB 文档上传。

#### Scenario: Agent stream remains live through the gateway
- **WHEN** 客户端通过网关建立 Chat 或 AIOps SSE
- **THEN** Nginx MUST 按事件转发响应而不聚合缓冲
```

`docker-compose-startup` delta 同时声明 Nginx 是基础设施入口服务，而 backend、frontend、CLS MCP 仍不进入 Compose。

- [ ] **Step 3: 验证 OpenSpec 在实现前能够解析**

Run:

```powershell
& 'C:\Users\86135\AppData\Roaming\npm\openspec.ps1' validate add-nginx-rate-limit-gateway --strict
```

Expected: change validation succeeds；若当前 CLI 不支持 `--strict`，改用 `validate add-nginx-rate-limit-gateway`，不得跳过验证。

- [ ] **Step 4: 使用仓库 wiki-sync 同步 active 页面**

Run:

```powershell
python .codex/skills/wiki-sync/scripts/sync_wiki.py active add-nginx-rate-limit-gateway
```

Expected: active 页面、索引和 Sidebar 同步，所有 `@include` 目标存在。

- [ ] **Step 5: 提交规格**

```bash
git add openspec/changes/add-nginx-rate-limit-gateway docs/changes/active/add-nginx-rate-limit-gateway docs/changes/index.md docs/.vitepress/config.mts
git commit -m "spec: define nginx rate limit gateway"
```

---

### Task 2: 接入 Nginx 配置和 Compose 服务

**Files:**
- Create: `infra/nginx/default.conf`
- Create: `infra/nginx/proxy-common.conf`
- Modify: `infra/compose.yaml`

**Interfaces:**
- Consumes: 宿主机 FastAPI `127.0.0.1:8000`。
- Produces: Nginx 入口 `http://127.0.0.1:8080`、自身健康端点 `/nginx-health`、三类限流区和 SSE 代理。

- [ ] **Step 1: 先运行缺失服务的失败验证**

Run:

```powershell
docker compose -f infra/compose.yaml config --services
```

Expected: 输出中没有 `nginx`，证明新增能力尚未实现。

- [ ] **Step 2: 创建最小完整 Nginx 配置**

创建 `infra/nginx/proxy-common.conf`：

```nginx
proxy_http_version 1.1;
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Request-ID $http_x_request_id;
proxy_set_header Connection "";
```

创建 `infra/nginx/default.conf`，核心内容如下：

```nginx
limit_req_zone $binary_remote_addr zone=api_per_ip:10m rate=20r/s;
limit_req_zone $binary_remote_addr zone=auth_per_ip:10m rate=10r/m;
limit_req_zone $binary_remote_addr zone=stream_per_ip:10m rate=5r/s;

log_format gateway_json escape=json
    '{"time":"$time_iso8601","client":"$remote_addr",'
    '"method":"$request_method","uri":"$uri","status":$status,'
    '"requestTime":$request_time,"upstreamStatus":"$upstream_status",'
    '"upstreamTime":"$upstream_response_time",'
    '"requestId":"$upstream_http_x_request_id",'
    '"limitStatus":"$limit_req_status"}';

upstream agent_py_backend {
    server host.docker.internal:8000;
    keepalive 16;
}

server {
    listen 80;
    server_name _;
    client_max_body_size 12m;
    limit_req_status 429;
    access_log /var/log/nginx/access.log gateway_json;

    location = /nginx-health {
        access_log off;
        default_type text/plain;
        return 200 "ok\n";
    }

    location = /health {
        include /etc/nginx/includes/proxy-common.conf;
        proxy_pass http://agent_py_backend;
    }

    location = /ready {
        include /etc/nginx/includes/proxy-common.conf;
        proxy_pass http://agent_py_backend;
    }

    location = /auth/login {
        limit_req zone=auth_per_ip burst=5 nodelay;
        include /etc/nginx/includes/proxy-common.conf;
        proxy_pass http://agent_py_backend;
    }

    location = /auth/register {
        limit_req zone=auth_per_ip burst=5 nodelay;
        include /etc/nginx/includes/proxy-common.conf;
        proxy_pass http://agent_py_backend;
    }

    location ~ ^/(chat/sessions/[^/]+/messages:stream|aiops/diagnostics/[^/]+:stream)$ {
        limit_req zone=stream_per_ip burst=10 nodelay;
        include /etc/nginx/includes/proxy-common.conf;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 600s;
        add_header X-Accel-Buffering no always;
        proxy_pass http://agent_py_backend;
    }

    location / {
        limit_req zone=api_per_ip burst=40 nodelay;
        include /etc/nginx/includes/proxy-common.conf;
        proxy_pass http://agent_py_backend;
    }
}
```

- [ ] **Step 3: 增加 Compose Nginx 服务**

在 `infra/compose.yaml` 的 `services` 下增加：

```yaml
  nginx:
    image: nginx:1.30-alpine
    ports:
      - "127.0.0.1:8080:80"
    volumes:
      - ./nginx/default.conf:/etc/nginx/conf.d/default.conf:ro
      - ./nginx/proxy-common.conf:/etc/nginx/includes/proxy-common.conf:ro
    extra_hosts:
      - "host.docker.internal:host-gateway"
    healthcheck:
      test: ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1/nginx-health"]
      interval: 5s
      timeout: 5s
      retries: 5
    restart: unless-stopped
```

- [ ] **Step 4: 验证 Compose 和真实 Nginx 语法**

Run:

```powershell
docker compose -f infra/compose.yaml config
docker compose -f infra/compose.yaml run --rm --no-deps nginx nginx -t
```

Expected: Compose config exits 0；Nginx 输出 `syntax is ok` 和 `test is successful`。

- [ ] **Step 5: 提交网关运行资产**

```bash
git add infra/nginx/default.conf infra/nginx/proxy-common.conf infra/compose.yaml
git commit -m "feat: add nginx rate limit gateway"
```

---

### Task 3: 将本地入口和启动器切换到网关

**Files:**
- Modify: `config/project.template.json`
- Modify locally, do not commit: `config/project.json`
- Modify: `scripts/start-local.sh`
- Modify: `scripts/start-local.bat`

**Interfaces:**
- Consumes: Compose `nginx` 服务和端口 8080。
- Produces: 前端与 AIOps 演示脚本默认使用 `http://127.0.0.1:8080`；启动器自动启动并报告网关。

- [ ] **Step 1: 写入跟踪配置模板**

在 `config/project.template.json` 中精确修改：

```json
"frontend": {
  "apiBaseUrl": "http://127.0.0.1:8080"
},
"aiopsDemo": {
  "backendBaseUrl": "http://127.0.0.1:8080"
}
```

保留 `backend.host=127.0.0.1`、`backend.port=8000`；`config/project.test.json` 继续直连测试后端，不切换到 Nginx。

- [ ] **Step 2: 同步当前开发机的忽略配置**

只修改 `config/project.json` 中相同两个 URL，不读取、输出或提交 `config/user.project.json` 的密钥字段。运行 `git check-ignore config/project.json`，Expected: 文件仍被 `.gitignore` 忽略。

- [ ] **Step 3: 更新跨平台启动器**

将两个启动器的 Compose 服务列表从：

```text
etcd minio milvus attu alertmanager
```

改为：

```text
etcd minio milvus attu alertmanager nginx
```

最终输出必须包含：

```text
前端：       http://127.0.0.1:5173
API 网关：   http://127.0.0.1:8080
后端直连：   http://127.0.0.1:8000（仅本机调试）
```

- [ ] **Step 4: 验证脚本没有泄漏凭据或切换应用容器边界**

Run:

```powershell
rg -n "nginx|8080|8000" scripts/start-local.sh scripts/start-local.bat config/project.template.json
docker compose -f infra/compose.yaml config --services
git status --short --ignored config/project.json
```

Expected: Nginx 出现在服务列表；模板和启动器使用 8080；`config/project.json` 显示 ignored；Compose 仍无 backend、frontend、cls-mcp-server。

- [ ] **Step 5: 提交入口切换**

```bash
git add config/project.template.json scripts/start-local.sh scripts/start-local.bat
git commit -m "feat: route local api traffic through nginx"
```

---

### Task 4: 为网关加入路径感知 CI

**Files:**
- Modify: `scripts/ci/detect_changes.py`
- Modify: `apps/backend/tests/test_ci_tooling.py`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `infra/compose.yaml` 和 `infra/nginx/default.conf`。
- Produces: `gateway=true|false` 变更输出、`gateway` CI job，以及纳入 `CI Gate` 的确定性配置检查。

- [ ] **Step 1: 先写失败的 CI 工具测试**

扩展 `test_ci_change_detection_classifies_known_areas`：

```python
assert _run_change_detection(tmp_path, ["infra/nginx/default.conf"]) == {
    "backend": "true",
    "frontend": "false",
    "docs_spec": "false",
    "gateway": "true",
}
```

所有现有预期字典增加 `gateway`；全量预期为 true，普通 backend/frontend/docs 变更的 gateway 为 false。Workflow 测试的 job 集合增加 `gateway`，required tokens 增加：

```python
"docker compose -f infra/compose.yaml config",
"docker compose -f infra/compose.yaml run --rm --no-deps nginx nginx -t",
```

- [ ] **Step 2: 运行测试确认失败**

Run from `apps/backend`:

```powershell
uv run pytest tests/test_ci_tooling.py -q
```

Expected: FAIL，因为输出尚无 `gateway` 且 workflow 尚无 gateway job。

- [ ] **Step 3: 扩展变更检测接口**

`ChangeAreas` 增加 `gateway: bool`，`all()` 返回四项 true，`_write_outputs()` 增加
`gateway=...`。分类规则满足：

```python
GATEWAY_PREFIXES = ("infra/nginx/",)
GATEWAY_FILES = {"infra/compose.yaml"}
```

`infra/nginx/*` 和 `infra/compose.yaml` 同时保持 backend=true，因为它们仍影响后端集成基础设施。

- [ ] **Step 4: 增加 gateway workflow job**

在 `changes.outputs` 暴露 gateway，并加入：

```yaml
  gateway:
    needs: changes
    if: needs.changes.outputs.gateway == 'true'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - run: docker compose -f infra/compose.yaml config
      - run: docker compose -f infra/compose.yaml run --rm --no-deps nginx nginx -t
```

`ci-gate.needs` 增加 `gateway`，保持现有 failure/cancelled 聚合规则。

- [ ] **Step 5: 运行聚焦测试和静态质量检查**

Run from `apps/backend`:

```powershell
uv run pytest tests/test_ci_tooling.py -q
uv run ruff check tests/test_ci_tooling.py ../../scripts/ci/detect_changes.py
uv run pyright tests/test_ci_tooling.py ../../scripts/ci/detect_changes.py
```

Expected: 全部通过。

- [ ] **Step 6: 提交 CI 保护**

```bash
git add scripts/ci/detect_changes.py apps/backend/tests/test_ci_tooling.py .github/workflows/ci.yml
git commit -m "ci: validate nginx gateway configuration"
```

---

### Task 5: 更新运行文档并完成端到端验收

**Files:**
- Modify: `README.md`
- Modify: `infra/README.md`
- Modify: `openspec/changes/add-nginx-rate-limit-gateway/tasks.md`
- Archive via CLI: `openspec/changes/add-nginx-rate-limit-gateway/`
- Update via archive/wiki-sync: `openspec/specs/edge-rate-limit-gateway/spec.md`
- Update via archive/wiki-sync: `openspec/specs/docker-compose-startup/spec.md`
- Update via archive/wiki-sync: `docs/changes/archive/2026-08-09-add-nginx-rate-limit-gateway/index.md`
- Modify via archive/wiki-sync: `docs/changes/index.md`
- Modify via archive/wiki-sync: `docs/.vitepress/config.mts`

**Interfaces:**
- Consumes: 已实现网关、启动器、配置模板和 CI job。
- Produces: 可复现运维说明、运行验收证据、已归档且同步到主规格的 OpenSpec change。

- [ ] **Step 1: 更新根 README 和基础设施指南**

文档必须明确：

```text
前端：http://127.0.0.1:5173
API 网关：http://127.0.0.1:8080
后端直连：http://127.0.0.1:8000（仅本机调试）
```

同时解释 Nginx IP 级入口限流与 FastAPI Redis 用户级 Token Bucket 的职责差异、三个限流参数、SSE 无缓冲、12m 网关请求体、429/502/504 排查及以下验证命令：

```powershell
docker compose -f infra/compose.yaml ps nginx
docker compose -f infra/compose.yaml logs nginx
curl.exe http://127.0.0.1:8080/health
```

- [ ] **Step 2: 启动网关并验证健康代理**

确保本机 FastAPI 已在 8000 启动，然后运行：

```powershell
docker compose -f infra/compose.yaml up -d nginx
docker compose -f infra/compose.yaml ps nginx
curl.exe -i http://127.0.0.1:8080/nginx-health
curl.exe -i http://127.0.0.1:8080/health
```

Expected: Nginx healthy；两个请求均为 HTTP 200；`/health` 保留 FastAPI 的 `X-Request-ID`。

- [ ] **Step 3: 验证普通 API 和认证限流**

使用 PowerShell 并发请求普通健康以外的轻量 API（例如 `/config/check`），统计状态码：

```powershell
$codes = 1..100 | ForEach-Object -Parallel {
  curl.exe -s -o NUL -w "%{http_code}" http://127.0.0.1:8080/config/check
} -ThrottleLimit 50
$codes | Group-Object | Select-Object Name,Count
```

Expected: 同一短窗口内同时出现正常响应和 429。认证分钟级限制用无效登录负载验证，禁止注册真实用户；短窗口请求应出现 401/422 等应用响应和 Nginx 429，且 Nginx 日志包含 `REJECTED`。

- [ ] **Step 4: 验证 SSE 配置和上传边界**

Run:

```powershell
docker compose -f infra/compose.yaml exec nginx nginx -T
```

Expected: 流式 location 含 `proxy_buffering off`、`proxy_cache off`、`proxy_read_timeout 600s`；server 含 `client_max_body_size 12m`。使用现有认证流程进行一次 Chat 或 AIOps 流式请求，确认首个 SSE 事件无需等待任务结束即可收到。上传一个小型 Markdown 文档通过网关，确认 multipart 正常；不创建接近 10 MB 的持久测试文档。

- [ ] **Step 5: 运行全量确定性验证**

```powershell
docker compose -f infra/compose.yaml config
docker compose -f infra/compose.yaml run --rm --no-deps nginx nginx -t
& 'C:\Users\86135\AppData\Roaming\npm\openspec.ps1' validate --all
npm run docs:build
cd apps/backend
uv run ruff check .
uv run pyright
uv run pytest
```

Expected: 全部退出 0。真实模型测试继续由 `not live_llm` 默认标记排除。

- [ ] **Step 6: 完成任务清单、归档 OpenSpec 并同步 WIKI**

所有验收通过后将 change tasks 全部标记 `[x]`，然后运行：

```powershell
& 'C:\Users\86135\AppData\Roaming\npm\openspec.ps1' archive add-nginx-rate-limit-gateway --yes
python .codex/skills/wiki-sync/scripts/sync_wiki.py archive add-nginx-rate-limit-gateway
& 'C:\Users\86135\AppData\Roaming\npm\openspec.ps1' validate --all
npm run docs:build
```

Expected: change 移至 `openspec/changes/archive/2026-08-09-add-nginx-rate-limit-gateway`，delta requirements 合并到主规格，WIKI active 页面移除且 archive 页面 include 全部有效。

- [ ] **Step 7: 提交文档和归档**

```bash
git add README.md infra/README.md openspec docs/changes docs/.vitepress/config.mts
git commit -m "docs: document nginx gateway operations"
```

- [ ] **Step 8: 最终工作区检查**

```powershell
git diff --check
git status --short
```

Expected: 无未提交的跟踪文件；只允许 `config/project.json` 作为被忽略的本机配置变化存在。
