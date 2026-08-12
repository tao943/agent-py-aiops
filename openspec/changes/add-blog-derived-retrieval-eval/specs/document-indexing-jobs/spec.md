## ADDED Requirements

### Requirement: Changed reviewed documents overwrite by scoped filename

知识文档上传在 `overwrite=true` 时 SHALL 在相同 owner、knowledge base 和 filename 范围内替换活动文档，即使内容哈希已经变化。替换 MUST 先删除旧文档 scoped vectors、软删除旧元数据，再创建并返回新文档；`overwrite=false` 的现有相同内容冲突行为保持不变。

#### Scenario: Reviewed card content changes under the same filename

- **WHEN** owner 使用 `overwrite=true` 上传一个与活动文档同名但内容不同的 Markdown
- **THEN** 系统 MUST 返回新 document ID、标记旧文档已删除，并 MUST 只保留一个该文件名的活动文档

#### Scenario: Different filename has changed content

- **WHEN** owner 使用 `overwrite=true` 上传内容不同且文件名不同的 Markdown
- **THEN** 系统 MUST 创建独立文档且 MUST NOT 替换无关文件名的活动文档

#### Scenario: Legacy duplicate filenames are detected

- **WHEN** 真实导入前发现同一范围内已经有多个活动同名文档
- **THEN** 运维流程 MUST 停止并报告重复项，且 MUST NOT 静默批量删除历史文档
