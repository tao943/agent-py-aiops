# Agent Py Repository Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Repository policy prohibits implementation subagents; only the completed plan receives one read-only subagent review. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the public repository into a concise, evidence-backed AIOps portfolio while preserving runtime code, benchmark assets, RAG knowledge cards, canonical specifications, and the user's unrelated local work.

**Architecture:** Perform the public-facing cleanup in three independently reviewable commits: curate documentation and screenshots, replace the README and add MIT licensing, then remove confirmed process artifacts and align repository rules. Keep `openspec/specs/` as the long-lived specification source, verify claims from versioned code/data, and defer all code deletion to a separate evidence-driven PR.

**Tech Stack:** Markdown, VitePress 1.6, OpenSpec CLI 1.6, Git/GitHub Actions, PowerShell, existing Vue 3/FastAPI/LangGraph repository assets. No new dependency.

## Global Constraints

- Work only in `D:\桌面\后端\agent_py-release-2026-07-25\agent_py-release-2026-07-25\.worktrees\repository-hygiene` on branch `chore/repository-hygiene`, based on clean `origin/main` commit `e85ecd5`.
- Do not merge the local `main` commits `0aa24f1`, `ece1a70`, or `88cd651`; do not add any untracked tutorial from the primary worktree.
- Do not read, print, stage, or commit `config/project.json`, `config/user.project.json`, API keys, CLS credentials, passwords, private service addresses, or generated runtime data.
- Do not rewrite Git history, force-push, use `git reset --hard`, or delete outside the exact tracked targets listed in Task 3.
- Preserve `apps/`, `packages/`, `benchmarks/`, `config/`, `infra/`, `scripts/`, `.github/`, `docs/knowledge-candidates/`, `openspec/specs/`, `AGENTS.md`, and `CONTEXT.md`.
- Do not change runtime code, dependencies, database migrations, benchmark fixtures, RAG cards, evaluation scoring, CI behavior, or API contracts in this PR. The only allowed `apps/` edit is removal of the obsolete wiki-sync assertion from `apps/backend/tests/test_ci_tooling.py`, because its target is intentionally deleted in this PR.
- README claims must be supported by current tracked code, fixtures, or versioned acceptance records. Distinguish “versioned scenario” from “recorded real run” and “recovery proposal” from “automatic execution.”
- README target length is 150–180 lines; a final range of 140–190 lines is acceptable if every required section remains concise.
- Use standard MIT License text with `Copyright (c) 2026 tao943`.
- Keep `openspec/changes/` available for future feature-branch review, but remove it from the final main-branch tree after canonical requirements are in `openspec/specs/`; do not add it to `.gitignore`.
- Reference assessment is complete: OpenHands (MIT), Robusta (MIT), and Netdata (GPL-3.0) are information-architecture references only. Do not copy code, images, or prose. No dependency is adopted.
- Current verified corpus facts are: 10 Snapshot directories; 5 Docker Live scenario directories; 30 RAG knowledge cards; 64 Retrieval queries comprising 58 answerable and 6 no-answer probes; 10 query-rewrite cases; 12 offline Conversation fixtures; and a six-scenario Conversation Model Eval runner.

---

## File Structure

### Files created

- `LICENSE`: standard MIT License.
- `docs/architecture.md`: current architecture, data flow, storage, safety, and deployment boundaries.
- `docs/knowledge-catalog.md`: curated public index for the 30 preserved RAG knowledge cards.
- `openspec/specs/documentation-site/spec.md`: canonical contract for the curated VitePress documentation site, replacing the retired OpenSpec WIKI capability.
- `docs/assets/screenshots/incidents-desktop.png`: stable path for the incident-center screenshot.
- `docs/assets/screenshots/agent-config-desktop.png`: stable path for the Agent configuration screenshot.
- `docs/assets/screenshots/agent-config-narrow.png`: stable path for the narrow Agent configuration screenshot.
- `docs/assets/screenshots/system-status-desktop.png`: stable path for the system-status screenshot.

### Files replaced or modified

- `README.md`: 150–180-line portfolio landing page.
- `docs/index.md`: curated VitePress home page without change-history links.
- `docs/.vitepress/config.mts`: manually maintained product documentation navigation.
- `.gitignore`: prevents local Agent process artifacts from returning.
- `AGENTS.md`: canonical-spec and curated-documentation workflow; no wiki-sync/history mirror instructions.
- `apps/backend/tests/test_ci_tooling.py`: retains CI/config tests but removes the deleted wiki-sync script contract.
- Existing `openspec/specs/*/spec.md`: receive accepted deltas from the 12 active changes before change history is removed.

### Files moved or removed

- Move four files from `docs/evidence/ui/2026-08-24/` to `docs/assets/screenshots/`.
- Remove `docs/foundation.md` and `docs/aiops-workbench.md` after their current facts are incorporated into `docs/architecture.md`.
- Remove tracked process/history assets: `.codex/`, `.superpowers/`, `docs/changes/`, `docs/superpowers/`, `openspec/changes/`, and `openspec从0到1项目实战的提示词.md`.
- Replace `openspec/specs/openspec-wiki/spec.md` with `openspec/specs/documentation-site/spec.md`; the old specification contradicts the approved curated-docs design.

---

### Task 1: Curate the architecture, documentation home, navigation, and screenshots

**Files:**
- Create: `docs/architecture.md`
- Create: `docs/knowledge-catalog.md`
- Create by binary move: `docs/assets/screenshots/incidents-desktop.png`
- Create by binary move: `docs/assets/screenshots/agent-config-desktop.png`
- Create by binary move: `docs/assets/screenshots/agent-config-narrow.png`
- Create by binary move: `docs/assets/screenshots/system-status-desktop.png`
- Modify: `docs/index.md`
- Modify: `docs/.vitepress/config.mts`
- Delete after merge: `docs/foundation.md`
- Delete after merge: `docs/aiops-workbench.md`

**Interfaces:**
- Consumes: current routes from `apps/frontend/src/router/index.ts`, service boundaries from `infra/compose.yaml`, current behavior from `AGENTS.md`, and evaluation semantics from `docs/aiops/agentpy-domainbench.md`.
- Produces: stable documentation links `/architecture`, `/aiops/agentpy-domainbench`, `/operations-and-monitoring`, `/runbooks/live-eval`, `/tutorials/real-log-and-alert`, `/knowledge-catalog`, and `/examples/skills/` for README Task 2.

- [ ] **Step 1: Verify the four binary source files and create the destination directory**

Run from the worktree root:

```powershell
$sources = @(
  'docs/evidence/ui/2026-08-24/incidents-desktop.png',
  'docs/evidence/ui/2026-08-24/agent-config-desktop.png',
  'docs/evidence/ui/2026-08-24/agent-config-narrow.png',
  'docs/evidence/ui/2026-08-24/system-status-desktop.png'
)
$missing = $sources | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) }
if ($missing.Count -ne 0) { throw "Missing screenshot source: $($missing -join ', ')" }
New-Item -ItemType Directory -Path 'docs/assets/screenshots' -Force | Out-Null
```

Expected: command exits 0 and creates only `docs/assets/screenshots/`.

- [ ] **Step 2: Move screenshots to stable paths without re-encoding them**

Run:

```powershell
git mv -- 'docs/evidence/ui/2026-08-24/incidents-desktop.png' 'docs/assets/screenshots/incidents-desktop.png'
git mv -- 'docs/evidence/ui/2026-08-24/agent-config-desktop.png' 'docs/assets/screenshots/agent-config-desktop.png'
git mv -- 'docs/evidence/ui/2026-08-24/agent-config-narrow.png' 'docs/assets/screenshots/agent-config-narrow.png'
git mv -- 'docs/evidence/ui/2026-08-24/system-status-desktop.png' 'docs/assets/screenshots/system-status-desktop.png'
```

Expected: `git status --short` reports four renames and no image content edits.

- [ ] **Step 3: Replace the two overlapping architecture documents with one current document**

Create `docs/architecture.md` using these exact top-level sections and facts:

```markdown
# 系统架构

## 产品边界
Agent Py 是本地优先的 AIOps 工作台。告警或对话负责进入事件，AIOps Agent 负责证据驱动的调查，恢复动作必须经过确定性安全校验与策略门。

## 端到端数据流
Alert 或 Chat → Single/Multi-Agent Router → CLS/PostgreSQL/Redis/Prometheus/RAG → Evidence Aggregator → Adjudicator/Decision → Validator/Policy Gate → 自动恢复或人工审批 → Verify/Audit。

## Agent 运行时
- Conversation Agent 使用 `langchain` `create_agent`，处理对话、知识检索、事件查询和确认入口。
- AIOps Agent 使用 LangGraph Plan-Execute-Replan；复杂且跨数据源的调查可路由到最多两个并行 Specialist，再汇总到同一证据/裁决链。
- 核心 AIOps Prompt、Validator 与恢复策略由服务端控制；用户 Prompt/Skill 不能扩大工具 allowlist。

## 证据与知识
- CLS/MCP 提供真实日志，PostgreSQL/Redis/Prometheus 工具提供运行证据，Milvus + BM25L + RRF + rerank 提供知识召回。
- 证据、工具审计、假设裁决、报告、恢复意图和验证结果按 owner scope 持久化并可追溯。
- RAG 知识只提供通用排查与 SOP，不包含 benchmark ground truth 或 oracle。

## 恢复治理
- 低风险且命中白名单的动作只有在证据充分、Validator 通过、Policy Gate 许可后才能自动执行。
- 高风险、证据不足、状态不确定或验证失败进入人工审批/复核。
- 恢复后必须重新验证；失败或不确定不得盲目重试不可安全重放动作。

## 存储与基础设施
- PostgreSQL 是关系数据、durable job、事件和审计的事实来源。
- Redis 是缓存和低延迟事件投递辅助，不是持久任务或审计的唯一事实来源。
- Milvus 保存按 owner/tenant/knowledge base/document 过滤的知识向量。
- Docker Compose 管理 PostgreSQL、Redis、Milvus 依赖、Alertmanager、Nginx，以及显式启用的 Live Eval 服务。

## 前端工作区
列出 `/incidents`、`/incidents/:incidentId`、`/assistant`、`/knowledge`、`/agent-config`、`/integrations`、`/system` 的当前职责；注明 `/chat`、`/aiops`、`/mcp` 仅为兼容重定向。

## 安全与隔离
说明 owner/tenant 过滤、凭据不进入 Git、敏感输出脱敏、ground truth 隔离、恢复 allowlist 和审计边界。

## 部署边界
说明当前是本地优先单机/Compose 工作流，不宣称 Kubernetes、多区域、高可用或无人值守生产部署。
```

Use prose to connect the listed facts, but do not add components or guarantees not present in code. Then delete the two superseded files with:

```powershell
git rm -- 'docs/foundation.md' 'docs/aiops-workbench.md'
```

- [ ] **Step 4: Create the public RAG card catalog without modifying card assets**

Create `docs/knowledge-catalog.md` with:

```markdown
# RAG 知识卡目录

这 30 张通用差分排查卡是项目的可导入知识资产。它们用于检索与引用，不包含 Benchmark 的场景答案、ground truth、oracle 或评分规则。

## PostgreSQL
## Redis
## Nginx 与网络
## Kubernetes
## 主机与服务
## 消息队列
```

Under each heading, add relative Markdown links for every current file in `docs/knowledge-candidates/`, exactly once. Generate the candidate list with:

```powershell
Get-ChildItem 'docs/knowledge-candidates' -File -Filter '*.md' |
  Sort-Object Name |
  Select-Object -ExpandProperty Name
```

Expected: 30 filenames. Do not rename, edit, or add files inside `docs/knowledge-candidates/`.

- [ ] **Step 5: Replace the VitePress home page with the product-documentation home**

Replace `docs/index.md` with VitePress home frontmatter containing:

```yaml
layout: home
title: Agent Py AIOps 文档
hero:
  name: Agent Py
  text: 可审计、可评测、恢复受治理的 AIOps Agent
  tagline: 从真实告警和对话入口，到证据调查、安全恢复、验证与审计。
  actions:
    - theme: brand
      text: 查看系统架构
      link: /architecture
    - theme: alt
      text: 查看评测体系
      link: /aiops/agentpy-domainbench
    - theme: alt
      text: 本地运行
      link: /setup/windows
```

Add four feature cards named `证据驱动诊断`, `受控恢复`, `四层评测`, and `本地优先运行`. Do not mention OpenSpec change history.

- [ ] **Step 6: Replace generated VitePress navigation with a curated static configuration**

Replace `docs/.vitepress/config.mts`; remove the generated-file comment, `activeItems`, `archivedItems`, `/changes/` nav, and change-history sidebar. The exported configuration must have:

```ts
export default defineConfig({
  lang: "zh-CN",
  title: "Agent Py AIOps",
  description: "可审计、可评测、恢复受治理的 AIOps Agent 项目文档",
  cleanUrls: true,
  themeConfig: {
    nav: [
      { text: "首页", link: "/" },
      { text: "架构", link: "/architecture" },
      { text: "评测", link: "/aiops/agentpy-domainbench" },
      { text: "运行手册", link: "/operations-and-monitoring" }
    ],
    sidebar: [
      {
        text: "项目",
        items: [
          { text: "系统架构", link: "/architecture" },
          { text: "评测体系", link: "/aiops/agentpy-domainbench" },
          { text: "RAG 知识卡", link: "/knowledge-catalog" }
        ]
      },
      {
        text: "安装与运行",
        items: [
          { text: "Windows", link: "/setup/windows" },
          { text: "Linux", link: "/setup/linux" },
          { text: "macOS", link: "/setup/macos" },
          { text: "配置与监控", link: "/operations-and-monitoring" },
          { text: "Live Eval", link: "/runbooks/live-eval" },
          { text: "真实日志与告警", link: "/tutorials/real-log-and-alert" }
        ]
      },
      {
        text: "示例",
        items: [{ text: "多步骤 Skills", link: "/examples/skills/" }]
      }
    ],
    search: { provider: "local" },
    outline: { level: [2, 3], label: "本页目录" },
    docFooter: { prev: "上一页", next: "下一页" },
    sidebarMenuLabel: "目录",
    returnToTopLabel: "返回顶部"
  }
});
```

- [ ] **Step 7: Build the curated documentation**

Run:

```powershell
npm run docs:build
git diff --check
```

Expected: VitePress reports `build complete`; `git diff --check` has no output. Existing bundle-size and `gitignore` syntax-highlighting warnings are non-blocking, but no missing-link or missing-file error is allowed.

- [ ] **Step 8: Commit the curated documentation**

Run:

```powershell
git add -- 'docs/architecture.md' 'docs/knowledge-catalog.md' 'docs/assets/screenshots' 'docs/index.md' 'docs/.vitepress/config.mts' 'docs/foundation.md' 'docs/aiops-workbench.md'
git diff --cached --check
git commit -m "docs: curate public project documentation"
```

Expected: one commit containing only documentation content, four binary renames, and deletion of the two superseded documents.

---

### Task 2: Replace the README and add MIT licensing

**Files:**
- Create: `LICENSE`
- Replace: `README.md`

**Interfaces:**
- Consumes: stable links and images produced by Task 1; current routes in `apps/frontend/src/router/index.ts`; scenario/query/card counts in versioned fixtures; startup behavior in `scripts/start-local.sh`, `scripts/start-local.bat`, and `infra/compose.yaml`.
- Produces: the public GitHub landing page and repository license.

- [ ] **Step 1: Reconfirm every numeric README claim from the tree**

Run:

```powershell
$facts = [ordered]@{
  Snapshot = (Get-ChildItem 'benchmarks/agentpy/scenarios' -Directory).Count
  Live = (Get-ChildItem 'benchmarks/agentpy/live' -Directory).Count
  KnowledgeCards = (Get-ChildItem 'docs/knowledge-candidates' -File -Filter '*.md').Count
  Retrieval = (Select-String 'benchmarks/agentpy/retrieval/queries.yaml' -Pattern '^\s*-\s+\{id:').Count
  RetrievalAnswerable = (Select-String 'benchmarks/agentpy/retrieval/queries.yaml' -Pattern 'expected_no_answer: false').Count
  RetrievalNoAnswer = (Select-String 'benchmarks/agentpy/retrieval/queries.yaml' -Pattern 'expected_no_answer: true').Count
  QueryRewrite = (Select-String 'benchmarks/agentpy/retrieval/query_rewrite_cases.yaml' -Pattern '^\s*-\s+id:').Count
  ConversationOffline = ((Get-Content -Raw 'apps/backend/tests/fixtures/conversation_eval.json' | ConvertFrom-Json).Count)
}
$facts | Format-Table -AutoSize
```

Expected values: `10`, `5`, `30`, `64`, `58`, `6`, `10`, and `12`. Also verify `apps/backend/scripts/run_conversation_model_eval.py` still describes exactly six scenarios before writing “6 model scenarios.”

- [ ] **Step 2: Add the standard MIT License**

Create `LICENSE` with this complete canonical text:

```text
MIT License

Copyright (c) 2026 tao943

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Do not add project-specific restrictions.

- [ ] **Step 3: Replace README with the approved portfolio structure**

Replace `README.md` with these sections in this exact order:

```markdown
# Agent Py
[CI badge] [FastAPI badge] [Vue 3 badge] [MIT badge]

一句话定位

![事件中心](docs/assets/screenshots/incidents-desktop.png)

## 端到端闭环
## 核心能力
## 评测体系
## 架构与技术栈
## 快速开始
## 文档
## 当前边界
## License
```

Use these exact badge targets:

```markdown
[![CI](https://github.com/tao943/agent-py-aiops/actions/workflows/ci.yml/badge.svg)](https://github.com/tao943/agent-py-aiops/actions/workflows/ci.yml)
![FastAPI](https://img.shields.io/badge/FastAPI-Python%203.10%2B-009688)
![Vue](https://img.shields.io/badge/Vue-3-42b883)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
```

The positioning sentence must say “本地优先” and “恢复受治理”; it must not say “生产级”, “无人值守”, or “全自动修复”.

Render the end-to-end loop as one compact fenced text block:

```text
Alert 或 Chat 入口
→ Single/Multi-Agent 路由
→ CLS/PostgreSQL/Redis/Prometheus/RAG 证据
→ 证据聚合与根因裁决
→ Safety Validator / Policy Gate
→ 白名单自动恢复或人工审批
→ 恢复验证与全链路审计
```

The core-capabilities section must cover six items only: event/chat entry, evidence-driven LangGraph diagnosis, single/multi routing, hybrid RAG with citations, governed recovery, and durable audit/evaluation history.

The evaluation table must state:

| Layer | Versioned coverage | What it proves |
|---|---:|---|
| Snapshot | 10 scenarios | deterministic evidence replay, diagnosis, scoring, answer isolation |
| Retrieval | 30 cards / 64 queries | 58 answerable + 6 no-answer; scoped retrieval and citation quality |
| Conversation | 12 offline / 6 model / 10 rewrite | routing, minimum tools, confirmation, safety, and follow-up rewriting |
| Docker Live | 5 scenario definitions | real fault injection, evidence collection, recovery policy, verify/cleanup |

Immediately below the table, state that scenario presence does not mean every run executes automatic recovery or has a current successful real-model baseline; link to `docs/aiops/agentpy-domainbench.md` for recorded runs and limitations.

The architecture section must list Vue 3 + TypeScript, FastAPI + Python, LangChain/LangGraph, PostgreSQL 16, Redis 7, Milvus, Nginx, Alertmanager/Prometheus, Tencent CLS MCP, and Qwen-compatible model access. Do not include low-level SQL, Redis Stream retention, Nginx rate-limit values, or migration internals.

The Quickstart must be limited to prerequisites, template copy, PostgreSQL/Redis startup, and one platform launcher:

```powershell
if (-not (Test-Path 'config/project.json')) { Copy-Item 'config/project.template.json' 'config/project.json' }
if (-not (Test-Path 'config/user.project.json')) { Copy-Item 'config/user.project.template.json' 'config/user.project.json' }
docker compose -f infra/compose.yaml up -d postgres redis
scripts\start-local.bat
```

Explain that the copy commands never overwrite existing local configuration. Model and CLS credentials remain local and must be filled before real-model/real-log features work. Link macOS/Linux users to `docs/setup/macos.md` and `docs/setup/linux.md`; do not duplicate the manual startup guide.

The documentation map must link architecture, DomainBench, operations/monitoring, platform setup, Live Eval, real logs/alerts, knowledge catalog, and Skill examples. The limitations section must list: local-first single-machine workflow, external model/CLS/Milvus prerequisites, explicit cost/permission for real Live Eval, and guarded recovery rather than arbitrary command execution.

End with `MIT — see [LICENSE](LICENSE).`

- [ ] **Step 4: Validate README length, forbidden marketing, local links, and license**

Run:

```powershell
$lineCount = (Get-Content 'README.md').Count
if ($lineCount -lt 140 -or $lineCount -gt 190) { throw "README line count out of range: $lineCount" }
if (Select-String 'README.md' -Pattern '生产级|无人值守|全自动修复') { throw 'README contains an unapproved claim' }
$links = Select-String 'README.md' -Pattern '\]\((?!https?://|#)([^)]+)\)' -AllMatches |
  ForEach-Object { $_.Matches } |
  ForEach-Object { $_.Groups[1].Value.Split('#')[0] } |
  Where-Object { $_ -ne '' } |
  Sort-Object -Unique
$missing = $links | Where-Object { -not (Test-Path -LiteralPath $_) }
if ($missing.Count -ne 0) { throw "Missing README target: $($missing -join ', ')" }
$quickstart = Get-Content -Raw 'README.md'
if ($quickstart -notmatch "if \(-not \(Test-Path 'config/project\.json'\)\)" -or $quickstart -notmatch "if \(-not \(Test-Path 'config/user\.project\.json'\)\)") {
  throw 'Quickstart does not protect existing local configuration'
}
$license = Get-Content -Raw 'LICENSE'
if ($license -notmatch 'Copyright \(c\) 2026 tao943' -or $license -notmatch 'Permission is hereby granted') { throw 'MIT license validation failed' }
git diff --check
```

Expected: no exception and no `git diff --check` output.

- [ ] **Step 5: Rebuild docs with README targets already present**

Run:

```powershell
npm run docs:build
```

Expected: `build complete`; no missing document or screenshot error.

- [ ] **Step 6: Commit README and licensing**

Run:

```powershell
git add -- 'README.md' 'LICENSE'
git diff --cached --check
git commit -m "docs: present agent py as an aiops portfolio"
```

Expected: one commit containing only `README.md` and `LICENSE`.

---

### Task 3: Canonicalize active specifications, retire Wiki tooling, and remove process artifacts

**Files:**
- Modify: `.gitignore`
- Modify: `AGENTS.md`
- Modify: `apps/backend/tests/test_ci_tooling.py`
- Create: `openspec/specs/documentation-site/spec.md`
- Modify/Create: canonical specs updated by the 12 active OpenSpec changes
- Delete: `openspec/specs/openspec-wiki/spec.md`
- Delete: `.codex/`
- Delete: `.superpowers/`
- Delete: `docs/changes/`
- Delete: `docs/superpowers/`
- Delete: `openspec/changes/`
- Delete: `openspec从0到1项目实战的提示词.md`

**Interfaces:**
- Consumes: the approved design and this implementation plan from commits `c65eb05` and the plan commit; accepted delta requirements from all 12 active changes; runtime code/tests, README, and curated docs.
- Produces: a complete canonical spec set, a curated documentation-site contract, and a main-branch tree without local Agent process assets, generated change-history mirrors, or obsolete SQLite-era instructions.

**Active-change disposition:** Every active change is accepted and must be synchronized into canonical specs before deletion. Unchecked boxes are stale verification/recording state, not authority to discard the delta. If any archive command cannot apply cleanly, stop the cleanup and report that change; do not delete it or use `--skip-specs`.

| Active change | Disposition | Canonical capabilities receiving the delta |
|---|---|---|
| `migrate-postgresql-add-redis-runtime` | synchronize, then remove history | API/SSE, background jobs, Compose, repositories, Redis runtime, readiness |
| `add-agentpy-sre-benchmark` | synchronize, then remove history | AgentPy benchmark, diagnosis tasks, repositories |
| `add-blog-derived-retrieval-eval` | synchronize, then remove history | AgentPy benchmark, indexing, retrieval eval |
| `add-knowledge-batch-import` | synchronize, then remove history | document indexing |
| `add-docker-live-postgres-lock-eval` | synchronize, then remove history | Live SRE evaluation |
| `expand-rag-retrieval-benchmark` | synchronize, then remove history | knowledge-card catalog, retrieval eval |
| `harden-aiops-decision-validation` | synchronize, then remove history | AgentPy benchmark, diagnosis tasks |
| `persist-evaluation-results` | synchronize, then remove history | evaluation-result history |
| `add-auditable-hypothesis-adjudication` | synchronize, then remove history | AgentPy benchmark, diagnosis tasks, background jobs |
| `add-single-multi-agent-source-routing` | synchronize, then remove history | AgentPy benchmark, diagnosis tasks |
| `add-order-pool-leak-live-scenario` | synchronize, then remove history | AgentPy benchmark, diagnosis tasks |
| `add-production-recovery-execution` | synchronize, then remove history | authorization, background jobs, production recovery, chat |

- [ ] **Step 1: Verify worktree root, target containment, tracked-only scope, and a clean task boundary**

Run:

```powershell
$worktreeRoot = (Resolve-Path '.').Path
$expectedSuffix = [IO.Path]::Combine('.worktrees', 'repository-hygiene')
if (-not $worktreeRoot.EndsWith($expectedSuffix)) { throw "Wrong worktree: $worktreeRoot" }
$targets = @(
  '.codex',
  '.superpowers',
  'docs/changes',
  'docs/superpowers',
  'openspec/changes',
  'openspec从0到1项目实战的提示词.md'
)
foreach ($target in $targets) {
  $resolved = (Resolve-Path -LiteralPath $target).Path
  if (-not $resolved.StartsWith($worktreeRoot + [IO.Path]::DirectorySeparatorChar)) {
    throw "Target escaped worktree: $resolved"
  }
}
$tracked = git ls-files -- @targets
if ($LASTEXITCODE -ne 0 -or $tracked.Count -eq 0) { throw 'Tracked target resolution failed' }
$untracked = git ls-files --others --exclude-standard -- @targets
if ($untracked.Count -ne 0) { throw "Untracked files under removal targets: $($untracked -join ', ')" }
git status --short
```

Expected: every resolved target is inside this worktree; `git status --short` is empty before rule edits. Do not continue if any untracked file exists under a target.

- [ ] **Step 2: Synchronize every accepted active change into canonical specs in dependency order**

The first dry archive exposed eight legacy `MODIFIED` headers in `migrate-postgresql-add-redis-runtime` that no longer match canonical requirement titles. Repair that delta before archiving:

- `api-and-sse-contracts`: change `Durable SSE sequence contract` from `MODIFIED` to `ADDED`.
- `background-job-runtime`: map the two modified headers to `Durable owner-scoped background jobs` and `Durable background job events`.
- `docker-compose-startup`: map the modified header to `Unified compose startup`.
- `memory-repositories`: express `PostgreSQL application schema`, `Alembic-managed PostgreSQL migrations`, `Application database project configuration`, and `Database-independent repository boundary` as `ADDED`; express the four superseded SQLite/memory requirements as `REMOVED` with migration reasons.
- `runtime-readiness-checks`: change `PostgreSQL and Redis readiness semantics` from `MODIFIED` to `ADDED`.

Run `openspec validate migrate-postgresql-add-redis-runtime --strict` and repeat the missing-header audit. It MUST report zero missing `MODIFIED`/`REMOVED` targets before continuing.

Then run this exact ordered archive sequence. OpenSpec archive is used only as its supported spec-merge mechanism; the generated archive history is removed later in this task.

```powershell
$changes = @(
  'migrate-postgresql-add-redis-runtime',
  'add-agentpy-sre-benchmark',
  'add-blog-derived-retrieval-eval',
  'add-knowledge-batch-import',
  'add-docker-live-postgres-lock-eval',
  'expand-rag-retrieval-benchmark',
  'harden-aiops-decision-validation',
  'persist-evaluation-results',
  'add-auditable-hypothesis-adjudication',
  'add-single-multi-agent-source-routing',
  'add-order-pool-leak-live-scenario',
  'add-production-recovery-execution'
)
$actual = Get-ChildItem 'openspec/changes' -Directory |
  Where-Object Name -ne 'archive' |
  Select-Object -ExpandProperty Name
$difference = Compare-Object ($changes | Sort-Object) ($actual | Sort-Object)
if ($difference) { throw "Active change set differs from reviewed list: $($difference | Out-String)" }
foreach ($change in $changes) {
  openspec status --change $change --json | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "Cannot read active change: $change" }
  openspec archive $change --yes
  if ($LASTEXITCODE -ne 0) { throw "Canonical spec synchronization failed: $change" }
  openspec validate --all
  if ($LASTEXITCODE -ne 0) { throw "Canonical specs invalid after: $change" }
}
```

Do not use `--skip-specs` or `--no-validate`. Expected: all 12 changes move under `openspec/changes/archive/`, and accepted deltas create/update canonical capabilities. Verify the seven previously missing canonical capabilities now exist:

```powershell
$canonical = @(
  'agentpy-sre-benchmark', 'knowledge-retrieval-eval', 'live-sre-evaluation',
  'production-recovery', 'knowledge-card-catalog', 'redis-runtime-services',
  'evaluation-result-history'
)
foreach ($capability in $canonical) {
  $spec = "openspec/specs/$capability/spec.md"
  if (-not (Test-Path -LiteralPath $spec -PathType Leaf)) { throw "Canonical spec missing: $spec" }
}
```

After all archives succeed, normalize canonical storage language against the current PostgreSQL-only runtime in these nine specs: `agent-tool-call-audits`, `aiops-diagnosis-tasks`, `background-job-runtime`, `chat-memory-management`, `chat-sessions`, `document-indexing-jobs`, `memory-repositories`, `runtime-readiness-checks`, and `stream-rag-chat`. Preserve each requirement's behavior while replacing stale SQLite storage, migration, configuration, and readiness statements with PostgreSQL/Redis semantics proven by current code and tests.

Run:

```powershell
$staleStorage = rg -n 'SQLite|sqlite' openspec/specs
if ($LASTEXITCODE -eq 0) { throw "Stale canonical storage contract remains: $staleStorage" }
if ($LASTEXITCODE -ne 1) { throw 'Canonical storage scan failed' }
openspec validate --all
```

Expected: no canonical spec describes SQLite as a current runtime, migration, repository, chat, job, audit, or readiness dependency.

- [ ] **Step 3: Replace the contradictory OpenSpec WIKI contract with a curated documentation-site contract**

Delete `openspec/specs/openspec-wiki/` and create `openspec/specs/documentation-site/spec.md` with this complete contract:

```markdown
# Documentation Site Specification

## Purpose

定义面向项目使用者的精选 VitePress 文档站、人工维护导航与构建约束；开发过程和 OpenSpec change 历史不作为公开文档导航。

## Requirements

### Requirement: Curated VitePress documentation runtime
仓库 SHALL 在根 npm workspace 中提供以 `docs` 为源目录的 VitePress 开发、构建和预览命令，并 SHALL NOT 提交构建或缓存产物。

#### Scenario: 开发者启动文档站
- **WHEN** 执行 `npm run docs:dev`
- **THEN** VitePress MUST 启动并提供项目首页、架构、评测、安装和运行手册。

#### Scenario: 文档生产构建
- **WHEN** 执行 `npm run docs:build`
- **THEN** 构建 MUST 成功，且 `docs/.vitepress/dist` 与缓存目录 MUST 被 Git 忽略。

### Requirement: Public navigation excludes process history
仓库 SHALL 人工维护面向当前产品的导航，并 SHALL NOT 把 OpenSpec active/archive change、Agent 计划或生成的历史镜像作为公开导航项。

#### Scenario: 访问者浏览文档导航
- **WHEN** 访问者打开文档首页或侧边栏
- **THEN** 导航 MUST 指向当前架构、评测、安装、运行手册、教程和示例，并且 MUST NOT 出现 change WIKI。
```

Use `git rm -r -- 'openspec/specs/openspec-wiki'` for the tracked retirement and `apply_patch` for the new spec. Then run `openspec validate --all`; expected: zero failures and a `spec/documentation-site` item.

- [ ] **Step 4: Remove the obsolete wiki-sync test seam and run its focused gates**

In `apps/backend/tests/test_ci_tooling.py`, remove only the `runpy` import, the `Callable` import, the `WIKI_SYNC` constant, and the complete function named `test_wiki_sync_accepts_resolved_directory_link_not_placeholder`.

```python
import runpy
from collections.abc import Callable
WIKI_SYNC = REPO_ROOT / ".codex" / "skills" / "wiki-sync" / "scripts" / "sync_wiki.py"
```

Keep `cast`, the CI change-detection tests, offline-config tests, workflow assertions, and runtime-input bootstrap tests. Run from `apps/backend`:

```powershell
uv run ruff check tests/test_ci_tooling.py
uv run pytest tests/test_ci_tooling.py -q
```

Expected: Ruff clean and all remaining `test_ci_tooling.py` tests pass.

- [ ] **Step 5: Update ignore rules without ignoring future OpenSpec changes**

Append these exact entries to `.gitignore` beside other tool/build metadata:

```gitignore
.codex/
.superpowers/
docs/superpowers/
docs/changes/
```

Do not add `openspec/changes/`. Verify:

```powershell
$required = @('.codex/', '.superpowers/', 'docs/superpowers/', 'docs/changes/')
foreach ($entry in $required) {
  if (-not (Select-String '.gitignore' -SimpleMatch $entry -Quiet)) { throw "Missing ignore: $entry" }
}
if (Select-String '.gitignore' -SimpleMatch 'openspec/changes/' -Quiet) { throw 'openspec/changes must remain available on feature branches' }
```

- [ ] **Step 6: Align AGENTS.md with canonical specs and curated docs**

Make these precise edits to `AGENTS.md`:

- In the repository structure, describe `openspec/specs/` as canonical requirements and `docs/` as curated architecture/setup/runbook/evaluation documentation; remove “活动变更和归档” and “OpenSpec WIKI”.
- In sources of truth, remove `openspec/changes/<change-id>/`; state that merged behavior must be reflected in `openspec/specs/<capability>/spec.md`.
- Rename `## OpenSpec 与 WIKI` to `## OpenSpec 与文档`.
- State that feature branches may use focused OpenSpec changes for review, but before merge their accepted requirements must be synchronized into `openspec/specs/` and the change directory removed from the final tree.
- Delete all `wiki-sync`, `docs/changes/`, generated-history navigation, and archive-as-long-lived-source instructions.
- Preserve all security, configuration, migration, validation, owner isolation, recovery, and Git safety rules verbatim unless a sentence directly depends on the removed history workflow.

Verify:

```powershell
if (Select-String 'AGENTS.md' -Pattern 'wiki-sync|docs/changes/|OpenSpec WIKI|已归档内容只用于追溯') {
  throw 'AGENTS.md still contains removed history workflow'
}
if (-not (Select-String 'AGENTS.md' -SimpleMatch 'openspec/specs/' -Quiet)) { throw 'Canonical spec path missing' }
```

- [ ] **Step 7: Recheck exact tracked targets, then remove only those targets**

Run:

```powershell
$prefixes = @('.codex/', '.superpowers/', 'docs/changes/', 'docs/superpowers/', 'openspec/changes/')
foreach ($prefix in $prefixes) {
  $count = (git ls-files -- "$prefix*").Count
  if ($count -eq 0) { throw "Expected tracked files under $prefix" }
  Write-Output "$prefix`t$count"
}
if ((git ls-files -- 'openspec从0到1项目实战的提示词.md').Count -ne 1) {
  throw 'Legacy prompt file target is missing or ambiguous'
}
git rm -r -- '.codex' '.superpowers' 'docs/changes' 'docs/superpowers' 'openspec/changes'
git rm -- 'openspec从0到1项目实战的提示词.md'
```

Expected: only the six confirmed targets are staged as deletions. This deliberately deletes the design and plan from the final tree; both remain recoverable from their earlier branch commits and PR history.

- [ ] **Step 8: Prove preservation, validate the branch, and commit**

Run:

```powershell
$mustRemain = @(
  'AGENTS.md', 'CONTEXT.md', 'apps', 'packages', 'benchmarks', 'config',
  'infra', 'scripts', 'docs/knowledge-candidates', 'openspec/specs', '.github',
  'package.json', 'package-lock.json', 'openspec/specs/documentation-site/spec.md'
)
foreach ($path in $mustRemain) {
  if (-not (Test-Path -LiteralPath $path)) { throw "Protected path missing: $path" }
}
$removed = git ls-files -- '.codex/*' '.superpowers/*' 'docs/changes/*' 'docs/superpowers/*' 'openspec/changes/*' 'openspec从0到1项目实战的提示词.md' 'openspec/specs/openspec-wiki/*'
if ($removed.Count -ne 0) { throw "Tracked removal incomplete: $($removed -join ', ')" }
if ((Get-ChildItem 'docs/knowledge-candidates' -File -Filter '*.md').Count -ne 30) { throw 'RAG card corpus changed' }
if ((Get-ChildItem 'benchmarks/agentpy/scenarios' -Directory).Count -ne 10) { throw 'Snapshot corpus changed' }
if ((Get-ChildItem 'benchmarks/agentpy/live' -Directory).Count -ne 5) { throw 'Live corpus changed' }
openspec validate --all
npm run docs:build
Push-Location 'apps/backend'
uv run ruff check tests/test_ci_tooling.py
uv run pytest tests/test_ci_tooling.py -q
Pop-Location
git diff --check
$wikiReferences = rg -n 'sync_wiki|openspec-wiki|docs/changes|wiki-sync' AGENTS.md README.md docs openspec/specs apps/backend/tests/test_ci_tooling.py --glob '!docs/.vitepress/dist/**'
if ($LASTEXITCODE -eq 0) { throw "Retired Wiki reference remains: $wikiReferences" }
if ($LASTEXITCODE -ne 1) { throw 'Wiki reference scan failed' }
$staleNavigation = rg -n '/changes/|/foundation|/aiops-workbench' README.md docs/index.md docs/.vitepress/config.mts AGENTS.md
if ($LASTEXITCODE -eq 0) { throw "Stale navigation remains: $staleNavigation" }
if ($LASTEXITCODE -ne 1) { throw 'Stale-navigation scan failed' }
git add -- '.gitignore' 'AGENTS.md' 'apps/backend/tests/test_ci_tooling.py' 'openspec/specs'
git diff --cached --check
git commit -m "chore: canonicalize specs and remove process artifacts"
```

Expected: canonical specs and curated docs pass; the targeted backend tooling test passes; no Wiki/history reference remains; one commit contains canonical spec updates, documentation-site replacement, the focused test cleanup, repository rules, and only the confirmed process/history deletions.

---

### Task 4: Final acceptance and PR handoff

**Files:**
- Verify only; do not create or modify repository files.

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces: evidence for the PR description and a branch ready for GitHub Actions. GitHub Topics are a post-merge metadata action, not part of the branch diff.

- [ ] **Step 1: Verify the complete branch diff is scoped to phase one**

Run:

```powershell
git status --short --branch
git diff --check origin/main...HEAD
git diff --stat origin/main...HEAD
git diff --name-status origin/main...HEAD
```

Expected: clean worktree; only README/license, curated documentation/screenshots, canonical spec synchronization, `.gitignore`, `AGENTS.md`, the focused removal in `apps/backend/tests/test_ci_tooling.py`, and confirmed process/history deletions. No runtime source, package, benchmark, config, infrastructure, script, workflow, or other test file is changed.

- [ ] **Step 2: Run the final documentation and specification gates from a clean tree**

Run:

```powershell
npm ci
npm run docs:build
openspec validate --all
Push-Location 'apps/backend'
uv run ruff check tests/test_ci_tooling.py
uv run pytest tests/test_ci_tooling.py -q
Pop-Location
git diff --check origin/main...HEAD
```

Expected: dependency install succeeds without modifying `package-lock.json`; docs build succeeds; every canonical spec passes; diff check has no output. If `npm ci` modifies a tracked file, stop and investigate rather than stage it.

- [ ] **Step 3: Run focused repository-hygiene assertions**

Run:

```powershell
$readmeLines = (Get-Content 'README.md').Count
if ($readmeLines -lt 140 -or $readmeLines -gt 190) { throw "README line count: $readmeLines" }
if (-not (Test-Path 'LICENSE')) { throw 'LICENSE missing' }
if ((git ls-files 'openspec/specs/**').Count -eq 0) { throw 'Canonical specs missing' }
if ((git ls-files 'docs/knowledge-candidates/**').Count -ne 30) { throw 'Knowledge cards changed' }
$scopedChanges = git diff --name-only origin/main...HEAD -- 'apps/**' 'packages/**' 'benchmarks/**' 'config/**' 'infra/**' 'scripts/**' '.github/**'
$unexpected = $scopedChanges | Where-Object { $_ -ne 'apps/backend/tests/test_ci_tooling.py' }
if ($unexpected.Count -ne 0) {
  throw "Out-of-scope runtime or CI change detected: $($unexpected -join ', ')"
}
```

Expected: no exception.

- [ ] **Step 4: Prepare the PR without changing main**

After user approval to publish, push `chore/repository-hygiene` and create a PR titled:

```text
chore: curate public repository and documentation
```

The PR body must summarize: portfolio-first README; MIT License; curated VitePress navigation; 10 Snapshot/64 Retrieval/12+6 Conversation/5 Live versioned coverage; removal of process/history assets; preservation of runtime, benchmark, RAG, canonical specs, and local user work. Include the exact validation commands and state that dead-code cleanup is intentionally deferred to a second PR.

- [ ] **Step 5: Perform post-merge GitHub metadata update only after the PR is merged**

Set these Topics and no Homepage:

```text
aiops, langgraph, rag, fastapi, vue3, postgresql, redis, milvus, mcp, sre
```

Verify the public repository page displays the MIT license, selected Topics, README screenshots, and passing GitHub Actions. Do not begin the dead-code PR until the merged `origin/main` contains this cleanup.

---

## Deferred Phase Two

Code cleanup is intentionally excluded from this plan. After phase one merges, create a fresh branch from the new `origin/main`, run existing Ruff/Pyright/Vue checks plus pinned one-off Vulture and Knip advisory scans, and require all five deletion criteria from the approved design. If no candidate satisfies all criteria, publish “no safely removable code found” instead of forcing a deletion.
