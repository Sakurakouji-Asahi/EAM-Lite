# Codex Task — Sprint 8：资产盘点

## 前置

- Sprint 0–7 已验收通过，完整测试通过。
- 正式资产、QR Token、状态机、树形位置和权限范围可用。
- 手机扫码页面可复用，不得另建绕过权限的扫码入口。

开始前完整阅读：

- `AGENTS.md`
- `docs/00-Requirements-Baseline.md`
- `docs/01-PRD.md`
- `docs/02-Business-Rules.md`
- `docs/04-Database-Design.md`
- `docs/05-UI-UX.md`
- `docs/07-Permissions-and-Workflows.md`
- `docs/09-Security-Backup-and-Deployment.md`
- `docs/10-Definition-of-Done.md`

## 范围

实现：

- InventoryTask
- InventoryTaskAssignee
- InventoryTaskAsset 不可变应盘快照
- InventoryScan
- InventoryResolution
- InventorySurplus
- 以跟踪迁移为既有 AttachmentLink 增加 inventory_surplus/inventory_scan/inventory_resolution 真实外键并更新恰好一个目标的 CHECK
- 部门盘点、财务全盘和专项盘点
- 公司、部门、类别、位置和选定资产范围
- 手机扫码盘点、进度和差异
- 盘亏及盘盈待确认
- 差异处理、任务关闭和盘点报表
- 盘点照片/附件的通用业务关联

## 盘点任务与快照

创建任务先保存 draft，只记录任务名称、类型、范围、计划日期和 `InventoryTaskAssignee`，不生成快照、不接受扫描。指派按任务授权非财务扫描能力，不扩大资产总账权限。

发布 Service 必须：

1. 校验发布角色、盘点类型、公司、scope，以及每名执行人的启用状态和角色权限。
2. 在事务中锁定 draft task，固定 scope 参数和基准时间。
3. 按基准时点查询应盘正式资产。
4. 为每项创建 InventoryTaskAsset，保存编号/名称显示值及 expected department、employee、location、status 等快照。
5. 保存应盘数量并进入 in_progress。
6. 写 AuditLog。

快照创建后不可因资产调拨、责任人变化或状态变化而反向更新。任务页面同时可显示“当前值”，但差异必须相对快照判断并明确标识。

同一任务/资产只能有一个快照行。selected assets 范围必须逐项做对象权限校验。

## 扫码与结果

手机扫描 QR 后：

- 先验证登录、Token、任务状态、扫码人和资产对象权限。
- 只允许扫描属于该任务快照的资产；非范围资产给出明确提示，不静默加入。
- 记录实际位置、实际责任人、实际状态及时间/人员。
- 位置、责任人、状态分别与快照比较：全部相同为 `normal`，仅一维不同时分别为 `location_mismatch/responsible_mismatch/status_mismatch`，两维或三维同时不同时为 `multiple_mismatch` 并逐维保留，非这三维的异常才用 `other_mismatch` 且 note 必填。result 由后端派生，用户不能选择一个值覆盖其他差异。
- 网络重试或双击的相同幂等键不得重复累计；再次盘点需保留版本/历史并明确当前有效结果。
- 扫描结果本身不自动修改 Asset 当前主档。

“未盘”从快照且无有效扫描推导，不伪造 Scan 记录。

## 盘亏与盘盈

### 盘亏

- 只有任务关闭/差异确认阶段按权限标记盘亏。
- 盘亏不等于直接删除或自动报废资产。
- 后续资产状态/处置必须复用生命周期 Service 并按权限执行。

### 盘盈

- 系统中不存在的实物进入 InventorySurplus，不得伪造 asset_id 或占用正式编号。
- 保存临时名称、类别描述、位置、发现人、时间、照片/附件和处理状态。
- 附件使用通用 AttachmentLink 直接挂盘盈对象，不先创建假资产。
- finance/授权角色确认后可通过明确动作转换为 Asset draft，并保存 linked_asset；仍需财务确认和正式发号。
- 重复转换同一盘盈记录必须幂等。

## 差异处理和关闭

- 使用 append-only `InventoryResolution` 提供差异确认、备注、必要附件及处理动作；不得只改 TaskAsset 状态而不留结论。
- 位置/责任人异常不能由 InventoryScan 直接覆盖主档；需要显式调用生命周期变更 Service，并产生 Movement/Audit。
- 有权角色先执行“停止扫码”，任务进入 reconciliation；此后拒绝普通扫描，并把无有效扫描快照行显示为未盘。差异处理角色可对单一未盘行执行受控补盘：重新扫当前 QR、填写原因、创建 `scan_mode=supplemental` 的新 Scan，任务保持 reconciliation；Assignee 不能调用。
- 关闭前显示应盘、已盘、正常、异常、未盘、盘盈和未解决数。
- 所有异常/未盘快照行及所有盘盈必须有处理结论后才允许关闭；`normal` 行的有效扫描结果就是完成证据，不得伪造 `InventoryResolution`。不允许带未解决项关闭。
- 关闭后任务、原始快照和结论不可编辑；批准纠错新增指向原 `InventoryResolution` 的更正记录，不覆盖原结论。
- 已发布任务如取消，必须保存取消人、时间和原因，以及执行人、快照、已有扫描/结论/盘盈；不得物理删除证据。

## 权限与数据范围

按权限文档：

- finance 可创建/关闭财务全盘。
- department_manager/授权部门用户只处理本部门盘点范围。
- equipment 可按矩阵创建专项任务；warehouse 只能执行被指派任务，不能创建/发布。
- employee/warehouse 等执行人只有存在当前任务的 `InventoryTaskAssignee` 且角色仍有效时才能扫描；指派不授予其他任务或资产总账权限。
- management 默认只读统计。
- 财务字段不出现在普通盘点页、QR 返回或导出中。
- 已发布任务取消及关闭后更正只授予权限矩阵中“可关闭该类型任务”的角色：财务全盘仅 finance，专项为 finance/equipment，部门任务为 finance/equipment/范围内 department_manager；普通执行人和单纯 Assignee 不得操作。

创建、扫描、重扫、差异处理、盘盈转换和关闭均实施后端权限及审计。

## 事务与并发

- 发布快照为原子事务；失败时保留 draft 或完整回滚发布，不留下半个快照。
- 扫描使用任务/资产唯一和幂等约束，防止重复提交。
- 受控补盘锁定 task/task_asset，替换有效 Scan 并重算正常/异常；补盘正常无需虚构 Resolution，补盘异常仍需处理结论，失败整体回滚。
- 关闭任务时锁定任务并重新计算未解决数；关闭与新扫描竞争时只能有一个一致结果。
- 盘盈转草稿锁定 surplus，并在同一事务创建 draft、建立链接和审计。
- 生命周期纠正使用 Sprint 7 Service，不复制更新逻辑。

## 自动测试

至少覆盖：

1. 部门、财务全盘、专项及各 scope 草稿创建，draft 不生成快照/不接受扫描；全盘只有 finance 可建/发，执行人指派唯一且发布后保留。
2. 发布后快照只包含基准时点和授权范围内资产。
3. 同任务/资产快照唯一。
4. 创建后调拨不改变 expected 值。
5. 扫描与快照逐维比较得到正常、三个单项异常、multiple_mismatch 和 other_mismatch；多维差异不丢失任一 before/actual 值，result 不能由请求篡改。
6. 非执行人、角色已撤销执行人、非任务资产、无效 Token、关闭任务扫描被拒绝；任务指派不扩大总账权限。
7. 重复网络请求不重复计数。
8. 重扫保留历史并明确当前有效结果。
9. 未盘由快照推导。
10. 扫描不直接更新 Asset 主档。
11. 部门用户不能查看/扫描其他部门任务资产。
12. 财务字段不泄露。
13. 盘盈不要求/伪造 asset_id，附件可关联 surplus。
14. 盘盈转 draft 不生成正式编号且重复转换幂等。
15. 盘亏不物理删除资产。
16. 每个异常/未盘行使用唯一 active InventoryResolution；主档纠正复用 Movement Service，结论附件挂 Resolution。
17. 停止扫码进入 reconciliation 后拒绝普通扫描，未扫描行正确显示未盘；finance/equipment/范围内 department_manager 可带原因受控补盘，Assignee/越权角色被拒绝，任务不退回 in_progress。
18. supplemental Scan 保留旧证据并成为唯一有效结果；补盘正常可关闭，补盘异常仍要求 Resolution，并发补盘/关闭保持一致。
19. 未解决差异/盘盈不能关闭；normal 行无需伪造结论，全部异常/未盘和盘盈有结论后关闭。
20. 关闭与并发扫描保持一致，关闭后不可修改快照；已发布取消按任务类型/范围权限执行并保留取消人、时间、原因和全部证据，Assignee 直接请求被拒绝。
21. 关闭后仅原任务类型的关闭角色可新增关联原结论的更正记录，部门经理受范围限制；原结论不覆盖/删除，任务不重开扫描。
22. 创建/发布/扫描/补盘/差异/关闭产生审计日志。
23. 手机宽度扫码和进度页面冒烟。
24. Sprint 0–7 回归测试通过。

## 本 Sprint 排除

- RFID、离线原生 App、GPS 强制定位
- 扫描后自动调拨/自动处置
- 公网匿名盘点
- T+ 写入
- 通用审批

## 验收场景

1. 部门经理创建本部门任务；任务创建后资产调拨，原快照保持不变并产生预期差异。
2. 手机连续扫描正常、位置错误和责任人错误资产，进度统计正确。
3. 重复提交同一扫描请求不重复增加已盘数。
4. 发现系统外实物，建立带照片的盘盈记录，财务确认后转为 draft 而非正式资产。
5. 财务停止扫码、处理全部差异后关闭全盘任务，快照、扫描和差异报告保持只读可追溯。

## 完成与停止条件

- 快照不可变、扫码幂等、差异、盘盈、关闭、权限和手机验证全部通过。
- 满足 `docs/10-Definition-of-Done.md`。
- Completion Report 单列快照并发和盘盈无假 asset_id 证据。

汇报后停止，不得开始 Sprint 9。
