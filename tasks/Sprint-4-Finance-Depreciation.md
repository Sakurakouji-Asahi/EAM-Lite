# Codex Task — Sprint 4：财务确认与折旧引擎

## 前置

- Sprint 0–3 已验收通过，完整回归测试通过。
- Sprint 3 的明确测试 fixture 能构造 `pending_finance` 资产；干净真实库不要求预先已有此类资产，因为初始化步骤 7/9 正由本 Sprint 完成。
- PostgreSQL 并发测试环境可用。

财务确认能力可先使用上述 fixture 开发和验证，但不得把 fixture 当生产入口。完成初始化步骤 7/9 后，必须立即通过 Sprint 3 正常业务入口新建并提交真实 `pending_finance` 资产，再执行端到端回归；正式发号始终只能依附该资产确认事务。

开始前完整阅读：

- `AGENTS.md`
- `docs/00-Requirements-Baseline.md`
- `docs/01-PRD.md`
- `docs/02-Business-Rules.md`
- `docs/03-Asset-Coding-Rules.md`
- `docs/04-Database-Design.md`
- `docs/05-UI-UX.md`
- `docs/07-Permissions-and-Workflows.md`
- `docs/08-Depreciation-Calculation-Spec.md`
- `docs/09-Security-Backup-and-Deployment.md`
- `docs/10-Definition-of-Done.md`

金额、日期、舍入和六种折旧方法必须以 `docs/08-Depreciation-Calculation-Spec.md` 为唯一计算口径，不得自行补公式。

## 范围

实现：

- FixedAssetCategory
- AssetFinance
- DepreciationPolicy
- AssetDepreciationProfile
- DepreciationSchedule
- DepreciationProfileEvent 基础 `suspend/resume/stop`；处置来源真实 FK 与 disposal_stop/restore 由 AssetDisposal 存在后的 Sprint 7 跟踪迁移接入，不提前造裸 ID
- DepreciationEntry
- AssetWorkUsage
- AssetValueAdjustment
- DepreciationBatch / DepreciationBatchItem 等数据库设计要求的月度确认批次模型
- TheoreticalDepreciationRun / TheoreticalDepreciationLine
- 只向 finance 开放 Sprint 1 SystemSetting registry 中的 `fixed_asset_warning_amount`；折旧默认全部使用版本化 DepreciationPolicy，不创建重复 key
- 通过迁移接通 AssetCategory.default_depreciation_policy（finance 配置、同公司可用版本）
- AssetQrIdentity 的最小安全身份模型；打印批次和贴标 UI 留到 Sprint 6
- 财务确认与正式编号原子签发
- 折旧政策、试算、确认、月度/年度计提、手工折旧、工作量录入
- 暂停、恢复、停止、冲销和调整
- 期初实际累计折旧及理论历史折旧参考
- 初始化向导步骤 7“折旧规则与财务参数”和步骤 9“校验并完成”

## 财务数据边界

- Asset 保存实物当前状态和经批准的必要标记。
- AssetFinance 保存会计属性及当前财务口径。
- DepreciationProfile 保存某一有效期的不可变折旧参数版本。
- Schedule 是计划/理论结果。
- confirmed Entry 是实际已确认历史的来源。
- current book value 等缓存字段如存在，只能由领域 Service 从权威 Entry 更新，不得允许表单独立编辑。

同一含义的重复字段必须明确唯一来源和同步方向；不得由多个页面各自写值。

## 可配置财务规则

固定 registry 的系统设置或版本化政策支持：

- 固定资产提示阈值，当前参考 5,000 CNY，仅警告不自动认定。
- 残值百分比或固定残值金额，当前默认 5%，可配置。
- 当前月、次月、指定月份、指定日期起算。
- 月度/年度期间。
- 政策优先级：单项资产 > 资产类别默认 > 系统默认。

数据库/API/导入 token 固定为 `posting_period=monthly/yearly`、`salvage_mode=rate/amount`；不得另造同义枚举。rate/amount 对应字段互斥，yearly 才填写 annual_posting_month。

公司系统默认由唯一当前 active `DepreciationPolicy.is_default=true` 表达，不把政策 UUID 塞进自由文本 SystemSetting。分类默认和公司默认必须同公司、在生效日可用；单项已明确但失效时阻止确认，不静默换政策。

政策解析为单项明确政策 → 实物类别默认政策 → 公司默认政策。选定后，具体参数优先使用 finance 在单项确认中显式填写的值；useful_life_months 可再取会计 FixedAssetCategory 默认；其余或仍缺失的寿命取选定 Policy 默认。最终全部固化到 Profile。Company.currency/timezone、Policy 默认和 SystemSetting 阈值各有唯一职责，禁止增加 `currency`、`business_timezone`、`default_salvage_rate` 等重复设置。

只有 finance 授权用户可最终确认是否固定资产及修改财务字段。

未确认只用 NULL/尚无 AssetFinance 行表达，不把 `unconfirmed` 保存为 accounting_treatment 枚举。无论最终认定为 fixed_asset 还是 controlled_non_fixed，正式化都要求 `original_cost` 非空、使用 Decimal 且不小于 0。controlled_non_fixed 不建立 active 折旧 Profile/Entry，累计折旧和减值固定为 0，但仍保存原值供处置与对账；不得把“非固定资产”解释为“可以没有成本”。

## 折旧方法

全部实现并按计算规范测试：

1. straight_line
2. units_of_production
3. double_declining_balance
4. sum_of_years_digits
5. manual
6. no_depreciation

必须使用 Decimal，明确金额精度和舍入点。累计折旧不得低于 0 或越过可折旧金额/残值底线，最后一期按规范修正。

工作量法必须维护预计总工作量、单位、当期和累计工作量；重复期间、单位不一致及累计倒退被拒绝。

manual 方法的批次明细使用结构化 Decimal 金额、原因、录入人和时间字段；不得只放 calculation_snapshot_json。空值是错误，明确 0 也必须有原因。

## 财务确认与正式发号

财务确认不是通用审批流。唯一确认 Service 必须在一个 PostgreSQL 原子事务内：

1. 校验 finance 权限并锁定 pending_finance Asset。
2. 校验责任人、部门、位置、quantity=1、类别及财务字段。
3. 明确固定资产/非固定资产分类。
4. 对需折旧资产建立并确认 Finance/Profile 及可复现试算结果。
5. 校验 finance 显式确认的正式编号生效日期（默认当前上海业务日、不得未来；旧资产历史日期必须有原因），再按 requested 具体版本 → 物理类别默认 → 公司默认顺序解析并锁定方案版本和 scope；已明确 requested 版本失效时直接失败，不静默换方案。
6. 锁定/创建 SequenceCounter 并取得下一个号码。
7. 创建唯一且不可复用的 IssuedCode。
8. 写 Asset.asset_code，并建立 AssetCodeHistory 的初始正式编号记录。
9. 使用密码学安全随机源创建唯一 AssetQrIdentity/Token，标签状态置为 ready_to_print；Token 不包含资产详情。
10. 状态迁移到 pending_label。
11. 写财务确认、发号和二维码身份创建 AuditLog，但不记录 Token 原文。

任一步失败，AssetFinance/Profile、counter、IssuedCode、Asset code、QR identity/Token、状态和审计全部回滚。不得先发号后保存资产，也不得返回未被 IssuedCode 永久保护的正式编号。

并发确认同一资产只能成功一次；并发确认不同资产不得产生重复编号。

## 试算与方案确认

财务确认前展示：原值、残值、可折旧金额、方法、期间、起止日期和完整 schedule。

- “返回修改”不锁定历史，不消耗编号。
- “确认折旧方案”创建版本化 Profile/Schedule。
- 已确认 Profile 不原地覆盖；参数变更通过新有效期版本。
- 理论历史试算与实际期初承接并列显示，理论值永不自动覆盖实际账面值。

## 计提、冲销与调整

- 月度/年度计提通过批次 Service 执行，批次状态和可重试行为按数据库/计算规范。
- 同资产同期间只允许一个未被冲销的 confirmed regular 批次来源 Entry；opening 来源、每个明确调整来源和一对一反向 Entry 分别按数据库唯一键控制。实际累计折旧必须汇总原始及反向的全部 Entry，不能为满足“单条”而丢弃合法调整/冲销。
- 已确认 Entry 不可编辑/删除；错误通过 reversal 链和新调整 Entry 处理。
- 批次失败不得留下部分 confirmed 与部分未处理的模糊状态；采用文档定义的批次/单项事务策略并记录结果。
- 暂停期间、停止日期、恢复及剩余年限按计算规范生成后续计划，不重写既有 confirmed Entry。
- 资产价值调整保存 old/new、影响额、原因、操作者和生效日期。

## UI

提供：

- 折旧政策管理
- 待财务确认资产列表及确认页面
- 折旧试算和确认
- 资产财务信息/折旧标签页
- 工作量录入
- 月度折旧批次、结果和失败明细
- 暂停/恢复/停止
- 冲销和调整入口
- `/setup/` 步骤 7 的折旧政策/财务默认值配置，以及步骤 9 的逐项校验与完成页

页面使用中文，危险操作二次确认并要求原因。非财务角色按权限文档只见允许的摘要或完全不可见。

步骤 7 只能由 finance 写批准的财务参数/默认政策；system_admin 可协调和查看通过状态但不能代填财务业务值。只有实际存在可用默认政策、5,000 提示值、残值默认、起算/期间设置且校验通过时，才设置 `finance_rules_configured=true`。

步骤 9 由 system_admin 发起最终校验 Service：重新查询公司、部门、员工、分类、具体位置、当前编码方案、财务默认、固定角色用户和部门范围等九项真实条件。全部通过才可原子设置 `initialization_completed=true`、完成者/时间并写 AuditLog；任一失败则保持未完成并返回逐项修复链接。不得只信任旧布尔标记。完成初始化后立即回归 Sprint 3 正常业务入口。

## 权限与审计

- finance 按矩阵维护财务规则、确认、计提、冲销和调整。
- system_admin 的系统权限不自动等于财务业务确认权限，按权限文档执行。
- equipment、department_manager、employee、warehouse 不得通过 POST、导入或 Service 写财务字段。
- management 的查看范围按矩阵，默认无编辑能力。
- 财务确认、政策版本、计提确认、冲销、调整、暂停/恢复/停止均写 AuditLog。

## 自动测试

至少覆盖：

1. 5,000 阈值仅警告，财务可明确确认不同分类；高于阈值选择 controlled_non_fixed 时原因必填并留存；两种认定原值为空/负数均拒绝，controlled_non_fixed 原值 0 可确认且不生成折旧/减值记录。
2. 默认 5%、次月、月度通过 DepreciationPolicy 配置覆盖；SystemSetting 只接受 finance 的 warning amount，未知或 currency/timezone/default_salvage 等重复 key 被拒绝；posting_period 只接受 monthly/yearly、salvage_mode 只接受 rate/amount 且字段互斥。
3. 政策选择按单项 > 实物类别 > 公司唯一 active default；参数按单项显式值 > 会计类别 useful life > 已选 Policy 默认，其他参数不读取会计类别；缺失、跨公司、失效或多个默认被拒绝。
4. 六种方法的规范样例。
5. Decimal 舍入、最后一期修正和残值底线。
6. 当月、次月、指定月、指定日起算。
7. monthly/yearly 期间及 annual_posting_month 的必填/互斥组合；API/导入同义或未知 token 被拒绝。
8. 工作量单位、当期/累计、上限和重复期间。
9. 期初实际累计折旧及理论参考不互相覆盖。
10. 已确认 Profile/Entry 不可原地修改或删除。
11. 暂停、恢复、停止和新有效期版本不改历史。
12. 冲销链、调整和账面价值更新正确。
13. 重复计提/重复提交幂等或明确拒绝。
14. 批次中途失败的事务结果符合规范。
15. 财务确认成功后 Asset、Finance/Profile、IssuedCode、History、QR identity、ready_to_print 状态和审计同时存在。
16. 财务确认中任一步失败全部回滚，包括 counter。
17. QR Token 唯一、高熵，不含资产详情且不进入日志/审计。
18. 并发确认同一资产只成功一次。
19. 并发确认多个资产编号和 QR Token 均唯一。
20. 预览/试算不消耗正式编号或创建 QR identity。
21. 无编码方案或缺 source 时确认失败且资产仍 pending_finance。
22. 非 finance 构造财务 POST/Service 调用被拒绝。
23. 跨部门/角色财务字段读取按矩阵。
24. 步骤 7 只有 finance 配置的实际可用政策/默认值完整时通过，system_admin 不能代写财务值。
25. 步骤 9 重新验证九项真实条件；缺任一条件不完成，全部满足时原子完成且产生审计。
26. 完成初始化后 Sprint 3 资产入口正常；未完成时普通入口仍阻断。
27. 所有关键财务动作产生安全审计日志。
28. Sprint 0–3 回归测试通过。

全部并发、唯一约束、锁和批次测试使用 PostgreSQL。

## 本 Sprint 排除

- 通用审批工作流
- 自动写 T+、凭证或税务折旧
- 资产初始化 Excel 导入
- QR 打印、换标、现场确认贴标及转 in_use/idle
- 调拨、处置、盘点、保养、离职、综合报表

## 验收场景

1. 财务打开一项 pending_finance 设备，看到 5,000 提示但手工确认是否固定资产。
2. 试算默认次月、5%残值的完整计划后确认；系统原子生成正式编号、QR identity/Token 并进入 pending_label/ready_to_print。
3. 同时确认多项资产没有重复编号；失败资产不残留编号或半套财务记录。
4. 承接旧资产实际累计折旧，同时显示理论参考差异，实际值不被覆盖。
5. 创建月度计提、冲销错误 Entry 并做调整，原确认历史保持可追溯。
6. finance 完成步骤 7 后，system_admin 在步骤 9 看到九项真实检查全部通过并完成初始化；移除一个必备条件时完成动作被阻断。

## 完成与停止条件

- 计算规范全部样例、财务权限、发号原子性和 PostgreSQL 并发测试通过。
- 空库与 Sprint 3 数据库升级迁移通过。
- 满足 `docs/10-Definition-of-Done.md`。
- Completion Report 列出方法、精度、批次和并发证据。
- Completion Report 列出初始化步骤 7/9 的逐项校验证据及 Sprint 3 正常入口回归结果。

汇报后停止，不得开始 Sprint 5。
