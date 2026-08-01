# GitHub Actions 第一版 CI 设计

## 目标

为 Agent Py 单仓库增加一套轻量、可重复、无真实外部凭据的 GitHub Actions CI。
流水线在面向 `main` 的 Pull Request、推送到 `main` 和手动触发时运行，自动完成
后端质量检查、PostgreSQL/Redis 集成测试、前端与共享契约检查、OpenSpec 校验和
文档构建，并通过一个名称稳定的最终 Gate 支持 GitHub 分支保护。

第一版只实现 CI，不构建或部署生产制品，不调用真实 DashScope、CLS、Milvus、
Prometheus 或 Alertmanager 服务。

## 选择的方案

采用路径感知的分层流水线，而不是所有改动全量运行或只在合并后运行完整测试。
工作流本身始终触发，由首个 Job 计算受影响区域，后续 Job 使用条件表达式决定是否
执行。最终 Gate 始终运行并将实际执行 Job 的失败或取消汇总为失败。

不在工作流触发器上使用顶层 `paths` 过滤。GitHub 对被路径过滤跳过的 required
workflow 可能保留 Pending 状态；始终触发加稳定 Gate 可以避免文档改动或单模块改动
被分支保护错误阻塞。

## 触发、安全与并发

工作流文件为 `.github/workflows/ci.yml`，名称为 `CI`，支持：

- `pull_request`，目标分支为 `main`；
- `push`，目标分支为 `main`；
- `workflow_dispatch`，用于从 GitHub UI 手动验证。

工作流设置 `permissions: contents: read`，不使用 `pull_request_target`，不向普通 CI
注入仓库或环境 secrets。真实模型测试继续由 pytest 的 `live_llm` marker 隔离，不在
此工作流中运行。

并发组按 workflow 和 PR 编号或分支 ref 组成，启用 `cancel-in-progress: true`。同一 PR
有新提交时，旧检查被取消，从而减少无效排队和 Actions 分钟消耗。

## 路径分类

`changes` Job 使用 `actions/checkout` 的完整历史和仓库内的
`scripts/ci/detect_changes.py` 计算三个布尔输出：

- `backend`：`apps/backend/**`、`config/**`、`infra/**` 或影响后端运行的根级文件；
- `frontend`：`apps/frontend/**`、`packages/api-contracts/**`、`package.json` 或
  `package-lock.json`；
- `docs_spec`：`docs/**`、`openspec/**`、根 README 或 OpenSpec 项目材料。

`.github/**`、`AGENTS.md` 和变更检测脚本自身被视为全局变更，三个输出全部为 true。
手动触发、无法确定有效基线或初始 push 时也全部为 true，优先保证正确性。

路径检测脚本只接收 base/head Git SHA，通过参数数组调用 `git diff --name-only`，不把
PR 标题、分支名或其他不可信文本拼接进 shell。分类逻辑实现为纯函数，以便在本地
进行确定性单元测试。

## Job 设计

### `changes`

在 `ubuntu-latest` 上检出 `fetch-depth: 0` 的仓库，通过路径检测脚本输出受影响区域。
它不安装项目依赖，也不访问外部服务。

### `backend-quality`

仅当 `backend` 为 true 时运行：

1. 检出代码；
2. 使用 Python 3.13；
3. 使用固定 commit 的 `astral-sh/setup-uv` v8.1.0，并固定 uv 0.11.32；
4. 按 `apps/backend/uv.lock` 启用 uv 缓存；
5. 在 `apps/backend` 执行 `uv sync --frozen`；
6. 执行 `uv run ruff check .`；
7. 执行 `uv run pyright`。

Python 3.13 与当前已验证的本地测试环境保持一致。最低支持版本矩阵不属于第一版
范围，后续确认 Python 3.10 依赖兼容性后再增加。

### `backend-tests`

仅当 `backend` 为 true 时运行，并声明两个 GitHub Actions service containers：

- PostgreSQL 16，用户 `agent_py`、密码 `agent_py_dev`、默认数据库 `agent_py`，使用
  `pg_isready` health check；
- Redis 7 Alpine，映射 6379 端口，使用 `redis-cli ping` health check。

服务健康后，通过 `psql` 创建独立的 `agent_py_test` 数据库。随后运行
`scripts/ci/prepare_test_config.py`：从可提交模板生成被 Git 忽略的 `project.json` 和
`user.project.json`，保留开发数据库 `/agent_py` 与 Redis `/0` 契约，并仅写入固定的
`offline-test-key` 以及模板内的非真实模型配置。脚本不读取环境变量或 GitHub secrets。

Job 最后在 `apps/backend` 执行 `uv sync --frozen` 和 `uv run pytest`。pytest 默认参数
包含 `-m 'not live_llm'`，因此不会产生真实模型调用。集成测试继续使用已提交的
`config/project.test.json` 中的 PostgreSQL `/agent_py_test` 和 Redis `/15`。

### `frontend`

仅当 `frontend` 为 true 时运行：

1. 使用 Node.js 22 和基于根 `package-lock.json` 的 npm 缓存；
2. 执行 `npm ci`；
3. 执行 `npm run contracts:typecheck`；
4. 执行 `npm --workspace packages/api-contracts run test`；
5. 执行 `npm run frontend:test`；
6. 执行 `npm run frontend:build`。

`frontend:build` 已包含 Vue TypeScript 检查，因此不再单独重复
`frontend:typecheck`，减少重复工作。

### `docs-spec`

仅当 `docs_spec` 为 true 时运行。使用 Node.js 22、npm 缓存和 `npm ci`，然后执行：

```bash
npx --yes @fission-ai/openspec@1.6.0 validate --all
npm run docs:build
```

OpenSpec 固定为 1.6.0，不使用 `latest`。设置 `OPENSPEC_TELEMETRY=0` 和
`DO_NOT_TRACK=1`，保证 CI 不发送遥测。

### `ci-gate`

Job 显示名称固定为 `CI Gate`，依赖上述全部 Job，并使用 `if: always()`。它接受
`success` 与因路径无关而产生的 `skipped`，但任何依赖 Job 为 `failure` 或
`cancelled` 时退出非零。

仓库接入 GitHub 后，只需将 `CI Gate` 配置为 `main` 的 required status check；内部
Job 可以继续拆分或调整而无需频繁修改分支保护规则。

## 测试策略

新增后端测试覆盖 CI 契约和两个脚本：

- 工作流存在且 YAML 可解析；
- 触发器、最小权限和并发取消存在；
- 六个 Job 及稳定 Gate 存在；
- 后端测试 Job 声明 PostgreSQL/Redis、健康检查、离线配置和默认 pytest；
- 工作流不含 `pull_request_target`、DashScope secrets 或 `-m live_llm`；
- 路径分类对后端、前端、文档和全局文件给出正确输出；
- 初始 push、手动运行和无有效基线时选择全部区域；
- 测试配置生成器输出可加载、无真实凭据且数据库/Redis URL 符合现有契约。

实现时遵循 red-green-refactor：先加入契约测试并确认因文件或行为缺失而失败，再添加
脚本和工作流使其通过。最终验证包括后端目标测试、Ruff、Pyright、完整 pytest、前端
type/test/build、Contracts 检查、OpenSpec 和 VitePress 构建。

## 错误处理与可观察性

- 依赖安装使用锁文件严格模式，锁文件漂移直接失败；
- service container 自带健康检查，服务未就绪时 Job 在测试前失败；
- 配置生成器对缺失模板、无效 JSON 或输出路径错误返回非零；
- 变更检测无法获得有效 Git 基线时选择全量运行，而不是错误跳过检查；
- 每条检查使用独立、可读的 step 名称，失败位置可直接在 Actions UI 中识别；
- `CI Gate` 输出每个依赖 Job 的 result，便于判断失败或跳过原因。

## 文档和仓库接入

README 增加 CI 章节，说明触发方式、各 Job、真实模型测试隔离、缓存策略，以及仓库
推送到 GitHub 后将 `CI Gate` 设为 required check 的步骤。第一版不添加状态徽章，
因为当前本地仓库尚未配置远程地址，无法生成稳定的仓库 URL。

## 非目标

第一版明确不包含：

- 自动部署或环境发布；
- Docker 应用镜像构建和镜像仓库推送；
- 真实 DashScope/CLS/MCP/Milvus 调用；
- pytest-xdist 多进程并行；
- 覆盖率阈值；
- CodeQL、依赖审查、SBOM 或镜像扫描；
- Python 多版本和多操作系统矩阵；
- Jenkins、Kubernetes、灰度发布或自动回滚。

## 参考资料

- GitHub Actions workflow syntax: https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
- GitHub job conditions: https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-jobs-with-conditions
- GitHub concurrency: https://docs.github.com/en/actions/concepts/workflows-and-actions/concurrency
- GitHub PostgreSQL service containers: https://docs.github.com/en/actions/tutorials/use-containerized-services/create-postgresql-service-containers
- GitHub secure use: https://docs.github.com/en/actions/reference/security/secure-use
- Astral uv GitHub Actions guide: https://docs.astral.sh/uv/guides/integration/github/
- OpenSpec npm package: https://www.npmjs.com/package/@fission-ai/openspec
