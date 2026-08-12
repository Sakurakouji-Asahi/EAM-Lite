# Codex Task — Sprint 6：二维码与资产贴标

## 前置

- Sprint 0–5 已验收通过，完整测试通过。
- 正式编号、AssetQrIdentity/安全 Token、pending_label/ready_to_print 状态和状态机可用。
- 通用附件和对象权限可用。

开始前完整阅读：

- `AGENTS.md`
- `docs/01-PRD.md`
- `docs/02-Business-Rules.md`
- `docs/04-Database-Design.md`
- `docs/05-UI-UX.md`
- `docs/07-Permissions-and-Workflows.md`
- `docs/09-Security-Backup-and-Deployment.md`
- `docs/10-Definition-of-Done.md`

## 范围

实现：

- 使用并完善 Sprint 4 已创建的 AssetQrIdentity；不得为正式资产补造孤立身份
- `AssetLabelPrintBatch`/`AssetLabelPrintItem` 打印批次、明细及贴标状态记录
- 建立最小 `AssetMovement` 模型并只启用 `movement_type=label_activation`；其他生命周期类型由 Sprint 7 接入
- 单项/批量 QR 生成
- A4 标签排版与打印页
- 手机扫码页面
- 确认贴标后从 pending_label 迁移到文档允许的 in_use/idle 状态
- 重新打印、Token 轮换和审计

## QR 安全设计

- QR 只包含公司 LAN 应用 URL 和高熵不透明 Token，不包含资产名称、编号、原值、员工或位置等详情。
- Token 使用密码学安全随机源，数据库唯一，不从 asset id/code 可预测派生。
- 扫码后仍必须登录并通过后端对象权限；知道 Token 不等于获得权限。
- 运行日志、AuditLog、错误页面和分析参数不得记录完整 Token。
- Token 轮换后旧 Token 立即失效或进入文档定义的撤销状态，历史仍保留。
- 不创建公开、永久有效且绕过登录的媒体/资产页面。

## 标签内容与排版

V1 支持 A4 批量打印，不适配专用标签打印机。

默认标签：

- 公司简称
- 资产名称
- 正式资产编号
- 部门
- QR

可选：责任人、位置、型号。财务金额不得出现在标签。

要求：

- 页面/生成文件不依赖公共 CDN、远程字体或外网 QR 服务。
- QR 按 100% 实际尺寸打印时正方形边长不得小于 20 mm，保留安静区，并在批准的实际手机上可扫描。
- A4 分页、边距、重复打印和空标签位置行为明确。
- 只有正式编号资产可打印；draft/pending_finance 不得生成正式标签。

## 打印与贴标流程

### 打印

1. 授权用户选择 pending_label，或已进入正式换标流程且当前标签为 `ready_to_print`/`printed` 的资产；`attached` 资产不得直接普通重印。
2. 后端重新校验每项权限和状态。
3. 验证资产已有财务确认事务创建的有效 QR 身份；缺失时作为数据完整性错误阻止打印，不静默补造 Token。
4. 创建 `generated` 打印批次及明细，固定模板版本、资产集合、页码和位置；`printed_by/printed_at` 保持为空，二维码身份仍为 `ready_to_print`。
5. 返回 A4 打印视图；浏览器打印动作本身不得把批次或标签标为已打印。
6. 用户点击“已完成打印”后，Service 锁定批次及所含当前二维码身份，原子写入打印人/时间、把批次改为 `printed`、把明细/标签状态改为 `printed` 并写 AuditLog。
7. 用户选择失败/取消时把批次标为 `cancelled`（或写明确失败结果），标签保持 `ready_to_print`；不得伪记打印成功。

重复请求应避免意外创建多个有效 Token 或重复打印记录；贴标前用户明确“重新打印”时产生新的批次记录并复用当前 Token。已贴标资产必须先执行换标并轮换 Token。

### 确认贴标

唯一 Service 在事务中锁定 Asset：

- 校验首次贴标资产为 pending_label，存在正式编号、有效当前 QR，且标签状态已经 printed。
- 必须扫描并匹配当前 Token，不能只凭 asset id 点击确认。
- 要求部门、责任人、位置完整。
- 记录贴标人、时间和标签状态。
- 首次贴标按状态机转为 in_use 或经明确选择的 idle。
- 创建 append-only `AssetMovement(movement_type=label_activation)`，精确保存 pending_label→in_use/idle 及当时部门、责任人、位置；与 Asset、标签状态和 AuditLog 在同一事务提交。

不能仅因为打印过就自动视为已贴标。已在用/闲置资产换标时，确认新标签只把标签状态改回 attached，资产业务状态保持不变。

## 手机扫码页

首屏按权限显示：

- 图片
- 名称
- 编号
- 状态
- 责任人
- 部门
- 树形位置路径
- 最近盘点/保养摘要（模块未上线时明确为空/尚未启用）

财务字段和操作按钮按权限文档控制。页面在 360 CSS px 宽的支持手机视口下无横向溢出，主要信息和确认按钮无需缩放可读；同时在至少一台批准的实际手机上验证扫码、登录回跳和确认。

## 权限与数据范围

- 打印、重印、轮换 Token、确认贴标按权限矩阵执行。
- 扫码查看仍受公司、部门、角色和财务字段范围限制。
- 批量打印不能通过选择 ID 越权包含其他部门资产。
- management/employee 等角色仅显示批准信息和按钮。
- QR 身份管理页面不得显示完整 Token。

打印、重印、轮换、贴标确认和拒绝的关键安全事件写 AuditLog。

## 自动测试

至少覆盖：

1. Token 高熵、唯一且不可由 id/code 预测。
2. QR 内容不含敏感资产详情。
3. 未登录扫码跳转登录。
4. 登录但无对象权限仍被拒绝。
5. 财务字段不因扫码泄露。
6. draft/pending_finance 不能打印正式标签。
7. pending_label 可打印且重复普通请求复用财务确认时创建的有效 Token；缺失 QR identity 时阻止而非补造。
8. 明确重印生成重印记录但不必轮换 Token。
9. Token 轮换使旧 URL 失效，新 URL 有效。
10. 打印批次不能包含越权资产。
11. A4 标签包含默认字段且不含金额。
12. 预览只创建 generated 批次，不写打印时间或改变 label_status；确认成功才原子置 printed，失败/取消不伪记成功。
13. 按 100% 打印时 QR 边长至少 20 mm，实际纸张扫码通过；打印页面不引用公共 CDN/远程 QR 服务。
14. attached 资产直接普通重印被拒绝，必须走换标并轮换 Token。
15. 打印不自动把资产标为已贴标。
16. 只有扫描匹配当前 Token 且标签已 printed 时，确认贴标才原子迁移状态，并只创建一条 `label_activation` AssetMovement 和审计；任一步失败全部回滚。
17. 缺责任人/位置时不能转 in_use。
18. 并发重复确认贴标只产生一次有效迁移；换标确认保持原 in_use/idle 状态。
19. 360 CSS px 手机页面响应式冒烟及长名称/长位置路径，并在实际手机验证扫码回跳。
20. 日志和审计不包含完整 Token。
21. 在 UAT 规定的数据和环境口径下，浏览器 A4 打印预览完成 500 张标签的分页与渲染不超过 60 秒，且无缺页、重复或越权数据；不以未交付的服务端 PDF 生成器替代。
22. Sprint 0–5 回归测试通过。

## 本 Sprint 排除

- 公网匿名扫码
- 专用标签打印机、RFID
- 公共外网部署
- 盘点扫描业务、保养完成业务
- 除 `label_activation` 外的调拨、借还、维修、处置等 AssetMovement 业务类型
- 自动贴标确认

## 验收场景

1. 选择 10 项 pending_label 资产生成 A4 标签，断开公共互联网仍可显示并打印。
2. 扫码后未登录先登录；有权用户看到实物信息，无权用户被拒绝。
3. 打印后资产仍 pending_label；现场贴好后手工确认才转 in_use。
4. 轮换一项泄露风险 Token，旧 QR 失效且操作可审计。
5. 手机浏览器完成扫码详情查看，无财务信息泄露。

## 完成与停止条件

- QR 安全、A4 打印、贴标状态、对象权限和手机验证通过。
- 若无法验证实际生成 QR 可扫描或手机布局，本 Sprint 不得完成。
- 满足 `docs/10-Definition-of-Done.md`。

汇报后停止，不得开始 Sprint 7。
