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
