# 测试环境与真实 LLM 分层设计

## 目标

修复当前 Windows/Python 3.13 环境中的依赖安装、临时目录、跨平台断言和测试夹具漂移，并将默认离线测试与显式真实 DashScope 测试彻底分离。默认测试不得读取或调用本地真实 API Key；只有用户主动选择 `live_llm` 时才允许产生模型请求。

## 阶段一：稳定开发与测试环境

### 依赖解析

腾讯 CLS SDK 传递依赖的 `python-snappy==0.6.0` 在 Windows/Python 3.13 下缺少适用 wheel，导致 `uv sync` 尝试本地 C++ 编译。项目继续保留 Python `>=3.10` 和腾讯 CLS SDK，在 uv override 中将 `python-snappy` 提升到 `>=0.7.3`，同时保留既有 `protobuf` override，并重新生成锁文件。

### pytest 临时目录

pytest 默认使用仓库内、已忽略的 `apps/backend/var/pytest` 作为 `basetemp`。本地与 CI 不再依赖系统临时目录的权限或编码行为。项目文档继续以 `uv run pytest` 为标准入口；已有虚拟环境可使用 `.venv/Scripts/python.exe -m pytest` 进行快速验证。

### Windows 与测试夹具兼容

- Bash 语法测试不直接把 Windows 中文路径交给 WSL；在 Windows 优先使用 Git Bash，并通过标准输入检查脚本内容。没有可用 Bash 时以明确原因跳过。
- Skill 上传校验把 CRLF 和孤立 CR 规范化为 LF，返回跨平台一致的 Markdown 内容。
- Redis 故障测试替身实现 `eval` 并抛出 `RedisError`，以验证真实的 local-fallback 路径，而不是触发 `AttributeError`。
- 仅测试 `/config/check` 的用例显式注入 Fake rate limiter，避免不相关的 `rateLimits` 配置在应用创建阶段中断测试。
- 基础设施与模板测试验证结构和语义，不依赖换行位置、本地凭据或已移除的 SQLite 迁移术语。

## 阶段二：离线与真实 LLM 测试分层

### 默认离线测试

普通测试使用临时项目 JSON、非真实占位 Key 以及 Fake Chat、Embedding、Rerank 实现。测试不得读取 `config/user.project.json`，不得断言真实 Key 前缀，也不得依赖用户当前选择的模型。模板测试只检查可提交模板已脱敏且结构完整。

pytest 注册 `live_llm` marker，并在默认 `addopts` 中排除该 marker。因此 `uv run pytest` 必须保证零真实模型请求。

### 显式真实测试

新增最小 `live_llm` smoke tests：

1. Chat readiness 发起一次简短请求，验证 provider、模型和安全错误边界。
2. Embedding 对一条短文本生成向量，验证输出维度为 `1024` 且元素有限。
3. Rerank 对一个查询和两条短文档排序，验证索引、分值和结果数量。

这些测试只在 `pytest -m live_llm` 时执行，并从被 Git 忽略的本地 JSON 加载配置。API Key 或所需模型配置缺失时明确跳过，不回退到环境变量，也不输出密钥。调用失败时只报告脱敏后的异常类别和安全错误消息。

## 配置与凭据边界

- `config/project.json` 和 `config/user.project.json` 保持本地、忽略、未跟踪。
- `config/project.template.json` 和 `config/user.project.template.json` 保持可提交且所有真实云凭据为空。
- 本地已选模型保持为 `qwen3.7-plus`、`qwen3.7-text-embedding`、`qwen3-vl-rerank`，向量维度保持 `1024`。
- 测试、日志、提交差异和文档不得包含真实 API Key。

## 验收标准

1. `uv sync` 在当前 Windows/Python 3.13 环境不再尝试构建 `python-snappy==0.6.0`。
2. 默认 pytest 不再出现系统临时目录、Windows 路径或 CRLF 相关失败。
3. 当前 12 个可复现失败全部修复，完整离线测试通过。
4. Ruff 与 strict Pyright 通过。
5. 默认测试不调用 DashScope；`pytest -m live_llm` 单独运行真实 Chat、Embedding、Rerank smoke tests。
6. Git 状态和提交历史不包含本地配置或真实凭据。
