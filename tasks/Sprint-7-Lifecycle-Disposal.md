# Codex Task — Sprint 7：资产生命周期与处置

## 前置

- Sprint 0–6 已验收通过，完整测试通过。
- 正式资产已完成财务确认、正式编号、QR 与贴标流程，可通过业务入口处于 in_use/idle 等允许状态。
- 权限矩阵和完整状态机已批准。

开始前完整阅读：

- `AGENTS.md`
- `docs/01-PRD.md`
- `docs/02-Business-Rules.md`
- `docs/03-Asset-Coding-Rules.md`
- `docs/04-Database-Design.md`
- `docs/05-UI-UX.md`
- `docs/07-Permissions-and-Workflows.md`
- `docs/08-Depreciation-Calculation-Spec.md`
- `docs/10-Definition-of-Done.md`

本 Sprint 实现正式资产的业务变动和报废/出售/处置闭环，不实现通用审批或完整维修工单。

## 范围

实现：

- 扩展 Sprint 6 已有 AssetMovement，启用除 `label_activation` 外的生命周期类型；不得重建第二套模型
- AssetLoan
- AssetDisposal
- AssetDisposalReversal
- 以跟踪迁移扩展 Sprint 4 的 DepreciationProfileEvent，增加处置来源/反向事件真实 FK 和 `disposal_stop/disposal_restore` 结构化事件
- 以跟踪迁移为既有 AttachmentLink 增加真实 `asset_disposal` 外键并更新恰好一个目标的 CHECK
- 领用/归还
- 部门变更
- 责任人变更/转交
- 位置变更
- 借出/归还
- 闲置/启用、送修/维修完成等批准的状态迁移
- pending_disposal、disposed、sold、other_disposed 及处置取消/冲销
- 正式编号管理员修正及 AssetCodeHistory
- 终态正式资产的 `record_status` 归档/恢复显示
- 处置附件和不可变财务快照

状态枚举、允许转换、必填字段和责任角色必须完全使用 `docs/07-Permissions-and-Workflows.md` 的状态机。

## 生命周期 Service

不得允许表单直接写 Asset 当前 department、responsible_employee、location 或 asset_status。

每项变动由唯一领域 Service 完成：

1. 校验操作者权限和对象数据范围。
2. 开启事务并锁定 Asset。
3. 重新验证当前状态及 from 值，防止页面过期覆盖。
4. 校验目标公司、部门、员工、位置均有效且范围一致。
5. 创建 append-only AssetMovement，保存 from/to、生效日期、原因、备注和操作人。
6. 更新 Asset 当前值/状态。
7. 写 AuditLog。
8. 提交后返回明确结果。

并发变动同一资产只能按锁后状态顺序成功；过期请求必须明确冲突，不能最后写入者静默覆盖。

### 归档与恢复显示

只有 `asset_status in (disposed,sold,other_disposed)` 的正式资产可由 system_admin 或 finance 通过专门 Service 归档；原因必填。归档只把 `record_status` 改为 archived，不改变终态、编号、QR、处置、财务或任何历史。恢复显示由相同角色带原因把 record_status 改回 active，资产仍保持原终态。

归档资产默认从业务列表/候选动作中排除，但有权用户可用明确筛选查看；扫码在登录和对象鉴权后只显示“资产已归档”及允许的只读摘要，不提供调拨、重打、保养、盘点或处置动作。归档/恢复均锁定 Asset、二次确认、写 before/after AuditLog；不得用物理删除代替。

## 流程要求

### 领用与归还

- 领用建立部门、责任人和位置当前归属及 assignment 历史。
- 归还不能用 NULL 静默清空；使用状态、归还位置和责任归属按状态机处理。
- 正式在用资产始终满足部门、责任人、位置要求。

### 调拨/转交/位置变更

- 支持单项资产流程。
- V1 每个资产均为 quantity=1，不实现部分数量调拨或部分数量处置。
- V1 无通用审批，但操作者、原因和生效日期必填。

### 借出/归还

- 使用 `AssetLoan.borrower_type` 精确区分 `internal_employee/external`，并以结构化字段记录内部借用员工 `borrower_employee_id` 或外部借用人/单位、借出日、预计归还日、经办人、原因、实际归还、接收人和归还目标；内部员工必须关联同公司且满足 `employment_status=active AND is_active=true` 的 Employee，服务端另存不可编辑 `borrower_name_snapshot`，外部输入名称/单位保持为空；外部借用才使用自由文本名称/单位且 snapshot 为空，二者不得混用或靠姓名猜测。
- 同一资产最多一条 active Loan；借出/归还各自与对应 AssetMovement 一对一关联，并与 Asset 状态/当前归属在同一事务提交。
- 重复归还、非借出状态归还被拒绝。
- 借出不删除原责任历史。
- 借出资产必须先归还，不能直接进入处置。

### 闲置、启用和维修占用

- `in_use ↔ idle` 记录生效日和原因。
- `in_use/idle → under_repair` 保存送修前状态；维修完成恢复原状态并保存结果。
- V1 只记录维修占用状态和历史，不创建维修工单、费用或备件模块。

### 编号修正

只有 `system_admin` 可按权限矩阵修正非终态正式资产编号，并必须输入原因；终态/已归档记录在 V1 阻止并报告，该动作不授予任何财务或资产状态修改能力。

事务内：

- 锁定 Asset 和编码记录。
- 验证新编号未出现在 Asset、IssuedCode 或 AssetCodeHistory。
- 旧编号永久保留且保持不可复用。
- 创建新 IssuedCode/History 关系并更新当前 asset_code。
- 调用 Sprint 6 换标领域能力：撤销旧 QR Token、创建新当前身份并置 `ready_to_print`，资产业务状态不变；旧纸质标签立即失效。
- 写修改前后和原因审计。

上述编号、QR、标签状态和审计在同一事务提交；不得直接覆盖 code 字符串、删除旧占号，或让旧编号标签继续有效。

## 报废、出售与处置

按照状态机严格区分 pending_disposal、disposed、sold、other_disposed。

### 发起/取消

finance、equipment 或范围内 department_manager 通过发起 Service：

1. 锁定 Asset，校验非借出且没有冲突业务。
2. 创建 AssetDisposal 草稿，保存处置类型、原因、申请日、拟处置日期和处置前状态；实际处置日期保持为空。
3. 将 Asset 迁移到 pending_disposal。
4. 写 AuditLog。

终态前，finance/equipment 可带原因取消，在事务中把 Disposal 标为 cancelled 并恢复处置前状态；不得删除处置记录、财务快照或附件。

### 实际完成信息与财务快照锁定

实物处置发生后，授权经办人在资产仍为 `pending_disposal` 时登记实际处置日期、经办人、接收方及所需证据。实际日期不得未来，并与拟处置日期分列保存；拟定日期不能触发快照、停止折旧或完成处置。

只有 finance 可随后在事务中锁定 Asset/Disposal，要求实际处置日期和实物证据已完整，并以该实际日期为唯一截止日，从 confirmed 财务权威记录生成原值、累计折旧、减值、净值和处置收入快照。若 stop_rule 要求截至该日仍应计提的期间尚未确认，必须列出缺失期间并阻断，不能拿理论折旧或旧缓存代替。所有正式资产原值都已在财务确认时保存；受控非固定资产的累计折旧和减值为 0，仍能形成完整快照。快照、实际日期锁定后不可普通编辑；填错只能在终态前带原因取消并重新发起，非财务角色只能看到“已核对/未核对”。

### 完成处置

finance 或 equipment 通过完成 Service：

1. 锁定 Asset/Disposal，要求财务快照已锁定。
2. 重新校验已锁定的实际处置日期、经办人、接收方及文档要求的照片/附件；完成动作不得改写拟定或实际日期。
3. 按类型迁移到 disposed、sold 或 other_disposed。
4. 按 stop_rule 新增带 `source_disposal/previous_profile_status` 的 `disposal_stop` DepreciationProfileEvent 并停止未来折旧，不改写以前 confirmed Entry；不得用自由文本 reason 猜来源。本 Sprint 尚无 `MaintenancePlan` 模型，不创建或更新保养数据，处置与保养计划的最终集成由 Sprint 9 扩展同一领域 Service。
5. 保留 Asset、Finance、二维码、附件和全部历史。
6. 写 AuditLog。

终态可继续在 Asset 当前字段保留处置前最后部门、责任人和位置供历史显示，但这些值不再代表活动领用关系，终态资产不得进入人员在用清单或业务候选。处置前 active Loan 仍必须先归还，不能用终态语义掩盖借用关系。

终态错误只允许 finance 在不存在后续业务记录时建立一对一 `AssetDisposalReversal`：保留原处置、快照、附件和冲销记录，把原处置标为 reversed，恢复处置前状态；对每条本处置产生的 disposal_stop 新增一对一 `disposal_restore`，effective_date 与原 stop 相同并恢复保存的 active/suspended 原状态，不删除事件或自动补建实际 Entry。若存在 stop 后的新 Profile/人工事件/冲突确认批次则阻断。若最后责任员工不满足 `employment_status=active AND is_active=true`，必须在同一冲销动作选择同公司合法替代责任人并写 Movement，否则阻断，不能恢复出非法非终态资产。本 Sprint 不引用尚未创建的保养模型；Sprint 9 再扩展该冲销 Service，仅恢复由同一处置自动终止的计划。取消与终态冲销是两个不同动作和模型状态。

处置收入使用 Decimal。锁定快照不随以后报表或财务缓存变化而改变。

## 权限与数据范围

严格执行权限文档：

- equipment/warehouse/department_manager 的领用、归还、调拨范围按矩阵；正式编号更正仅 system_admin 可执行。
- finance 独占处置财务快照、收入及折旧终止相关字段。
- 需要多角色协作的流程用明确状态和字段权限实现，不引入通用审批引擎。
- employee 只能执行允许的查看/确认动作。
- management 默认只读。

直接请求、跨部门目标 ID、所选对象和 Service 均校验权限。V1 不接受部分数量处置。

## 自动测试

至少覆盖：

1. 完整允许状态迁移表。
2. 每个禁止状态迁移。
3. in_use 必须有部门、责任人和位置。
4. 领用/归还生成正确 from/to Movement。
5. 部门、员工、位置变更更新当前值并保留历史。
6. 跨公司、停用目标、跨部门越权被拒绝。
7. AssetLoan 必填、预计日期、单一 active、借出/归还 Movement 的 OneToOne/唯一、公司/资产/type/两 FK 不同约束、幂等及重复归还；另一 Loan 复用 Movement 被数据库拒绝。
8. 闲置/启用和送修/维修完成恢复原状态，不创建维修工单。
9. 借出资产不能直接处置。
10. 并发变动同一资产的过期请求不丢失更新。
11. 中途异常同时回滚当前值、Movement 和 AuditLog。
12. 正式编号修正保留旧 IssuedCode/History、轮换 QR 并进入待重打；任一步失败全部回滚到旧编号/旧 QR。
13. 新编号已用时修正失败且原编号不变。
14. 普通用户不能直接覆盖 asset_code。
15. 发起处置进入 pending_disposal 并保留处置前状态。
16. 非 finance 不能查看/写财务快照；未锁快照不能完成。
17. 锁定快照等于处置时 confirmed 财务值，后续缓存变化不改变快照。
18. 拟处置日期不能锁财务或完成；实际日期必填、不得早于申请日或晚于当前日，并作为快照与终态唯一日期，锁定后不能普通改写；截至实际日缺少应计 confirmed 期间时明确阻断。
19. 受控非固定资产以非空原值、0 累计折旧和 0 减值生成可勾稽处置快照。
20. 只有 finance/equipment 可取消未完成处置并写 cancelled/取消人时间原因、恢复原状态且不删记录/快照；department_manager 即使曾发起也不能取消，直接请求被拒绝；锁错实际日期时只能由有权角色取消重开。
21. 完成处置按 event_date/next_month 创建唯一 disposal_stop、保存来源/前状态并从事件 effective_date 停止未来折旧但不改历史 Entry；next_month 下即使 Asset 已进入终态、Profile 已置 stopped，处置当月仍须按业务期间纳入并已确认，人工 stop/completed 不被误标。本 Sprint 不提前创建、终止或测试 Sprint 9 的保养模型。
22. 终态冲销仅 finance、无后续记录时可创建唯一 DisposalReversal 和逐 Profile 唯一 disposal_restore，精确反向本处置 stop 并保留全部证据；后续冲突被阻断，原责任人不再满足 active+is_active 时必须选择合法替代并写 Movement，否则回滚。
23. 处置附件需要对象权限。
24. 正式/处置资产不能物理删除或被级联删除。
25. 仅 system_admin/finance 可归档终态或恢复显示；非终态、越权和直接 API 请求被拒绝，操作不改变 asset_status/QR/财务/历史并产生审计。
26. 归档资产默认不进入业务候选，扫码经鉴权显示已归档且无业务动作；恢复后仍为原处置终态。
27. 每项关键动作产生审计日志。
28. Sprint 0–6 回归测试通过。

## 本 Sprint 排除

- 通用审批工作流
- 完整故障维修工单
- `MaintenancePlan/Record` 及其与处置完成/冲销的集成（统一在 Sprint 9 实现）
- 二维码生成/贴标确认
- 盘点、保养、离职清退、综合报表
- 自动会计凭证、T+ 写入

## 验收场景

1. 已在用设备从 A 部门转到 B 部门并更换责任人/位置，详情显示新当前值及完整旧值历史。
2. 借出资产产生结构化 Loan 和 Movement，归还恢复借出前状态；两人同时归还时只有一个成功且没有丢失历史。
3. system_admin 因录入错误修正编号，旧编号不能再分配、旧标签失效，新标签进入待打印且业务状态不改变。
4. equipment 发起处置，finance 锁定快照，equipment 完成；未锁快照时被阻止，模拟每步失败时事务一致。
5. 普通员工不能跨部门调拨或修改处置收入。
6. system_admin 归档一项终态资产后普通候选不再出现，扫码仅显示已归档；finance 恢复显示后资产仍保持原终态且两次动作均有原因审计。

## 完成与停止条件

- 状态机、历史、编号修正、处置快照、权限和并发测试通过。
- 满足 `docs/10-Definition-of-Done.md`。
- Completion Report 列出每类状态迁移及处置快照证据。

汇报后停止，不得开始 Sprint 8。
