# Codex Task — Sprint 11：报表、Dashboard 与 T+ 对账导出

## 前置

- Sprint 0–10 已验收通过，完整测试通过。
- 资产、财务、折旧、生命周期、二维码、盘点、保养和离职数据口径稳定。
- `docs/11-Tplus-Reconciliation-Export.md` 已批准且无未决字段映射。

开始前完整阅读：

- `AGENTS.md`
- `docs/00-Requirements-Baseline.md`
- `docs/01-PRD.md`
- `docs/02-Business-Rules.md`
- `docs/04-Database-Design.md`
- `docs/05-UI-UX.md`
- `docs/07-Permissions-and-Workflows.md`
- `docs/08-Depreciation-Calculation-Spec.md`
- `docs/10-Definition-of-Done.md`
- `docs/11-Tplus-Reconciliation-Export.md`
- `docs/12-UAT-Acceptance.md`（仅使用已批准的性能阈值）

本 Sprint 只做查询、Dashboard、Excel 报表和 T+ 人工对账文件。不得调用 T+ API、写 T+ 数据库或自动生成正式凭证。

## 范围

至少实现：

- 公司资产总账
- 固定资产明细
- 折旧计划/明细/月度计提报表
- 部门资产、人员资产
- 设备清单、模具工具检具清单
- 盘点结果和盘点差异
- 保养计划、到期和完成记录
- 离职资产未清
- 报废/出售/处置清单
- AssetExternalReference 中 T+ 资产卡片编码的财务维护入口
- 权限化 AuditLog“操作日志”查询页面（只读，不导出、不修改）
- 权限化 Dashboard
- T+ 月末人工核对导出
- ExportLog、按版本注册的 Decimal ExportLogTotal 合计明细及生成时点

## 报表数据口径

- 正式资产和草稿是否纳入、状态筛选、基准日期均明确显示。
- 资产当前归属来自 Asset；历史时点报表必须使用有效期/历史记录，不能用当前值冒充历史。
- 财务原值、累计折旧、净值和本期折旧来自文档指定的 confirmed 权威记录。
- theoretical 与 actual 明确分列，默认 T+ 核对使用批准的实际口径。
- 处置报表使用 AssetDisposal 不可变快照，不用当前缓存反算。
- 盘点报表使用任务快照和有效扫描，不因后续调拨变化。
- 所有金额使用 Decimal，所有日期按 `Asia/Shanghai` 业务日期解释。
- 报表显示 generated_at、筛选条件和数据截止时间。

不得为方便导出复制一套会漂移的财务计算逻辑；复用已有领域查询/计算 Service。

## Excel 输出

要求：

- `.xlsx` 标准文件，工作表名和列顺序稳定并有版本。
- 金额、数量保持数值单元格，不导出为带逗号的文本。
- 日期保持 Excel 日期单元格并有显示格式。
- code、编号、序列号等标识字段保持文本，保留前导零且防公式注入。
- 中文表头、筛选、冻结表头和合理列宽。
- 空值、0、无折旧、已处置等口径明确。
- 大数据量采用受控内存策略；初始 150 项和目标 5,000 项均可稳定导出。
- 文件名包含报表类型和业务日期，不含用户输入的危险路径字符。

对以 `=`, `+`, `-`, `@` 开头的非数值用户文本采取安全写入，防止 spreadsheet formula injection。

## T+ 对账导出

完全遵循 `docs/11-Tplus-Reconciliation-Export.md`：

- 使用批准字段映射、期间口径、方向和汇总/明细层级。
- 文件显著标注“对账导出，不是 T+ 导入凭证/自动入账结果”。
- 导出前校验期间是否存在未确认折旧、冲销链异常或缺失财务数据，并给出阻断/警告明细。
- 保存导出人、期间、筛选、文件摘要/hash、行数、生成时间，以及数据库设计规定的逐项 Decimal ExportLogTotal；T+ 合计至少覆盖原值、期初/期末累计折旧、自动/手工、调整净额、冲销净额、减值、净值和处置收入，不得塞入 filters_json。
- 同条件重复导出可重现或明确显示数据版本差异。
- 不存储、不索取 T+ 数据库凭据。
- 不直接修改 T+ 数据库，不创建 API posting。

`AssetExternalReference` 只保存财务人工核对得到的 `external_system='TPLUS'`、`reference_type='asset_card_code'` 引用。只有 finance 可新增/更正，management 只读；同一资产同类型最多一条，同一公司同类型代码不得重复。更正写 old/new 和原因审计，但不得据此调用 T+、自动认领记账状态或把外部编码当 EAM 主键。

## Dashboard

按 PRD/UI/权限文档实现：

- 财务：固定资产原值、累计折旧、净值、本月折旧。
- 实物：资产总数、在用、闲置、报废数量。
- 待办：待财务确认、待贴标、待盘点/异常、即将/逾期保养、离职未清。
- 部门、类别图表。

每张卡和图表使用统一筛选及数据范围。无财务权限角色不能通过页面、HTMX endpoint、图表 JSON 或缓存键获得财务金额。

## 操作日志查询

实现菜单中已有但此前仅具备写入基础的“操作日志”页面。只读分页查询 AuditLog，支持上海时间范围、操作者、action、object_type、object_id 和 correlation_id 精确筛选；默认最近 7 天，必须限制最大页大小，禁止任意 SQL、修改、删除或普通文件导出。

权限严格使用矩阵：system_admin 可看公司全域脱敏记录；finance 可看本人记录及权限文档逐字列出的财务 object_type；hr 可看本人记录及 `Employee/EmployeeAssetClearance/EmployeeAssetClearanceItem`；其他角色拒绝。对象类型映射使用服务端固定 registry，不接受别名、自由文本或用户构造 object_type 扩大范围。old/new 仍按当前字段权限二次脱敏，Token、Secret、Cookie、文件内容和无权 A1 数据永不回显。

## 权限与数据范围

按 `docs/07-Permissions-and-Workflows.md`：

- finance 查看/导出财务与折旧报表及 T+ 对账文件。
- equipment/department_manager 查看批准的实物范围。
- employee 只查看本人/允许范围资产。
- management 按矩阵查看公司级汇总，不自动获得编辑或敏感下载权限。
- system_admin 的技术管理权限不替代 finance 业务权限，按文档执行。

所有筛选参数、导出后台入口、文件下载和重复下载均重新校验权限。导出及敏感下载写 AuditLog。

## 一致性与事务

- 单次导出在明确数据快照/事务边界中计算表头合计和明细，避免生成过程中计提导致合计与明细不一致。
- 不长期锁住业务表；采用数据库和规模适合的一致性读取策略并记录 generated_at。
- 导出失败不留下可下载的半文件；文件、ExportLog、该 schema 全部 ExportLogTotal、摘要和成功状态原子发布。
- 重试使用幂等键或创建明确的新版本，不覆盖已有已审计文件。

## 自动测试

至少覆盖：

1. 每类报表的列、筛选、状态和数据口径。
2. 当前归属与历史时点归属区分。
3. confirmed actual 与 theoretical 分列。
4. 处置快照和盘点快照口径。
5. Excel 金额为数值、日期为日期、标识为文本。
6. 前导零保持，用户文本公式注入被防护。
7. 合计等于明细的 Decimal 精确和；ExportLogTotal 的固定 metric registry、NUMERIC 类型、schema 版本和必填集合与页面/工作簿一致，缺项/未知 key/JSON 代替金额均失败。
8. 空数据、0 值、已处置和无折旧资产。
9. AssetExternalReference 的同资产/同类型及公司内代码唯一、finance 写/management 只读/其他角色拒绝，并保留更正审计。
10. T+ 字段映射、期间、行数和合计符合 docs/11，外部卡片编码与数值型处置收入正确带出；冲销净额保留代数符号，正折旧冲销和负调整冲销都能按加法恒等式勾稽，且 cost_correction/减值不混入累计折旧调整列。
11. 未确认折旧/缺失财务数据的阻断或警告。
12. 无 T+ API/数据库连接代码或凭据要求。
13. Dashboard 各角色卡片和数据范围。
14. HTMX/JSON/下载 URL 不泄露财务字段。
15. 跨部门筛选参数篡改被拒绝。
16. 操作日志按 system_admin 全域、finance 本人/财务白名单、hr 人员清退/本人动作精确过滤；其他角色、伪造 object_type/object_id、超大分页均被拒绝，old/new 二次脱敏。
17. 导出失败无半文件/半套 Total，成功文件、ExportLog、全部合计行和摘要匹配。
18. 同条件重复导出的版本/摘要行为明确。
19. 5,000 项资产数据集的关键报表查询、操作日志分页和 Excel 导出性能冒烟。
20. 外部引用维护、导出、下载和 T+ 对账文件生成产生审计日志。
21. Sprint 0–10 回归测试通过。

## 本 Sprint 排除

- T+ API、数据库写入、自动凭证、自动入账
- 税务折旧和税务申报
- 自助报表设计器/任意 SQL
- 公网分享链接
- 生产备份恢复和最终 UAT

## 验收场景

1. finance 选择一个月，导出固定资产和折旧明细；Excel 金额可直接求和，日期可筛选。
2. 按 docs/11 生成 T+ 对账文件，页面显示行数与金额合计且标明仅供人工核对。
3. 部门经理只能导出本部门实物资产，不能通过 URL 获取财务文件。
4. Dashboard 的报废数量、待贴标和逾期保养与对应明细一致。
5. 5,000 项资产基准数据下完成关键查询/导出，结果和资源使用满足 `docs/12-UAT-Acceptance.md` 的验收阈值。

## 完成与停止条件

- 所有报表、Excel 单元格类型、权限、T+ 映射、一致性和性能验证通过。
- `docs/11-Tplus-Reconciliation-Export.md` 若仍有未决字段，必须报告阻塞，不能自行猜测。
- 满足 `docs/10-Definition-of-Done.md`。

汇报后停止，不得开始 Sprint 12。
