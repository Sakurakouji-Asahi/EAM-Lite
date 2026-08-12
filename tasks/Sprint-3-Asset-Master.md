# Codex Task — Sprint 3：资产主档

## 前置

- Sprint 0–2 已验收通过，现有测试全部通过。
- 基础资料、权限、审计和编码规则预览可用。
- 尚未启用正式资产发号。

开始前完整阅读：

- `AGENTS.md`
- `docs/00-Requirements-Baseline.md`
- `docs/01-PRD.md`
- `docs/02-Business-Rules.md`
- `docs/03-Asset-Coding-Rules.md`
- `docs/04-Database-Design.md`
- `docs/05-UI-UX.md`
- `docs/07-Permissions-and-Workflows.md`
- `docs/09-Security-Backup-and-Deployment.md`
- `docs/10-Definition-of-Done.md`

本 Sprint 建立资产实物主档、动态字段、附件和台账页面，只做到“草稿→待财务确认”。财务确认、正式编号和折旧在 Sprint 4。

初始化步骤 7/9 尚未完成，因此生产式入口继续受“初始化未完成”门槛保护。Sprint 3 的页面/Service 验收使用明确的测试 fixture 模拟已完成门槛，不得修改真实 `InitializationSetting`、跳过后端权限或把不完整初始化标记为完成；Sprint 4 完成步骤 7/9 后再做正常入口回归。

## 范围

实现：

- Asset
- AssetCustomField
- AssetCustomValue
- AssetCodeHistory schema 及 Asset.current_issued_code 的 nullable 关联；本 Sprint 不创建首发历史
- 复用 Sprint 1 的通用 Attachment，并实现 AttachmentLink 的资产用法
- 资产草稿新增、编辑、详情、列表、搜索和筛选
- 提交财务确认
- 资产当前部门、责任人和树形位置
- 权限化字段展示和审计日志
- 与 `IssuedCode`/`AssetCodeHistory` 的受保护关联迁移，但保持空值且不正式发号

## 模型与约束

### Asset

字段、状态和公司范围以数据库设计为准。至少支持：

- 业务 UUID/主键
- nullable 的 asset_code 与 current_issued_code（草稿及待财务确认阶段均为空）
- nullable 的 requested_coding_scheme（system_admin 可在正式化前明确选择具体活动版本）
- asset_status、record_status
- asset_name、category、品牌、型号、厂家、序列号、出厂编号、历史参考编号
- quantity、unit；V1 全部资产按单件追踪
- company、department、responsible_employee、location
- acquisition/commissioning 等已批准实物日期
- is_maintenance_required；会计认定和是否折旧不在 Asset 重复保存，留待 Sprint 4 的 AssetFinance/Profile
- cover attachment
- initialization source/date
- created/updated user 与时间

要求：

- 正式编号为空与空字符串严格区分；未发号不得保存 `""` 作为伪编号。
- asset_code 与 current_issued_code 的提交时一致性按数据库设计约束；Sprint 3 所有资产两者均为空。
- requested_coding_scheme 同公司，仅 system_admin 可在 draft/pending_finance 设置；普通业务角色只能看到解析提示，正式化后不可改。
- V1 所有正式资产均为单件追踪，quantity 恒为 1。
- 不建立 batch_quantity、部分数量调拨或部分数量处置；若保留 tracking_mode，只能为不可变的 single_item，若无未来兼容价值可不建该字段。
- 物理 AssetCategory 与会计固定资产分类分离。
- in_use 等正式状态的责任、部门、位置约束尚不应被草稿绕过；完整状态机按权限文档实现。
- 正式资产和历史不得被级联删除。草稿删除权限及审计按文档执行。

### 动态字段

- field_type 只允许 `text/decimal/date/boolean/select`；select 的 options_json 必须是去重非空字符串数组，其他类型 options_json 必须为空。
- Value 列映射严格使用数据库设计，select 值必须属于批准选项，required 在提交财务确认时校验。
- code 在适用范围唯一。
- 每资产/字段值唯一。
- 不用任意 Python 表达式或动态代码执行校验。
- 类别变化时对既有必填值给出明确迁移/阻止策略，不静默丢值。

### 附件

使用修订后的通用 Attachment + AttachmentLink，使附件可安全关联不同业务对象。

资产附件至少支持照片、发票、合同、验收单、说明书、合格证和其他。要求：

- 上传校验扩展名、实际 MIME、大小和空文件。
- 存储文件名不可直接信任用户文件名。
- 下载必须经过后端对象权限检查；不得将媒体目录无条件公开。
- AttachmentLink 必须保存 A0/A1 安全分类；A1 财务附件只有 finance 可创建/作废，finance/management 可按矩阵查看，其他角色即使可看资产也不得获得文件或元数据敏感值。
- 草稿孤儿可按保留策略清理；已提交/正式附件只能把 AttachmentLink 标为 voided，记录原因/人/时间并保留文件和元数据。替换封面不得物理丢失旧证据。

## 流程

### 新建草稿

授权用户创建实物草稿，可暂缺责任人、位置和财务信息。系统记录创建人和来源。

### 编辑草稿

只能编辑权限和状态允许的字段。普通实物管理角色不能写原值、折旧、净值等财务字段，即使构造 POST 也应被拒绝/忽略并记录安全事件。

### 提交财务确认

Service 在事务中：

1. 锁定草稿资产。
2. 校验公司、类别、quantity=1、部门/责任人/位置、至少一张有效资产照片及其他必要字段。
3. 将状态迁移为 pending_finance。
4. 写入审计日志。

此操作不得生成正式编号、不得创建已确认 AssetFinance、不得进入 pending_label。

### 台账与详情

提供 UI 文档要求的列表、搜索、筛选和详情基础标签页。未完成模块显示明确“后续 Sprint”状态，不创建伪数据。

位置 UI 允许层级联动，但资产只保存选中的规范位置节点，祖先从树推导。

## 权限与数据范围

按 `docs/07-Permissions-and-Workflows.md`：

- system_admin 可查看公司全域非财务数据，但没有单独的资产草稿新增/编辑权；需另授 finance/equipment 等批准角色才能操作业务资产。
- equipment、warehouse、department_manager、employee 等按角色与部门范围查看/维护允许字段。
- finance 可查看待确认资产，但财务确认留到 Sprint 4。
- management 的财务字段可见性严格按矩阵。
- 附件查看和下载继承对象权限及附件类型权限。

所有列表 queryset、详情、编辑、附件下载和 Service 都实施后端对象级/数据范围校验。

## 事务与审计

- 提交、类别改变、责任/位置初始设置和附件业务关联使用明确事务。
- 并发提交同一草稿只能成功一次，第二次返回当前状态而不重复写历史。
- 资产新增、关键字段变化、提交、撤回、草稿删除和附件变化写 AuditLog；正式资产归档/恢复属于 Sprint 7，当前不得向尚不存在的终态提供入口。
- 不允许绕过 Service 直接把状态改为 pending_label/in_use。

## 自动测试

至少覆盖：

1. 草稿 asset_code 为 NULL，不使用空字符串。
2. 所有资产 quantity 只能为 1，批量数量输入被拒绝。
3. 不存在部分数量资产、部分调拨或部分处置入口。
4. 物理分类与固定资产属性可独立表达。
5. 动态字段 `text/decimal/date/boolean/select` 的精确值列映射、required、select options 组合/成员及唯一值约束；未知类型和错列值被拒绝。
6. 跨公司/跨部门责任人和位置被拒绝。
7. 树形位置只存一个节点并正确展示路径。
8. 草稿允许缺责任人，提交时必须具备部门、责任人、位置和至少一张有效资产照片。
9. 提交只到 pending_finance，不生成 code/IssuedCode/AssetCodeHistory。
10. 非法状态跳转被拒绝。
11. 并发重复提交不产生重复记录。
12. 搜索 asset name/model/serial/responsible employee。
13. category、department、employee、location、status 等筛选。
14. 非 system_admin 修改 requested_coding_scheme 被拒绝；跨公司/失效版本被拒绝且不发号。
15. 财务字段 POST 越权被拒绝。
16. 跨部门列表、详情和附件下载被拒绝；A1 对非 finance/management 不返回内容或敏感元数据。
17. 上传类型、MIME、大小和危险文件名校验。
18. 资产、编码方案选择、状态和附件变化产生审计日志且无秘密泄露。
19. 正式/引用资产不被级联删除。
20. Sprint 0–2 回归测试通过。

## 本 Sprint 排除

- 财务确认、AssetFinance、折旧政策和折旧计算
- 正式编号签发、贴标和 in_use
- 初始化资产 Excel 导入
- 调拨、领用、借出、报废
- QR、盘点、保养、离职、报表、T+

## 验收场景

1. 在明确的 Sprint 3 验收 fixture 中，设备角色创建单件设备草稿，上传照片、选择三级位置并提交财务确认；真实未完成初始化的普通入口仍被阻断。
2. 提交后状态为待财务确认，asset_code/current_issued_code 仍为空，IssuedCode/AssetCodeHistory 未增加。
3. 无权部门用户看不到该资产；构造详情和附件 URL 也被拒绝。
4. 财务用户能看到待确认资产，但本 Sprint 没有确认/发号按钮。
5. 管理员输入 quantity=2 或尝试批量数量追踪时被明确拒绝。

## 完成与停止条件

- 主档、页面、附件、权限、事务和测试全部通过。
- 空库与 Sprint 2 数据库升级迁移通过。
- 满足 `docs/10-Definition-of-Done.md`。
- Completion Report 明确说明正式编号和财务确认尚未启用。

汇报后停止，不得开始 Sprint 4。
