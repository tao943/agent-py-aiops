---
name: wiki-sync
description: "同步 OpenSpec 变更到 WIKI。\n触发时机：变更创建（openspec new）或归档（openspec archive）后。\n自动生成/更新 docs/changes/ 下的 WIKI 页面。"
---

# 同步 OpenSpec 变更到 WIKI

OpenSpec 变更与 `docs/changes/` 下的 WIKI 页面必须保持同步。

## 触发时机

| 操作 | 文档同步 |
|------|---------|
| 创建新变更 | 创建 WIKI 页面到 `docs/changes/active/` |
| 归档变更 | 移动 WIKI 页面到 `docs/changes/archive/` |

## 确定性入口

从仓库根目录执行：

```bash
python3 .codex/skills/wiki-sync/scripts/sync_wiki.py active <change-name>
python3 .codex/skills/wiki-sync/scripts/sync_wiki.py archive <change-name-or-archive-name>
python3 .codex/skills/wiki-sync/scripts/sync_wiki.py all
```

脚本会生成或更新页面、索引和 Sidebar，并单独验证 delta specs、include 目标与导航一致性。归档模式完成后仍须执行 `npm run docs:build`。

## 工作流程

### 模式一：新建变更同步

1. **验证变更存在**：检查 `openspec/changes/{name}/` 目录。
2. **创建 WIKI 页面**：在 `docs/changes/active/{name}/` 下创建 `index.md`，使用 VitePress 的 `@include` 指令引用 proposal、design、tasks 和 delta specs。项目使用符号链接 `docs/openspec` → `openspec`。
3. **更新 Sidebar**：在 `docs/.vitepress/config.mts` 的“进行中”分组中添加条目。
4. **更新索引**：同步 `docs/changes/index.md`。
5. **验证 include**：检查页面中的每个 `@include` 目标文件存在。
6. **输出完成摘要**。

### 模式二：归档变更同步

1. **验证归档变更存在**：在 `openspec/changes/archive/` 下查找匹配目录，支持完整名称或简短名称自动匹配日期前缀。
2. **强制检查 delta specs**：
   - 检查归档变更目录下是否存在 specs；不存在则阻断并提示用户创建或显性声明跳过。
   - 检查 delta specs 是否已同步到 main specs；未同步则询问用户选择同步或跳过。
   - 仅当用户显性声明跳过时才可使用脚本的 `--allow-unsynced` 绕过检查。
3. **移动或创建 WIKI 页面**：将 active 页面移至 `docs/changes/archive/{date}-{name}/`，更新 frontmatter 状态。
   - frontmatter 统一使用 `title`、`status`、`createdDate`、`archivedDate`。
   - 归档页 `@include` 必须指向 `../../../openspec/changes/archive/{date}-{name}/...`。
   - 禁止保留 `../../../openspec/changes/{name}/...` 这类归档前路径。
4. **更新 Sidebar**：从“进行中”移除，添加到“已归档”。
5. **更新索引**：同步更新 `docs/changes/index.md`，保持与 Sidebar 顺序一致。
6. **验证 include**：任一目标缺失时阻断。
7. **验证构建**：执行 `npm run docs:build`；构建通过后仍需保留 include 目标存在性检查结论。
8. **输出完成摘要**。

### WIKI 页面结构

页面使用 VitePress frontmatter 和 include 指令引用 OpenSpec 变更文件。

**Active 页面 frontmatter**：

```yaml
---
title: change-title
status: active
createdDate: YYYY-MM-DD
---
```

**Archive 页面 frontmatter**：

```yaml
---
title: change-title
status: archived
createdDate: YYYY-MM-DD
archivedDate: YYYY-MM-DD
---
```

include 语法：

```markdown
<!--@include: ../../../openspec/changes/archive/YYYY-MM-DD-change-title/proposal.md-->
```

## Guardrails

- 不修改 OpenSpec 原有文件，只创建或移动 WIKI 页面。
- 使用相对路径的 `@include` 指令引用 OpenSpec 内容。
- 操作前验证路径有效性；失败时提供清晰错误和恢复建议。
- 归档时强制检查 delta specs 存在性和同步状态。
- 归档页面禁止引用归档前的 `openspec/changes/{name}/` 路径。
- VitePress 构建成功不代表 include 有效，必须单独检查 include 目标文件。
- `docs/changes/index.md`、`docs/.vitepress/config.mts`、`docs/changes/` 与 `openspec/changes/` 必须保持一致。
