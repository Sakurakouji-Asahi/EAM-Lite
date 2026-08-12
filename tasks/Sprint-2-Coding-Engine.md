# Codex Task — Sprint 2：可配置资产编码引擎

## 前置

- Sprint 0、1 已验收通过。
- 所有现有测试通过。
- PostgreSQL 测试环境可用于约束和迁移测试；正式发号并发将在 Asset 存在后的 Sprint 4 验收。
- Company、Department 和 AssetCategory 已存在且公司范围明确。

开始前完整阅读：

- `AGENTS.md`
- `docs/00-Requirements-Baseline.md`
- `docs/01-PRD.md`
- `docs/02-Business-Rules.md`
- `docs/03-Asset-Coding-Rules.md`
- `docs/04-Database-Design.md`
- `docs/05-UI-UX.md`
- `docs/07-Permissions-and-Workflows.md`
- `docs/10-Definition-of-Done.md`

本 Sprint 只完成编码规则、预览、版本保护，以及后续并发安全发号所需的 schema 和纯函数能力。

同时接入初始化向导步骤 6“编码规则”：只有存在一个结构校验通过、当前生效且唯一的公司默认编码方案时，才可把 `InitializationSetting.coding_scheme_configured` 设为 true。步骤 7 和最终完成仍未实现，本 Sprint 不得设置 `initialization_completed=true`。

## 关键边界

Asset 模型尚未在本 Sprint 建立，因此：

- 不得提供“脱离资产生成正式编号”的页面、API、管理命令或公开 Service。
- 预览和“生成 10 个示例”绝不消耗正式流水。
- 建立 `IssuedCode` 永久占号空表及约束，但本 Sprint 不创建任何业务 IssuedCode，不提交 counter 增量，也不实现可被调用的孤立分配原语。
- 通过迁移接通 `AssetCategory.default_coding_scheme`，只允许同公司、当前可选版本；system_admin 可配置，其他分类维护角色不能借此修改编码规则。
- 正式资产编号只有在 Sprint 4 财务确认时，才能与 Asset 状态变更、IssuedCode 绑定、AssetCodeHistory 和审计日志在同一事务中提交。
- Sprint 2 不得以“返回一个字符串”或无 Asset 的 IssuedCode 冒充完整正式发号。

## 模型

以修订后的数据库设计和编码规则为准，至少实现：

### AssetCodingScheme

- company
- name
- scheme_key
- version
- description
- status (`draft` / `active` / `retired`)
- is_default
- reset_mode
- sequence_start
- category_scope_level
- effective_from
- effective_to
- previous_version
- created_by
- created_at
- updated_at

要求：

- `(company, scheme_key, version)` 的唯一范围明确。
- 同一适用范围只能有一个有效默认版本。
- 同一 `scheme_key` 的 active 生效区间不得重叠。
- 已使用版本不能原地破坏性修改；编辑产生克隆的新版本。
- 旧版本及其已发编号永久保留。

### AssetCodingSegment

字段为 `coding_scheme, sequence_order, segment_type, fixed_value, format_string, sequence_length, zero_pad, created_at`。`segment_type` 至少支持：

- fixed_text
- company_code
- major_category_code
- minor_category_code
- category_code
- department_code
- year
- year_month
- full_date
- sequence
- custom_text
- separator

各类型所需的 `fixed_value` / `format_string` / `sequence_length` / `zero_pad` 组合必须与数据库 CHECK 和编码规范一致；`sequence_start` 只存于方案，不在片段重复保存。

要求：

- 每个方案必须且只能有一个 sequence。
- 同方案 sequence_order 唯一。
- V1 不实现 `custom_field` 片段，也不在枚举、API 或配置页暴露该选项；未来版本必须先补充明确来源字段、白名单取值器和迁移，绝不能对任意模型属性执行动态 `getattr`。
- 规则最大长度、允许字符、缺失来源字段和格式错误按编码文档明确验证。

### SequenceCounter

至少包含 company、coding_scheme、scope_key、current_value、created_at、updated_at。

要求：

- `(company, coding_scheme, scope_key)` 数据库复合唯一；`coding_scheme` 指向一个确定的版本记录。
- scope_key 由唯一 Service 按 reset_mode 和批准的公司/类别/日期维度规范生成，不在 View 中拼接。
- 明确 `sequence_start` 表示第一个可签发值，避免 off-by-one。
- 第一次创建 counter 的 `INSERT ... ON CONFLICT DO NOTHING` + `SELECT FOR UPDATE` 算法按编码规范形成可测试契约；只有 Sprint 4 的 Asset 发号事务可以实际提交 counter 更新。

### IssuedCode

按数据库设计建立不可复用的正式编号占用记录基础，至少保存：

- company
- coding_scheme（一个确定的方案版本）
- scope_key
- sequence_value
- display_code
- normalized_code
- effective_date
- effective_date_reason（历史回填时必填）
- status (`active` / `replaced` / `voided`)
- idempotency_key
- issued_by / issued_at
- replaced_or_voided_reason / replaced_or_voided_at

要求：

- `(company, normalized_code)`、`(company, coding_scheme, scope_key, sequence_value)` 和 `(company, idempotency_key)` 按文档落数据库唯一约束。
- 已提交的 IssuedCode 不得删除、改号或再次分配；本 Sprint 只建 schema/约束，不通过业务流程插入记录。
- Sprint 2 不提供业务侧独立创建入口。
- `AssetCodeHistory` 与 Asset 的最终绑定在 Asset 建立后接通；不得用单个自由文本历史编号替代永久占号。

## Reset 与 scope 规则

必须完整实现文档批准的：

- never
- yearly
- monthly
- category_yearly
- category_monthly
- 公司范围维度

effective_date 必须显式传入并使用 `Asia/Shanghai` 业务日期解释。不得用服务器当前时间偷偷替代用户批准的生效日期。

scope_key 应使用稳定 ID/规范化值，避免类别或公司显示名称变更导致意外开启新流水。具体序列化格式要集中定义、写入说明并有测试。

## 服务层

建立独立编码领域 Service，至少分离：

- 规则验证
- segment 渲染
- scope 计算
- 无消耗预览
- 正式发号所需的 renderer 和规范化 code 纯函数

复杂逻辑不得写在 View、Form 或模板中。

预览使用当前 counter 的只读快照或显式样例起点计算；不得锁定、创建或更新 SequenceCounter/IssuedCode。

`AssetCodeIssuer.issue(asset, ...)` 的正式事务入口留到 Sprint 4；Sprint 2 不注册、不隐藏预留一个可绕过 Asset 的入口。

## UI

system_admin 可：

- 新增编码方案草稿
- 编辑从未使用的版本
- 克隆已使用版本为新版本
- 启用/停用
- 设置唯一默认方案
- 添加、删除和调整 segment 顺序
- 配置流水位数、起始值、补零及 reset mode
- 查看实时预览
- 生成 10 个不消耗流水的示例
- 为物理分类选择同公司的默认编码方案版本

界面必须明确标识：草稿、有效版本、历史版本、已使用不可改。

本 Sprint 不显示“正式生成编号”按钮。

同一编码页面作为 `/setup/` 第 6 步使用；完成条件必须查询实际活动方案，不因访问页面或仅保存 draft 而通过。方案被退役导致公司没有当前默认版本时，完成标记必须重新变为未通过并写审计。

## 权限与审计

- 只有 `docs/07-Permissions-and-Workflows.md` 授权角色可新增、版本化、启停和设置默认方案。
- 其他允许角色只能按矩阵查看。
- 所有 View、Service 和直接 POST 都做后端权限校验。
- 方案新增、克隆、启用、停用、默认变更和 segment 变化写入 AuditLog。
- 不在审计中记录安全 Token 或不必要的完整请求体。

## 事务与并发契约

本 Sprint 的方案启用、默认版本切换和版本克隆在事务中完成，并由唯一/排斥约束防止两个重叠活动默认版本。

正式发号的 PostgreSQL 事务契约必须按 `docs/03-Asset-Coding-Rules.md` 固化在 Service 设计和 Sprint 4 接口中：锁 Asset、解析具体方案、幂等建立并锁 counter、递增、渲染、写 IssuedCode、绑定 Asset、写 CodeHistory/Audit 后一起提交。本 Sprint 因没有 Asset 不执行或提交该流程。

禁止：

- `MAX(asset_code) + 1`
- 无 Asset 时提交 counter 或 IssuedCode
- 捕获唯一冲突后无限重试
- 预览写 counter
- 用 SQLite 证明未来行锁正确

端到端 first-row race、同行锁、幂等键和失败回滚在 Sprint 4 使用真实 Asset 与 PostgreSQL 并发连接验收。

## 自动测试

至少覆盖：

1. 固定文本、分隔符和非法字符。
2. 公司、父/子类别、部门来源。
3. 年、年月、完整日期。
4. 4 位、5 位及配置的补零/不补零行为。
5. sequence_start 的首个签发值无 off-by-one。
6. never、yearly、monthly、category_yearly、category_monthly。
7. 公司和类别 scope 相互隔离。
8. 缺失 required source 明确报错。
9. 必须且只能有一个 sequence。
10. segment 顺序唯一、最大长度、`custom_text` 固定值校验，以及 `custom_field` UI/API 拒绝。
11. 实时预览不写 counter。
12. 10 个示例连续且不消耗 counter。
13. AssetCategory 默认方案只能选同公司可用版本；停用/未生效方案不能被选为正式方案，预览明确标注非正式。
14. 唯一默认版本约束。
15. 版本克隆链、有效期不重叠和旧版本不变；新版本从自身 sequence_start 首发，续号只能在 draft 版本明确设置下一个 sequence_start，不能预建/复制 Counter。
16. SequenceCounter、IssuedCode 的复合唯一、状态和不可删除约束在 PostgreSQL 生效。
17. 本 Sprint 所有预览/规则操作后 SequenceCounter、IssuedCode 均保持 0 行。
18. 无权限用户无法修改规则；不存在正式发号 URL/API/命令/Service。
19. 方案启用/默认切换并发不会产生两个活动默认版本。
20. 初始化步骤 6 只有当前有效唯一默认方案时通过；只有 draft、过期或重叠方案时不通过，整体初始化仍未完成。
21. 规则变化和步骤完成状态变化产生审计日志。
22. Sprint 0–1 回归测试通过。

PostgreSQL 用于约束和默认版本并发测试。正式编号并发不在无 Asset 的 Sprint 2 伪测，必须在 Sprint 4 完成。

## 本 Sprint 排除

不得开发：

- Asset 主档
- 面向用户的正式发号入口
- 财务确认和资产状态迁移
- 正式编号修改业务流程
- 折旧、导入、生命周期、QR、盘点、保养、报表

## 验收场景

1. 管理员不改 Python 代码即可建立两套不同规则并看到实时预览。
2. “生成 10 个示例”前后 SequenceCounter 和 IssuedCode 行数完全不变。
3. 已使用版本只能克隆，旧版本及示例保持不变。
4. PostgreSQL 并发启用/默认切换后只有一个有效默认版本。
5. UI、URL、管理命令和 Service 均不存在脱离 Asset 的正式发号入口，SequenceCounter/IssuedCode 保持空表。
6. 当前默认方案生效后向导步骤 6 通过，但步骤 7/9 未完成且系统仍不得进入整体已初始化状态。

## 完成与停止条件

- 规则、版本、预览、schema 约束、权限和审计全部通过。
- Completion Report 必须明确写明：“正式资产发号尚未启用，将在 Sprint 4 财务确认事务中接通”。
- PostgreSQL 约束及默认版本并发测试若不可运行，本 Sprint 不得完成；正式发号并发不得声称已完成。
- 满足 `docs/10-Definition-of-Done.md`。

汇报后立即停止，不得开始 Sprint 3。
