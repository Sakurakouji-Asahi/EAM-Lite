# Codex Task — Sprint 9：预防性保养

## 前置

- Sprint 0–8 已验收通过，完整测试通过。
- 正式资产、责任人、附件、二维码权限和首页待办基础可用。

开始前完整阅读：

- `AGENTS.md`
- `docs/00-Requirements-Baseline.md`
- `docs/01-PRD.md`
- `docs/02-Business-Rules.md`
- `docs/04-Database-Design.md`
- `docs/05-UI-UX.md`
- `docs/07-Permissions-and-Workflows.md`
- `docs/08-Depreciation-Calculation-Spec.md`
- `docs/09-Security-Backup-and-Deployment.md`
- `docs/10-Definition-of-Done.md`

V1 只实现预防性保养。完整故障维修、维修工单、备件和运行小时周期不在范围内。

## 范围

实现：

- MaintenancePlan
- MaintenanceRecord
- MaintenanceProblem 问题跟进记录，不是维修工单
- 以跟踪迁移为既有 AttachmentLink 增加 `maintenance_record`、`maintenance_problem` 真实外键并更新恰好一个目标的 CHECK
- 扩展 Sprint 7 的处置完成/冲销 Service，接通保养计划的自动终止与受控恢复
- 日、周、月、年周期
- 保养责任人、标准内容、提前提醒天数
- 上次/下次保养日期
- 即将到期、今日到期、逾期和数据不足状态
- 保养完成、发现问题、照片/附件和备注
- 首页待办、资产详情保养标签和手机完成页面
- 可由计划任务调用的幂等管理命令/查询入口（如实现物化提醒）

## 保养计划

授权角色可为 is_maintenance_required 的适用资产建立一个或多个计划。至少保存：

- plan name/item
- cycle value
- cycle unit：day/week/month/year
- responsible employee
- advance notice days
- standard content
- first due date
- last maintenance date
- next maintenance date
- status：active/suspended/ended
- 处置终止元数据：`ended_reason`、`ended_by_disposal_id`、`status_before_disposal`、`ended_at`

要求：

- cycle value 为正整数。
- 责任人、资产同公司，责任人满足 `employment_status=active AND is_active=true`。
- V1 的“被指派保养”只以 `MaintenancePlan.responsible_employee` 表达：当前用户绑定 Employee 等于该字段时视为责任人指派，不另建未批准的指派表。
- 停用/处置资产的新计划按文档限制。
- next date 由唯一领域函数计算，不由表单任意输入覆盖。
- 尚无有效完成记录时 next date 等于 first due date；之后只按最近有效实际完成日推进。
- 日周期按实际完成日加 `cycle_value` 个日历日；周周期加 `7 × cycle_value` 个日历日；月周期采用日历安全加月（目标月无同日则取月末）；年周期保持月日，2 月 29 日遇非闰年取 2 月末。统一使用上海业务日期，禁止用固定 30 天替代“月”或固定 365 天替代“年”。
- V1 不提供 runtime_hour；发现旧枚举/迁移冲突时先按修订数据库设计解决，不擅自扩展。

## 与处置流程集成

Sprint 9 创建 `MaintenancePlan` 后，必须扩展 Sprint 7 的既有领域 Service，不复制第二套处置逻辑：

- 完成处置时在同一事务锁定资产、处置及其 `active/suspended` 计划，把计划置为 `ended`，记录 `ended_reason=asset_disposal`、同一 `ended_by_disposal_id`、原 `status_before_disposal` 和 `ended_at`；历史 Record 不修改。
- 终态处置冲销只恢复由该次处置自动终止、且没有后续冲突变更的计划，恢复其原 `active/suspended` 状态，清除本次自动终止元数据，并从未作废 confirmed Record 重算 last/next date；不得恢复此前人工终止的计划，也不得伪造完成记录。
- 终态前取消处置不触碰保养计划。任一步失败，处置状态、资产状态、计划、折旧恢复和审计必须一起回滚。
- 对已在 Sprint 7 形成的终态资产禁止新建计划；迁移不得为其制造启用计划。

## 到期和提醒

- 即将到期与逾期可由查询按上海业务日期实时计算，或由幂等管理命令刷新状态。
- 不引入 Celery、Redis、钉钉消息或外部通知。
- 如提供计划任务命令，重复运行同一天不创建重复提醒。
- 首页、列表和资产详情使用同一计算 Service，不能出现不同页面日期口径不一致。

## 完成保养

唯一完成 Service 在事务中：

1. 校验操作者、资产和计划权限。
2. 锁定 MaintenancePlan，并确认未停用/未处置冲突。
3. 校验完成日期、实际内容、结果、发现问题和必要附件。
4. 创建 append-only MaintenanceRecord。
5. 结果为“发现问题”时要求问题说明并创建 open 跟进项。
6. 更新 last maintenance date。
7. 按实际完成日及规则计算 next maintenance date。
8. 写 AuditLog。

同一计划/计划到期实例的重复提交必须幂等或明确拒绝；不得创建两个相同完成记录并把 next date 连跳两期。

`MaintenanceRecord.result` 只允许 `normal/problem_found`。problem_found 必须在同一事务创建恰好一条 open MaintenanceProblem，normal 不创建；V1 不收集未定义的 severity。

完成照片和附件使用通用 AttachmentLink 关联 MaintenanceRecord；问题后续处理证据关联 MaintenanceProblem。两类附件均继承资产、记录/问题及 A0/A1 权限，不能把问题证据错误挂到资产根对象规避权限。

发现问题只形成保养记录和 open 跟进项，不自动创建 V2 维修工单。equipment 或范围内 department_manager 可填写处理说明并关闭跟进项。

错误完成记录不得直接编辑/删除；只有 equipment 可带原因作废原记录。作废保留原证据，并从剩余有效记录重新计算 last/next date；关联 Problem/附件继续保留历史，但因源 Record 已 voided 而从当前 open 待办和可关闭集合派生失效，不伪造 closed 字段。作废后重建仍按当前计划责任人和矩阵范围重新鉴权，作废人不自动获得重建权；新记录再次 problem_found 时创建自己的新 Problem。

## UI

提供：

- 计划列表、新增、编辑、停用
- 待保养、即将到期和逾期列表
- 完成保养表单
- 保养记录详情和资产时间线
- open/closed 问题跟进及处理说明
- 错误完成记录作废和重建入口
- 手机扫码后的“完成保养”按钮及响应式表单

页面明确显示计划日期、实际完成日期、责任人和下一次日期。附件错误定位清楚。

## 权限与数据范围

按权限文档：

- 只有 equipment 管理保养计划；department_manager 没有计划建议/编辑入口，system_admin 不自动获得保养业务权限。
- 保养责任人（当前 User 绑定 Employee 等于 Plan.responsible_employee）和矩阵范围角色的完成权限，不创建第二套隐含指派。
- department_manager/management 的查看范围。
- employee 不能为无权资产伪造完成记录。
- 财务字段不在保养页面或附件接口泄露。

计划新增/修改/停用、完成记录、equipment 作废、授权执行人重建、问题附件和必要的日期调整写 AuditLog。

## 自动测试

至少覆盖：

1. day/week/month/year 各周期。
2. 月末、2 月、闰年、年末和上海时区边界。
3. cycle value、notice days 和同公司责任人校验。
4. 只为适用状态/标志资产创建计划。
5. 即将到期、今日、逾期状态边界。
6. 首页与列表使用相同口径。
7. first due date 初始化 next date；完成创建不可变 Record 并更新 last/next。
8. 同一计划/计划到期日最多一条 confirmed Record，重复提交不产生双记录/跳两期；作废后才允许重建。
9. 中途异常回滚 Record、计划日期和审计。
10. 停用计划/处置资产不能错误完成。
11. 完成附件只挂 MaintenanceRecord，跟进证据只挂 MaintenanceProblem；两者对象权限、A0/A1、类型和大小校验正确。
12. result 仅 normal/problem_found；problem_found 必须且只创建一个 open 跟进项，normal 不创建，模型/UI 不要求 severity，也不创建维修工单。
13. equipment/范围内 department_manager 可写处理说明并关闭问题，其他角色被拒绝。
14. 只有 equipment 可作废错误完成记录；重建者必须重新满足当前责任人/范围完成权限。原记录、Problem、附件保留且 last/next 从有效记录重算；源 voided 的 open Problem 不进入当前待办或可关闭集合，新 Record 的问题独立，越权直接请求被拒绝。
15. 处置终态在同一事务自动终止启用/暂停计划，保存原状态和处置关联，不删除保养历史；终态前取消不改变计划。
16. 处置冲销仅恢复由该次处置自动终止的计划并按有效 Record 重算日期；人工终止或有后续冲突的计划不被错误恢复，失败时整体回滚。
17. 若有管理命令，同日重复执行幂等。
18. 跨部门和非责任人越权被拒绝。
19. 手机完成表单响应式冒烟。
20. 计划、完成、问题关闭、作废、处置联动和冲销恢复动作产生审计日志。
21. 不存在 runtime_hour UI/API 入口。
22. Sprint 0–8 回归测试通过。

## 本 Sprint 排除

- 完整维修工单、故障派工、维修费用、备件库存
- runtime_hour 周期、自动采集设备运行数据
- Celery、Redis、钉钉/邮件通知
- 通用审批

## 验收场景

1. 为设备建立每月保养计划，跨 1 月 31 日正确计算下次日期。
2. 首页和待保养列表在提醒天数边界显示一致。
3. 责任人用手机扫码完成保养并上传照片，计划日期正确推进一次。
4. 网络重试相同请求不创建重复记录。
5. 记录“发现问题”后生成 open 跟进项，equipment 填处理说明关闭，但系统不扩展成维修工单。
6. 完成一项资产处置会终止其启用/暂停计划；冲销该处置只恢复由它自动终止的计划，人工终止计划保持终止。

## 完成与停止条件

- 日历计算、提醒、完成事务、附件、权限和手机验证全部通过。
- 无法确认月末/闰年规则或存在 runtime_hour 冲突时不得擅自完成。
- 满足 `docs/10-Definition-of-Done.md`。

汇报后停止，不得开始 Sprint 10。
