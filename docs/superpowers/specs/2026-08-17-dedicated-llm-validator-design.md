# 独立 LLM Validator 与安全结构化错误审计设计

## 背景

真实 `APY-013` Run `eval-2a88e913a8f444aa99e8da650d35aad3` 得分 94，根因 Candidate
通过全部十项确定性检查，但 LLM Validator 两次返回均未通过结构化解析：

- `validationErrorCategory=retry_exhausted`
- `validationErrorPhase=structured_parse`
- 内部错误码为 `invalid_json_or_schema`
- 最终以 `deterministic_grounded_fallback` 保留结论
- Recovery 强制为 `manual_review`，`executionPermitted=false`

当前诊断 Agent 与 LLM Validator 都使用 `qwen3.7-plus`。这既没有形成真正独立的语义复核，
也无法从历史审计中区分缺失字段、枚举错误、容器类型错误或 LangChain envelope 不兼容。

## 目标

- 主诊断 Agent 保持 `qwen3.7-plus`，只把 LLM Validator 切换为 `qwen3.8-max`。
- Validator 与主模型共享现有 DashScope API Key、Base URL、超时和重试设置，不复制凭据。
- 保持确定性 Validator 为事实和证据合同的主门禁，LLM Validator 只做独立语义复核。
- 对结构化解析失败保存安全、稳定、可统计的错误子分类。
- 为 Qwen JSON Mode 提供明确 JSON 指令和无答案注入的响应形状示例。
- 保持两次调用上限、fail-closed、人工恢复限制和现有评分阈值。

## 非目标

- 不切换 Planner、Replanner、Evidence Evaluator、Decision、Report 或普通 Chat 模型。
- 不删除确定性 Validator，也不允许 LLM Validator 覆盖确定性失败。
- 不保存原始模型响应、Prompt、异常正文、字段值、Ground Truth、Oracle、原始 CLS 日志或凭据。
- 不新增数据库 schema、第三方依赖、外部服务或自动恢复权限。
- 不修改 Benchmark 答案、权重、阈值或 canonical labels。
- 实现完成后不自动重跑 APY-013；真实运行需要新的明确授权并保存为独立 Run。

## 模型选择

采用 `qwen3.8-max` 作为 Validator：

- 用户当前账户有独立免费额度，并与现有 API Key/端点兼容。
- 阿里云官方 Structured Output 文档明确列出 `qwen3.8-max` 支持 JSON Mode。
- Validator 请求短、字段固定，Max 模型适合语义核验和严格结构化输出。
- 截图中的 DeepSeek V4/R1 系列更偏长推理；用于本次短 JSON 合同会增加 reasoning 内容和
  provider 差异，不作为首选。

主 Agent 保持 `qwen3.7-plus`，避免同时改变调查计划、Tool Calling、Decision 和 RAG 行为，
确保后续差异只归因于 Validator。

## 方案比较

### 方案 A：Provider 内独立 Validator 模型（采用）

配置增加可选 `validatorModel`，Provider 使用同一认证和端点创建独立 Chat Model；Diagnostics
只在 `_decision_validator` 使用它。历史配置缺少字段时回退到 `chatModel`。

优点是边界清晰、兼容旧配置、测试替换简单；代价是 Provider 增加一个窄接口。

### 方案 B：整个系统切换 qwen3.8-max

配置最少，但会同时改变所有 Agent 节点，破坏现有 94 分基线的可归因性，因此不采用。

### 方案 C：Diagnostics 自行构造第二个 ChatOpenAI

改动局部，但业务层会接触 API Key、Base URL 和 provider 细节，破坏依赖边界，因此不采用。

## Reuse-first 评估

- 项目已依赖 LangChain `ChatOpenAI`、Pydantic v2 和现有 structured-output invoker，不新增依赖。
- `langchain-ai/langchain`（MIT）现有 `with_structured_output(include_raw=True)` envelope 继续直接
  复用；GitHub issue 显示不同 Provider 对 envelope/Pydantic 的兼容行为存在差异，因此本项目
  必须在边界处分类，而不能假设解析错误只有一种。
- Pydantic（MIT）的 `ValidationError.errors()` 可提供 `type` 与 `loc`；本项目只投影允许列表
  类型和字段名，不读取 `input`、`msg`、`ctx` 或完整异常字符串。
- `567-labs/instructor`（MIT）提供 Pydantic 重试，但会增加依赖并重复现有两次有界重试，属于
  reference only，不采用。
- GitHub 上的 Qwen/DashScope JSON Mode 兼容案例与官方文档都指出消息必须明确包含 `JSON`；
  当前 Prompt 已包含该词，本轮再增加固定响应示例以稳定字段形状。

结论：直接复用现有 LangChain/Pydantic；自定义的只有项目安全审计映射和 Provider 选择逻辑。

## 配置与 Provider 设计

`llm` 配置新增：

```json
{
  "chatModel": "qwen3.7-plus",
  "validatorModel": "qwen3.8-max",
  "modelCapabilities": {
    "qwen3.7-plus": {
      "contextWindowTokens": 1000000,
      "structuredOutputMethod": "json_mode"
    },
    "qwen3.8-max": {
      "contextWindowTokens": 1000000,
      "structuredOutputMethod": "json_mode"
    }
  }
}
```

`validatorModel` 可选；缺失时等于 `chatModel`。配置加载必须校验 Validator 也存在 capability
profile，并派生独立的 `validator_structured_output_method`。

`QwenOpenAIProvider` 增加：

- `create_validator_model()`：使用相同 API Key/Base URL/temperature/timeout/retry，只替换 model；
- `validator_model_name`：返回安全模型名用于审计；
- `validator_structured_output_method`：返回 Validator profile 的 structured-output 方法。

Diagnostics 通过兼容 helper 调用这些能力；测试 Fake 或旧 Provider 没有新接口时，回退到
`create_chat_model()` 和现有 `structured_output_method`，避免一次性修改所有测试替身。

## Validator 数据流

```text
LLM Candidate
  -> deterministic Validator
  -> grounded normalization（必要时）
  -> deterministic Validator 再验证
  -> qwen3.8-max semantic Validator
  -> Policy Gate
```

只有前置确定性检查全部通过，才调用 `qwen3.8-max`。LLM Validator 返回 `invalid` 时只能拒绝
或触发既有有界 Replan，不能把确定性失败改为 valid。

Validator Prompt 增加无答案注入的 JSON 形状示例。示例只使用 Candidate 已引用的公开 Evidence
IDs，不包含 ground truth、正确标签、恢复动作或隐藏评分字段。两次调用上限保持不变；第二次只
追加固定格式纠正指令。

## 安全错误子分类

Structured Parse 失败映射到以下允许列表：

- `invalid_json`：原始非 structured 路径不是合法 JSON；
- `structured_envelope_mismatch`：缺少 `parsed`、存在 `parsing_error` 或 envelope 形状错误；
- `missing_required_field`：Pydantic `missing`；
- `invalid_enum`：Pydantic `literal_error`；
- `wrong_container_type`：列表字段收到非列表；
- `extra_field`：Pydantic `extra_forbidden`；
- `unknown_evidence_id`：结构合法但引用非当前任务 Evidence ID；
- `invalid_json_or_schema`：无法安全细分的兼容回退。

分类函数只能使用异常类型、Pydantic error `type` 和允许列表 `loc`。不得读取或保存原始输入、
`msg`、`ctx`、异常正文或模型响应。多错误去重后按稳定顺序保存，最多六项。

Step、Checkpoint 和 Artifact 增加以下安全字段：

- `validationModel`
- `validationErrorCodes`
- 既有 `validationErrorCategory`
- 既有 `validationErrorPhase`
- 既有 `validationAttempts`
- 既有 `validationRetryable`
- 既有 `validationHttpStatusClass`

模型调用异常仍使用现有 timeout/authentication/rate-limit/provider 4xx/5xx 分类；structured
parse 子分类不与 Provider 调用错误混淆。

## 失败与安全行为

- Validator 配置无 profile：启动/配置加载失败，不静默换成未知模型。
- 新接口不存在的旧 Fake Provider：兼容回退主 Chat Model，只用于测试和旧实现。
- qwen3.8-max 调用失败：保留现有安全错误分类；确定性全通过时只允许
  `deterministic_grounded_fallback + manual_review`。
- qwen3.8-max 返回非法结构：两次后 `retry_exhausted`，同时保存脱敏 parse 子分类。
- qwen3.8-max 明确返回 invalid：记录 `model_rejected`，按现有规则最多进行一次有界 Replan。
- 无论 LLM Validator 是否成功，高风险恢复仍需人工审批；本轮不扩大自动恢复范围。

## 测试与验收

采用 RED-GREEN-REFACTOR：

1. 配置未指定 Validator 时回退主模型；指定 `qwen3.8-max` 时读取独立 profile；缺失 profile
   fail closed。
2. Provider 创建 Validator 时模型名不同，但 API Key、Base URL、timeout/retry 相同且 API Key
   不进入 repr。
3. Diagnostics 只在 Decision Validator 使用独立模型，其余节点保持主模型。
4. 每个 parse 子分类都有直接失败测试；输出和 repr 不包含原始响应、字段值或异常正文。
5. `validationModel`、`validationErrorCodes` 在 Step/Checkpoint/Artifact 中一致，历史 Artifact
   缺失字段仍兼容。
6. Validator Prompt 包含 JSON 指令和安全示例，且不包含 Ground Truth/Oracle。
7. 独立 `qwen3.8-max` Validator readiness 使用合成公开事实和虚构 Evidence ID，不读取真实日志。
8. 两组专项 pytest、Ruff、Pyright 和 focused OpenSpec strict 通过；不运行全量 pytest。
9. 实现验收不自动重跑 APY-013；若用户另行授权，只运行一次并保存新 Run。

## 验收标准

- 主 Agent 审计仍显示 `qwen3.7-plus`，Validator 审计显示 `qwen3.8-max`。
- 相同 DashScope API Key/Base URL 被内部复用，不新增或输出凭据。
- 合法 Validator 结果产生 `validationOrigin=llm_confirmed`。
- 非法结果能定位到至少一个安全 parse 子分类，而不是只剩泛化
  `invalid_json_or_schema`。
- 确定性 Validator、评分、Policy Gate 和恢复权限不降低。
- 无 Prompt、原始响应、异常正文、字段值、Ground Truth、Oracle、原始 CLS 日志或密钥进入
  Git、Step、Checkpoint、Artifact 或测试输出。
