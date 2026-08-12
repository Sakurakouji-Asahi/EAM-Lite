# Codex Task — Sprint 10：员工离职资产清退

## 前置

- Sprint 0–9 已验收通过，完整测试通过。
- Employee 状态、资产当前责任人、生命周期/处置 Service 和权限矩阵可用。

开始前完整阅读：

- `AGENTS.md`
- `docs/00-Requirements-Baseline.md`
- `docs/01-PRD.md`
- `docs/02-Business-Rules.md`
- `docs/04-Database-Design.md`
- `docs/05-UI-UX.md`
- `docs/07-Permissions-and-Workflows.md`
- `docs/10-Definition-of-Done.md`

## 范围

实现：

- EmployeeAssetClearance
- EmployeeAssetClearanceItem
- 以跟踪迁移为既有 AttachmentLink 增加 clearance/clearance_item 真实外键并更新恰好一个目标的 CHECK
- Employee 进入 leaving 时的清退任务创建/同步
- 已完成清退后由 HR 建立独立补充清退的可追溯流程
- 扩展既有处置冲销 Service：任何被 ClearanceItem 作为 disposed 解决证据引用的处置在 V1 均视为后续冲突并阻断冲销
- 名下正式资产快照和未解决数量
- 归还、转交、处置三类解决方式
- 完成/阻断规则、明显警告、页面和审计

不连接外部 HR 系统，不实现通用审批。

## 发起与快照

员工从 active 进入 leaving 必须通过领域 Service：

1. 校验 HR/授权角色及员工数据范围。
2. 开启事务并锁定 Employee。
3. 更新 employment_status 为 leaving，并明确把 Employee.is_active 置 false；User 账号启停按安全流程独立处理，不在此静默联动。
4. 查询该员工当前负责，或通过 active `AssetLoan.borrower_employee_id` 明确关联为内部借用人的正式资产；不得用自由文本姓名猜测员工身份。
5. 创建或幂等获取 EmployeeAssetClearance。
6. 为每项创建 ClearanceItem，保存 `source_type`、结构化来源 Loan（适用时）、所有来源中最晚的关联生效时间、发现时间，以及编号、名称、原状态、原部门、原责任人和原位置路径等不可变快照；同一资产同时属于责任和内部借用来源时使用 `both`，不拆成重复 Item。
7. 计算 total_assets、unresolved_assets。
8. 写 AuditLog。

同一员工同一次离职流程只能有一个有效 clearance。重复请求不得重复生成 Items。

员工进入 `leaving` 后，生命周期和借出 Service 必须拒绝把任何新资产领用、转交或内部借用给该员工，不能用“自动补清退项”绕过该禁令。另提供 HR 可手工触发、系统也可在既有关联状态变化后调用的 refresh/核对 Service：只把关联生效时间不晚于清退发起时间、但因历史数据修复、并发边界、遗漏或既有关联资产状态变化而后补发现的当前负责/内部借用资产追加为 `added_during_clearance=true` 的新 Item，保存来源、原关联生效时间、发现时间、补入原因和审计，不静默重写原快照。外部借用自由文本不参与员工清退匹配。

## 解决方式

### 归还

调用 Sprint 7 的 return Service，成功后在同一业务事务/协调流程更新 ClearanceItem resolution 和未解决数。

### 转交

调用责任人转交 Service，校验新责任人、部门和位置，保留 AssetMovement。

### 处置

调用处置流程。需要 finance 的财务步骤仍由 finance 权限完成，HR 不能直接填写处置收入或财务快照。

发起后 Item 进入 `disposal_in_progress` 并关联 `AssetDisposal`，仍计入未解决；只有处置进入 confirmed 终态才把 Item 改为 `disposed` 并减少未解决数。处置取消必须把 Item 恢复为 `pending`。不得在“已发起处置”时提前清零。终态 Asset 上保留的最后责任人/部门/位置只作历史显示，不再代表活动领用，因此 disposed 是下面活动关系清除条件的明确例外；借出资产必须先归还，不能靠处置跳过 active Loan。

禁止直接改 Asset.responsible_employee 来“清零”；所有解决方式必须产生既有生命周期历史和审计。

如果资产已由其他合法流程解决，清退同步 Service 应识别真实当前状态并以可追溯方式关闭对应 Item，不伪造 Movement。

returned/transferred Item 只有在该员工已不再是当前责任人、且不再存在指向该员工的 active 内部 Loan 时才算解决；`source_type=both` 时归还借用或转交责任关系中的任意一项都不能提前清零。disposed Item 以关联处置 confirmed 和资产终态为权威例外。

## 完成与阻断

- unresolved_assets 必须从 Items 权威状态计算或由 Service 严格同步，不能由表单编辑。
- 存在未解决 Item 时，页面显示明显阻断警告。
- 能否将 Employee 改为 resigned、完成 Clearance 的规则完全按权限工作流文档；不得仅隐藏按钮。
- 完成时事务锁定 clearance，重新查询全部 Items 和相关 Asset 当前状态。首次清退的 HR 完成表单必须填写实际离职日期 `termination_date`，不早于 hire_date、不晚于当前上海业务日；不得默认服务器当天。
- 首次完成在同一事务保存 termination_date、把 Employee leaving→resigned、记录 completed_at/operator；Clearance/Items 不可物理删除。
- Employee 保持 is_active=false；V1 不提供 resigned→active 或 completed Clearance 重开。
- 已完成后发现遗漏资产时不修改或重开原单：HR 必须填写补充原因，新建 `supplements_clearance` 指向原 completed 单。员工保持 resigned 及原 termination_date；新单只快照异常资产，解决后完成补充单但不重复改写人员状态。已有 active 补充单时复用，不能并发创建第二张。

## UI

提供：

- 离职资产清退列表和未解决计数
- 员工清退详情及每项资产原快照/当前状态
- 归还、转交、处置入口（按角色显示）
- 刷新/重新核对
- 新建补充清退（仅已完成清退后、HR、原因必填）
- 完成清退和阻断原因

页面须清楚区分 HR 操作、资产管理操作和财务处置操作。

## 权限与数据范围

按 `docs/07-Permissions-and-Workflows.md`：

- hr 发起离职处理、查看清退进度和执行批准的人事状态动作。
- 只有 hr 可手工刷新/核对和为已完成清退建立补充清退；系统自动 refresh 仍走同一 Service 与审计，不成为匿名权限绕过。
- equipment/warehouse/department_manager 只处理授权范围内的归还/转交。
- finance 独占处置财务字段和确认。
- employee 可查看/配合的范围按矩阵。
- management 默认只读汇总。

跨部门资产仍按对象权限安全展示；需要协作时显示最小必要信息，不因 HR 页面泄露财务数据。

## 事务与并发

- leaving 状态、Clearance、Items 和审计创建为原子事务。
- 补充清退创建锁定 Employee、原 completed Clearance 和 active 唯一范围；失败不留下空补充单。
- 解决 Item 时锁定 ClearanceItem 和 Asset，并复用生命周期事务 Service。
- 两人同时处理同一 Item 只能有一个解决结果；第二个请求返回已处理/冲突。
- 完成与最后一个解决请求竞争时重新锁定/计算，不产生 unresolved=0 但资产仍归属离职员工的状态。
- 计数字段如缓存，只能由 Service 更新并有一致性测试。

## 自动测试

至少覆盖：

1. active→leaving 创建唯一有效 Clearance。
2. 当前责任和 active 内部借用两种结构化来源全部进入 Items；source_type/source_loan/关联生效时间正确，同一资产双重来源不重复建 Item。
3. 重复及并发发起只返回同一个 `open/blocked` 有效清退单，不重复创建。
4. 编号、名称、原部门/责任人/位置路径和状态快照与 Asset 后续当前值区分，后补发现项有明确标记和原因。
5. 新领用、转交或内部借用给 leaving 员工被拒绝；refresh 只补充发起时已存在但后补发现的结构化关联资产并保留留痕。
6. 归还复用 Movement Service；只有员工不再承担任何该 Item 来源关系时才解决，both 来源不得提前清零。
7. 转交校验目标并保留 Movement。
8. 处置需要 finance，HR 不能写财务字段；发起处置仍 unresolved，confirmed 终态才以 disposed 例外解决，保留的最后责任字段不被误判为活动领用，取消恢复 pending。
9. 直接清空 responsible employee 被拒绝。
10. unresolved_assets 与 Items 始终一致。
11. 未解决时完成/转 resigned 按状态机被阻断。
12. 全部解决后 HR 必须填写合法 termination_date；完成原子写 resigned/日期/记录，空值、未来或早于 hire_date 被拒绝。
13. 并发解决同一 Item 只有一个成功。
14. 完成与解决并发不产生错误完成。
15. 跨部门、角色和财务字段权限正确。
16. Clearance/Items 不能物理删除。
17. refresh 仅 HR 可手工触发且不能纳入发起后的新关系；系统触发使用同一受控 Service。
18. completed 原单不可重开/改写；HR 可带原因建立一张 active 补充清退并保留原 termination_date，重复/并发请求不建第二张，其他角色被拒绝。
19. 任何 disposed Item 引用的 Disposal 均使终态处置冲销按“后续冲突”被拒绝，不能静默改写 completed/active ClearanceItem；无清退引用的处置仍按 Sprint 7 规则冲销。
20. 发起、刷新、补充、解决、完成产生审计日志。
21. Sprint 0–9 回归测试通过。

## 本 Sprint 排除

- 外部 HR/钉钉集成
- 通用审批、消息通知
- 工资结算或人事档案管理
- 绕过生命周期 Service 的快捷清空

## 验收场景

1. HR 将员工设为 leaving，系统列出其名下设备、电脑和工具并显示未解决数。
2. 一项归还、一项转交、一项进入财务处置，各自产生正确历史且职责隔离。
3. 未解决前不能错误完成清退；全部解决后完成并永久保留证据。
4. 两名处理人同时操作同一资产不会重复归还/转交。
5. HR 页面不显示无权财务金额。

## 完成与停止条件

- 发起、快照、三种解决方式、阻断、并发、权限和审计测试通过。
- 满足 `docs/10-Definition-of-Done.md`。
- Completion Report 列出端到端清退场景证据。

汇报后停止，不得开始 Sprint 11。
