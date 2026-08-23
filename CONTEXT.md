# Agent Py Domain Context

本文件统一 Agent Py 对话编排与 AIOps 诊断衔接使用的领域语言，避免相同概念在代码、测试和文档中出现不同名称。

## Language

**Chat Turn**:
用户的一次输入及其对应的持久执行、公开事件、结构化结果和可降级解读。
_Avoid_: Chat request, message execution

**Chat Turn Execution**:
根据意图、目标和安全策略，将一个 Chat Turn 确定为直接读取、待确认写入或受限 ReAct 的执行决策。
_Avoid_: Chat orchestration service, intent handler

**Context Envelope**:
一次模型调用可使用的完整上下文，包括结构化记忆、最近原始轮次、工具 Schema、工具 Observation 和输出预算。
_Avoid_: Prompt context, message window

**Chat Execution Budget**:
一个 Chat Turn 可消费的模型调用、工具调用、重复调用和总时限配额。
_Avoid_: ReAct limit, timeout config

**Pending Chat Action**:
经只读预检后等待用户明确确认的新诊断或恢复审批写入意图；它不是恢复批准，也不具有恢复执行权限。
_Avoid_: Pending command, approval action

**Structured Memory**:
由用户目标、确认事实、偏好、决策、未完成事项、资源引用和 citation 构成的版本化长期对话记忆。
_Avoid_: Chat summary, conversation digest

**Direct Read**:
目标明确且无写入副作用的 owner-scoped AIOps Bridge 查询，不经过 ReAct 工具选择。
_Avoid_: Fast path, direct tool call

**LLM Explanation**:
对已验证结构化结果的自然语言解读；它可以降级失败，但不能改变业务结果或安全字段。
_Avoid_: Final answer, result rewrite
