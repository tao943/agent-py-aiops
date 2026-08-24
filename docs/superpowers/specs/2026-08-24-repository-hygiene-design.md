# Agent Py 仓库清理设计

## 目标

将 `tao943/agent-py-aiops` 从开发过程档案密集的内部工程仓库，整理成以求职作品集为主要读者的公开项目。清理后，访问者应能在 README 中快速理解项目价值、真实能力、架构、评测方式和启动入口；维护者仍能从精简文档和 `openspec/specs/` 找到当前有效约束。

本次工作只清理当前 Git tree，不改写 Git 历史，不强推，不删除本地配置或运行数据，也不把未验证的能力包装成“生产级”或“全自动恢复”。

## 已确认的实施边界

工作拆成两个独立 PR：

1. `chore/repository-hygiene`：README、许可证、文档信息架构和明确的过程资产清理。
2. 后续独立分支：基于可追溯证据的无用代码清理。

第一阶段从干净的 `origin/main` 创建，不包含本地主分支领先的 benchmark 文档提交，也不包含主工作区中的未跟踪教程。第二阶段只能在第一阶段合并后，从新的 `origin/main` 创建。

## 第一阶段：公开仓库与文档清理

### 根目录结构

长期保留的顶层入口为：

```text
README.md
LICENSE
AGENTS.md
CONTEXT.md
apps/
packages/
benchmarks/
config/
infra/
scripts/
docs/
openspec/specs/
.github/
package.json
package-lock.json
```

明确删除以下已跟踪路径：

```text
.codex/
.superpowers/
docs/changes/
docs/superpowers/
openspec/changes/
openspec从0到1项目实战的提示词.md
```

这些目录保存的是本地 Agent 配置、设计过程、实施计划、报告或 OpenSpec 变更历史，不是产品运行依赖。删除前必须再次用 `git ls-files` 解析精确目标，确认所有目标位于当前 worktree，且不包含未跟踪文件。

`.gitignore` 新增 `.codex/`、`.superpowers/`、`docs/superpowers/` 和 `docs/changes/`，避免本地 Agent 过程资产重新进入版本控制。`openspec/changes/` 不加入忽略规则：后续功能分支仍可使用 OpenSpec change 进行评审，但合并前必须将有效要求同步到 `openspec/specs/`，并从最终 tree 移除 change 目录。

### README

README 控制在约 150–180 行，按以下顺序组织：

1. 一句话定位：可审计、可评测、恢复受治理的本地优先 AIOps Agent 工作台。
2. CI、技术栈和 MIT License 徽章。
3. 一至两张关键界面截图。
4. 端到端闭环：

   ```text
   Alert 或 Chat 入口
   → Single/Multi-Agent 路由
   → CLS/PostgreSQL/Redis/Prometheus/RAG 证据
   → 证据聚合与根因裁决
   → Safety Validator
   → 自动恢复或人工审批
   → 验证与审计
   ```

5. 核心能力：告警/对话入口、证据驱动诊断、受控恢复、RAG、审计链和评测。
6. Snapshot、Retrieval、Conversation 与 Docker Live 四层评测。数字只能来自仓库内可追溯场景、结果或验收文档；无法从当前 tree 验证的数字直接省略，不使用推测值。
7. 紧凑架构与技术栈。
8. 最小 Quickstart；平台配置、CLS、模型和故障实验链接到深层文档。
9. 精选文档索引。
10. 诚实限制，包括本地优先部署、外部服务依赖、自动恢复边界及 Live Eval 成本。
11. MIT License。

README 移除 PostgreSQL、Redis、Nginx 的低层运维说明、重复命令、变更时间线、过时路由和无法验证的宣传性表述。不得声称所有 Live 场景均会执行自动修复；应区分恢复提案、人工审批和白名单内自动执行。

### 文档信息架构

将 `docs/foundation.md` 与 `docs/aiops-workbench.md` 合并为 `docs/architecture.md`。新文档只描述当前系统边界、主要组件、诊断数据流、安全恢复边界和持久化职责，不保留早期“仅基础骨架”描述。

继续保留并在导航中公开：

- `docs/architecture.md`
- `docs/aiops/agentpy-domainbench.md`
- `docs/operations-and-monitoring.md`
- `docs/setup/`
- `docs/runbooks/`
- `docs/tutorials/real-log-and-alert.md`
- `docs/examples/`
- `docs/knowledge-candidates/`

`docs/knowledge-candidates/` 是 RAG 知识资产，不按普通文档冗余处理。`benchmarks/` 中的场景、oracle、provenance 和评测配置同样保留。

把现有四张界面图片移动到 `docs/assets/screenshots/`，使用稳定、无日期的文件名。README 选择事件工作台和 Agent 配置中心中最能表达能力的一至两张，其余图片供架构或使用文档引用。图片移动后必须修复所有相对链接。

重写 `docs/index.md` 与 `docs/.vitepress/config.mts`，使用人工维护的精选导航，不再生成或展示 OpenSpec WIKI、活动 change 和归档 change 列表。VitePress 标题与描述改为 Agent Py AIOps 项目文档。

### 规范与 Agent 指南

`openspec/specs/` 是主分支唯一长期 OpenSpec 事实来源。当前 12 个 active changes 中包含尚未进入主规格的有效 delta requirements，因此删除 `openspec/changes/` 前必须按依赖顺序使用 OpenSpec 的规范合并机制逐项同步；任何一项无法无冲突同步时停止删除，不允许用 `--skip-specs` 丢弃要求。

迁移 change 的首次同步暴露出八个旧 `MODIFIED` 标题与当前主规格名称不一致，同时九个 canonical specs 仍把 SQLite 描述为当前存储。同步前应把迁移 delta 修正为准确的新增、修改和移除操作；同步后以当前 PostgreSQL-only 代码、迁移和测试为依据消除 canonical specs 中的 SQLite 事实漂移，并把零 SQLite 残留作为验收门禁。

当前 `openspec/specs/openspec-wiki/` 与取消 Wiki/历史镜像的设计直接冲突。它将被退役，并由只约束精选 VitePress 文档、人工导航和构建行为的 `openspec/specs/documentation-site/` 取代。同步后运行 `openspec validate --all` 必须只验证主规格且全部通过。

保留 `AGENTS.md` 和 `CONTEXT.md`。更新 `AGENTS.md` 中与仓库现状冲突的部分：

- 仓库结构不再描述活动/归档 change 与 OpenSpec WIKI。
- 事实来源改为当前代码、测试、`openspec/specs/` 和精选文档。
- 删除 `wiki-sync`、`docs/changes/` 镜像和 change 归档要求。
- 保留凭据、权限隔离、迁移、测试和 Git 安全约束。

后端 `test_ci_tooling.py` 当前直接加载将被删除的 `.codex/skills/wiki-sync/scripts/sync_wiki.py`。第一阶段允许一个严格限定的测试兼容性修改：删除该路径常量、专属导入和唯一 wiki-sync 测试；其余 CI、离线配置与启动保护测试保持不变，并运行该文件的 Ruff 与 pytest。

### 许可证与 GitHub 元数据

新增标准 MIT License，版权行使用 `Copyright (c) 2026 tao943`，完整文本使用标准 MIT License。不复制参考项目代码，也不改变现有依赖许可证。

第一阶段 PR 合并后设置 GitHub Topics：

```text
aiops, langgraph, rag, fastapi, vue3, postgresql,
redis, milvus, mcp, sre
```

在文档站或演示站真实发布前不设置 Homepage。

### 复用评估

仓库结构和 README 组织参考 OpenHands、Robusta 与 Netdata 的公开仓库，但只采用信息架构原则，不复制其代码、图像或文案。OpenHands 与 Robusta 使用 MIT，Netdata 使用 GPL-3.0；参考不会引入许可证传播问题。MIT License 文本直接采用 SPDX 标准文本。第一阶段不增加运行时或开发依赖。

## 第二阶段：证据驱动的代码清理

第二阶段不是“按文件数量删代码”。每个候选删除项必须同时满足：

1. 没有 import、re-export 或静态调用方。
2. 没有 FastAPI、LangGraph、后台 Job、MCP 或其他动态注册。
3. 没有 CLI、package script、CI、Compose、文档或测试入口。
4. 存在明确替代实现，或对应能力已被明确取消。
5. 删除后相关 lint、type check、测试和构建全部通过。

Ruff、Pyright 和 Vue TypeScript 检查作为已有直接验证工具。Vulture 与 Knip 只以固定版本的一次性审计命令运行，版本和许可证记录在实施计划/PR 中，不写入项目依赖；它们的结果只是候选线索，不能自动触发删除。

以下资产不能仅因静态扫描未命中而归类为垃圾：

- Alembic migrations。
- Benchmark 场景、ground truth、provenance 和结果契约。
- RAG 知识卡片。
- 测试 fixture 与 Live 故障注入器。
- 配置模板和 API/SSE 契约。
- Repository Protocol。
- 恢复 allowlist、Validator 与审计逻辑。

如果审计没有找到满足五项条件的候选，第二阶段应以“未发现可安全删除代码”结束，不为制造 diff 强行删除。

## 安全、失败与回滚

- 所有工作在隔离 worktree 和功能分支中完成，不直接推送 `main`。
- 不读取、显示或提交 `config/project.json`、`config/user.project.json` 或任何凭据。
- 不使用 history rewrite、force push、`reset --hard` 或面向宽目录的递归删除。
- 每组删除前列出 Git 索引中的精确路径；删除只针对已确认的版本控制资产。
- 若文档构建、OpenSpec 验证或链接检查失败，先修复引用，不通过降低校验或移除有效内容规避。
- PR 合并前可通过关闭 PR 或回退该 PR 的提交恢复，历史保持完整。

## 验收标准

第一阶段必须满足：

- README 中的路由、能力、评测数量和命令均能由当前代码、配置或版本化结果验证。
- README 的所有相对链接和截图路径有效。
- `LICENSE` 为标准 MIT 文本。
- 目标过程目录不再出现在 `git ls-files` 中，保留目录仍存在。
- `.gitignore` 能拦截四类本地过程资产。
- `AGENTS.md` 不再要求 wiki-sync、历史镜像或长期保存 OpenSpec changes。
- 12 个 active changes 的有效 delta requirements 已进入 canonical specs，且 `openspec/changes/` 才被移除。
- `openspec-wiki` 规格已由 `documentation-site` 规格替代，仓库及测试不存在 wiki-sync 残留引用。
- `apps/backend/tests/test_ci_tooling.py` 的剩余测试通过 Ruff 和目标 pytest。
- `npm run docs:build` 成功。
- `openspec validate --all` 成功。
- `git diff --check` 成功。
- GitHub Actions 全部通过。

第二阶段必须为每个删除候选附上五项证据、受影响入口和目标回归结果；若存在任何动态注册或行为不确定性，则保留该代码并记录原因。

## 提交与 PR 策略

设计文档先在 `chore/repository-hygiene` 上单独提交并由用户审阅。用户批准书面规格后，再生成详细实施计划并接受一次只读计划审查。实施提交按“许可证与 README”“文档导航与架构”“过程资产清理与规则更新”分组，便于审阅和回退。

本设计文档位于待清理的 `docs/superpowers/` 中，因此在第一阶段最终实施提交里会随过程资产一起从最终 tree 删除；其决策仍保留在分支提交历史、PR 描述、README、`AGENTS.md` 和规范化文档中。
