# EAM-Lite Changelog

## Unreleased

## v0.2.1 - 2026-08-31

- 生产加固审计：修复折旧处置停止事件遗漏、月中/年度处置错账风险、陈旧主数据写入、
  跨公司用户绑定、正式编号冲突、低值物品领退冲销与清退竞态、权限范围泄露、
  库存/盘点锁序死锁、备份发布/过期/下载竞态及登录审计失败后的会话残留。
- 新增当前正式资产位置必须持续为叶级节点的 Service 与 PostgreSQL 触发器保护；
  升级迁移会拒绝既存违规数据，不会静默改写资产位置。
- 生产镜像升级并固定为 PostgreSQL 18.6 与 Caddy 2.11.4 的官方多平台摘要，
  同步收紧生产代理 HTTPS、Host、CSRF 来源及导入 XLSX 容器校验。
- 强化低值物品库存核算不变量：拒绝非有限 Decimal，统一数量/金额/平均成本勾稽，
  保留全量出库及分次退回尾差、原来源成本、调拨双腿、完整冲销和严格幂等链路。
- 将库存与保管 reconcile/rebuild 提升为逐笔链路完整性核对，并增加 PostgreSQL
  延迟约束、正式历史表运行时防删权限以及并发锁序回归。
- 重构角色导航、任务中心、搜索筛选与分页，增加资产、盘点、低值物品和离职清退的
  直接办理入口、高风险确认与 390px 手机端操作优化。
- 二维码盘点的 `Origin: null` 兼容仅限带短时签名的指定桥接端点，并持续要求正确 Host、
  登录 Session、CSRF Cookie/Token 和任务权限；其他 POST 端点仍按标准 CSRF 拒绝。

## v0.2.0 - 2026-08-27

- Sprint 13：新增 `apps.supplies` 基础骨架、低值物品分类/仓库/物品档案、
  公司隔离与后端角色权限、分页页面和物品档案 XLSX 全有或全无导入。
- 逐件低值耐用品入口继续复用现有 `Asset + controlled_non_fixed`，未修改
  `Asset.quantity=1`，未提前实现库存余额、流水、过账、领退调拨、保管、
  盘点、清退或低值物品报表。
- Sprint 14：新增期初/日常入库、移动加权平均、不可变库存流水、余额缓存及期初库存导入。
- Sprint 15：新增领用、退回、调拨、完整冲销、数量型耐用品保管及 PostgreSQL 并发控制。
- Sprint 16：新增耐用品归还、转交、报损、报废、期初保管导入，并完成逐件
  `controlled_non_fixed` 防折旧集成。
- Sprint 17：新增仓库/保管盘点、差异处理和数量型耐用品离职清退闭环。
- Sprint 18：新增低值物品正式 Dashboard、12 张分页报表、按权限裁剪的流式 XLSX、
  ExportLog 审计、库存/保管余额 dry-run 与受控重建、导航首页和业务操作文档。
- Sprint 18 PostgreSQL 容量验证覆盖 10,000 个物品、20 个仓库、100 个用户和最高
  520,000 条库存流水；基于 EXPLAIN 为公司级库存流水分页增加发生时间索引，未盲目增加其他索引。
- 低值物品 UAT 使用隔离 PostgreSQL、内置 Chromium 和 Windows Edge；生成本版证据时
  默认业务数据库仍未迁移，且未创建版本标签或 Release。

## v0.1.0 - 2026-08-25

- 整合 Sprint 0–12、Requirements V1.0/V1.1 及后续纠正提交为首个预发布软件功能基线。
- 完成资产主档、财务确认、六类折旧、二维码标签、生命周期、盘点、保养、离职清退、报表和 T+ 人工对账。
- 完成中文企业界面、受控用户创建、中文审计展示、二维码查看/打印及 Web 贴标确认。
- 兼容 Edge Android 扫码产生的受限 `Origin: null` 场景，同时保留登录、CSRF Token、固定 Host、权限、当前二维码和幂等校验。
- 移除无法由浏览器验证的二次打印确认；点击打印即记录批次并打开 A4 预览，实际贴标仍须逐项确认。
- 提供 PostgreSQL 18、Gunicorn、Caddy、加密备份、30 日保留和隔离恢复工具。
- PostgreSQL 最终全量自动回归 `1085 passed, 3 skipped`。
- 本标签代表功能冻结基线；固定 DNS、受信任 HTTPS、独立备份设备和多角色人工 UAT 仍是生产上线门槛。

## Requirements Baseline V1.1

- Clarified that development is authorized one Sprint at a time.
- Added complete Sprint 3–12 execution tasks and a universal Definition of Done.
- Separated physical asset category from Finance fixed-asset classification.
- Fixed official-code issuance, permanent history, counter uniqueness, reset scope and scheme versioning requirements.
- Added role, field and department-scope permission matrices and controlled workflow state machines.
- Added durable user-department grants, structured loan/return records, inventory assignees/resolutions and distinct disposal cancellation/reversal records.
- Added normative depreciation formulas, rounding, final-period correction, opening-balance, adjustment and reversal rules.
- Resolved duplicated Finance data sources and added monthly depreciation batch control.
- Added QR/label records, secure tokens, label state and LAN/mobile security requirements.
- Reworked attachments so inventory surplus, maintenance and disposal records can own files safely.
- Added import staging/idempotency and spreadsheet-injection protection.
- Moved audit integration to Sprint 0 and every subsequent Sprint.
- Added T+ manual reconciliation workbook specification.
- Added LAN HTTPS, local static assets, backup retention, restore drill and production gates.
- Added end-to-end UAT and requirement-to-Sprint traceability.
- Closed all nine initialization steps across Sprint 1/2/4 and added numeric 5,000-asset performance thresholds.
- Required original cost for every formal asset, structured internal/external borrowers, planned versus actual disposal dates, exact import staging fields, disposal-aware maintenance restoration and reachable offboarding refresh rules.
- Finalized custom-field, Employee activation, depreciation token, Loan/Movement, multi-dimensional inventory, maintenance-problem invalidation, disposal-linked depreciation-event and typed export-total contracts so each Sprint can implement without inventing schema values.

## Requirements Baseline V1.0

- Initial requirements baseline and Sprint 0–2 task set.
