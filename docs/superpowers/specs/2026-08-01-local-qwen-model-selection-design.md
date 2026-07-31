# 本地 Qwen 模型与凭据安全设计

## 目标

在不暴露 DashScope API Key、不改变现有 Agent 与 RAG 架构的前提下，使用当前账户有免费额度且与现有客户端兼容的模型。

## 配置选择

- Chat 模型使用 `qwen3.7-plus`，上下文窗口配置为 `1000000` Token。
- Embedding 模型使用 `qwen3.7-text-embedding`，继续输出 `1024` 维向量，以兼容当前 Milvus collection。
- Rerank 模型暂时使用 `qwen3-vl-rerank`，沿用现有客户端的请求结构和端点；`qwen3-rerank` 需要独立改造客户端后再切换。

这些值只写入本地 `config/user.project.json`，不修改可提交的模板默认值。

## 凭据安全

- `config/project.json` 和 `config/user.project.json` 是本地运行配置，必须加入 `.gitignore`。
- 使用 `git rm --cached` 仅取消这两个文件的 Git 跟踪，保留本地文件及其中的配置。
- 仓库继续跟踪 `config/project.template.json`、`config/user.project.template.json` 和 `config/project.test.json`。
- 实现及验证过程不打印、不记录、不测试固化 API Key 的内容。

## 验证

1. Git 不再跟踪两个本地配置文件，且 `git check-ignore` 能确认忽略规则生效。
2. 合并后的 LLM 配置能成功加载，模型名、向量维度与上下文窗口符合上述选择。
3. API Key 只验证为非空，不在命令输出、测试日志或提交差异中显示。
4. 先运行离线配置测试；真实 API 连通性测试后续通过独立的 `live_llm` 测试显式执行。
