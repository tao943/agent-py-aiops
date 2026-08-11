## ADDED Requirements

### Requirement: Evaluation repository boundary

后端 SHALL 通过数据库无关的 Repository 协议和 Record 保存评测运行与一对一结果，业务代码 MUST NOT 依赖 SQLAlchemy ORM 类型。

#### Scenario: Evaluation result is persisted

- **WHEN** runner 完成一个评测并计算 scorecard
- **THEN** Repository MUST 在 PostgreSQL 保存版本化运行元数据、维度分数、有效性、通过状态、失败项与得分理由。

#### Scenario: Duplicate run conflicts

- **WHEN** 相同 run ID 被用于不同场景或 Agent 版本
- **THEN** Repository MUST 拒绝覆盖已有运行。
