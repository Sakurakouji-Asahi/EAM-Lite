# EAM-Lite Changelog

## Unreleased

- Sprint 13：新增 `apps.supplies` 基础骨架、低值物品分类/仓库/物品档案、
  公司隔离与后端角色权限、分页页面和物品档案 XLSX 全有或全无导入。
- 逐件低值耐用品入口继续复用现有 `Asset + controlled_non_fixed`，未修改
  `Asset.quantity=1`，未提前实现库存余额、流水、过账、领退调拨、保管、
  盘点、清退或低值物品报表。

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
