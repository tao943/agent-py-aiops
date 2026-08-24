# Agent Py 代码治理设计

## 目标

在不改变产品能力、公共 API、数据模型、评测口径和运行时架构的前提下，清理能够由证据证明无用的代码、文件、导出和依赖。治理结果应降低维护噪声，同时保持 FastAPI、Vue、LangGraph、MCP、后台任务、Benchmark、RAG 和恢复闭环的现有行为。

本轮采用用户确认的保守方案 A。不会拆分 `diagnostics.py` 等大型核心模块，不做顺手重构，不修改远端分支或 GitHub 仓库设置，也不为了制造差异而强行删除候选项。

## 工作边界

工作从已通过 CI 的 `origin/main` 提交 `ac68239` 创建独立分支 `chore/code-hygiene`。主工作区的本地提交和未跟踪教程不进入该分支。

本轮允许：

- 删除无调用方、无动态入口且有充分验证证据的 Python/TypeScript/Vue 代码。
- 删除无入口、无生成职责且不属于产品资产的文件。
- 删除未使用且不由插件、构建、类型或运行时隐式需要的直接依赖。
- 为确认动态入口或防止回归而增加聚焦测试。
- 同步因实际删除而失效的当前文档或测试断言。

本轮禁止：

- 拆分或重写大型模块、改变模块边界或替换编排框架。
- 改变 API/SSE 契约、数据库 schema、配置字段、评测评分或恢复策略。
- 删除 Alembic migration、Benchmark fixture、ground truth、provenance、RAG 知识卡片或 Live 故障注入器。
- 删除 Repository Protocol、恢复 allowlist、Validator、安全降级、审计和用户隔离逻辑。
- 删除看似未使用但通过 FastAPI、LangGraph、MCP、后台 Job、CLI、字符串注册、反射或文件发现加载的实现。
- 修改、删除任何远端分支，或更改 GitHub 仓库设置。

## 复用评估

### 当前能力

仓库已有 Ruff、strict Pyright、`vue-tsc`、TypeScript、Vitest、pytest 和 GitHub Actions。这些工具是最终正确性门禁，但 Ruff/Pyright 主要发现局部未使用符号和类型问题，不能独立证明跨模块代码、导出或依赖可删除。

### 候选工具

| 工具 | 用途 | 状态与许可证 | 使用结论 |
|---|---|---|---|
| Vulture 2.16 | Python 未使用函数、类、变量和导入线索 | 活跃，MIT | 一次性审计，结果仅作为候选线索 |
| Knip 6.32.2 | JS/TS 未使用文件、导出和依赖线索 | 活跃，ISC | 一次性审计，显式配置 workspace 入口后使用 |
| Deptry 0.25.1 | Python 未使用、缺失和传递依赖线索 | 活跃，MIT | 一次性依赖审计，结合动态导入复核 |
| ts-prune 0.10.3 | TypeScript 未使用导出 | 已归档，MIT | 不采用；维护状态不满足要求 |

选择“包装式一次性使用”：使用固定版本的 `uvx`/`npx` 命令运行前三个工具，不写入 `pyproject.toml`、`package.json` 或锁文件。扫描器不能自动删除内容，也不能作为唯一删除证据。

GitHub 上的社区健康模板、OpenSSF Scorecard 和 dependency-review Action 与本轮代码清理目标不同，仅作仓库治理参考，不引入本 PR。

## 审计流程

### 1. 建立基线

记录分支提交、工作区状态和远端 `main` 的成功 CI。安装锁定依赖后，运行现有质量门禁和与后续候选相关的目标测试。若基线本身失败，先区分环境问题与代码问题；不得把基线失败归因于本轮删除。

### 2. 生成候选清单

分别运行：

- Ruff 与 strict Pyright。
- Vulture 固定版本扫描 `apps/backend/src/super_ai`。
- Deptry 固定版本扫描 `apps/backend`。
- `vue-tsc`、TypeScript 和 Knip 固定版本扫描两个 npm workspace。
- `rg`/`git grep` 检查代码、测试、脚本、CI、Compose、文档和 OpenSpec 引用。

扫描输出保存在工作区临时目录或 PR 说明中，不提交生成报告。任何包含动态框架装饰器、注册表、字符串键、路径发现或条件导入的命中先标记为“需要人工复核”，不能直接删除。

### 3. 五项证据门

每个删除候选必须形成一条审计记录并同时满足：

1. **静态调用证据**：没有 import、re-export、类型引用、模板引用或静态调用方。
2. **动态入口证据**：没有 FastAPI 路由、LangGraph node/edge、MCP tool、后台 Job、CLI、构建入口、反射或字符串注册。
3. **仓库入口证据**：没有测试、CI、npm/uv script、Compose、文档、OpenSpec、Benchmark 或配置入口。
4. **产品处置证据**：存在仍在使用的替代实现，或该能力已从当前产品规格中取消。
5. **回归证据**：删除后目标测试、静态检查和受影响构建全部通过。

任一项不满足即保留，并记录保留原因。扫描器置信度、文件大小、代码年龄和名称相似都不能替代五项证据。

### 4. 最小删除

候选按“单个符号或紧密相关文件组”处理。每组先增加或确认能够覆盖保留行为的测试，再执行最小删除，随后立即运行目标验证。不同子系统的候选不合并成一次大删除，确保每个提交可以独立审阅和回退。

依赖删除还必须确认：

- 不由 extras、build backend、pytest plugin、类型 stub 或命令行工具隐式使用。
- 不通过 `import_module`、entry point 或供应商 SDK 的可选路径加载。
- 更新锁文件后安装与构建成功，且锁文件变化只来自该直接依赖。

## 动态加载风险

本仓库广泛使用动态注册，静态扫描会产生预期误报。以下位置需要人工追踪调用链：

- `apps/backend/src/super_ai/api/app.py` 中的路由和 Job handler 注册。
- LangGraph 的 `StateGraph` node、edge 和条件路由。
- AIOps specialist、tool capability 与恢复工具注册表。
- `import_module` 驱动的 Milvus、检索、聊天和 CLS 适配器。
- Live Eval scenario registry、Benchmark 文件发现和 CLI 入口。
- Vue Router 的异步视图、Pinia store、组件模板引用和共享契约导出。

这些位置的候选默认保留，除非能从注册源到运行入口完整证明不可达。

## 测试与验收

每个删除组至少运行受影响模块的目标测试。分支最终运行：

- 后端 Ruff 与 strict Pyright。
- 与实际删除相关的 pytest 文件；不在本机强制执行耗时全量 pytest。若最终差异只命中前端路径，路径过滤会跳过 PR 的后端任务，因此记录基线 `main` 的成功 Linux/Python 3.13 全量 CI，并在最终分支本地重跑 Ruff 与 strict Pyright。
- 前端与共享契约 typecheck。
- 若前端或共享契约有删除，则运行相关 Vitest 和前端构建。
- 若文档、OpenSpec、Compose 或 CI 入口受影响，则运行对应构建/验证。
- `git diff --check` 和敏感配置路径检查。

验收必须说明：审计了哪些范围、扫描器给出多少候选、哪些候选被删除、哪些因动态入口或证据不足被保留、实际运行了哪些验证。记录基线后端全量 CI、最终分支本地后端质量门禁和 PR 实际选择的远端任务；所有被选择的远端 CI 必须通过后才建议合并。

## 结果形态

允许三种有效结果：

1. 删除一组或多组满足五项证据的无用资产，并提交相应回归证据。
2. 只删除未使用依赖，代码保持不变。
3. 没有候选满足证据门，以“未发现可安全删除代码”结束，不创建制造性差异。

最终 PR 只包含实际代码治理结果和必要测试/文档同步。此设计和后续实施计划属于过程资产，在用户审核和执行完成后从最终分支 tree 移除，但保留在分支提交历史用于追溯。

## 回滚与安全

- 不使用 history rewrite、force push、`reset --hard` 或宽范围递归删除。
- 删除前以 `git ls-files` 和精确路径确认目标位于治理 worktree。
- 不读取、打印或提交本地项目配置和凭据。
- 每组删除使用独立 Conventional Commit；回滚通过普通 revert 完成。
- 若扫描工具安装、网络或兼容性失败，明确报告失败，不把失败描述为“没有候选”。
