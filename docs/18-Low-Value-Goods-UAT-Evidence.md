# EAM-Lite V1.2 低值物品 UAT 证据

## 1. 执行信息

- 执行日期：2026-08-27（Asia/Shanghai）
- 执行人：Codex（隔离开发/UAT 环境）
- 数据库：PostgreSQL 18.4 隔离库；默认业务数据库未迁移
- 业务数据：`S18BROWSER`，含易耗品、数量型耐用品、逐件受控非固定资产、库存/保管流水、盘点差异、离职清退和跨期间冲销
- 浏览器：内置 Chromium；Windows Edge（扩展控制，实际执行）
- 版本收口：`VERSION` 从 `0.1.0` 更新为 `0.2.0`；未创建 tag 或 Release
- 结果值：`PASS` / `FAIL` / `NOT_APPLICABLE` / `MANUAL_PENDING`

下表中的预期结果均为 `docs/16-Low-Value-Goods-UAT.md` 对应编号的完整预期；“实际”只记录本次与预期的差异或关键数值。

## 2. 基础档案与 Excel 初始化

| UAT | 数据与角色 / 步骤 | 实际 | 自动或浏览器证据 | 状态 |
|---|---|---|---|---|
| UAT-SUP-001 | warehouse；新增父子分类并查看树 | 保存、层级、审计和公司边界正确 | `tests/test_sprint13_models_services.py::test_supply_category_normalization_uniqueness_tree_and_uuid` | PASS |
| UAT-SUP-002 | warehouse；用空格/大小写重复编码 | 中文拒绝且无重复记录 | 同上 | PASS |
| UAT-SUP-003 | warehouse；自身/后代设为上级 | 后端拒绝循环 | 同上 | PASS |
| UAT-SUP-004 | warehouse；仓库、叶位置、在职负责人 | 合法保存，跨公司/失效人员拒绝 | `tests/test_sprint13_models_services.py::test_supply_warehouse_validates_company_location_and_active_employee` | PASS |
| UAT-SUP-005 | warehouse；`S18BPAPER` consumable/箱/最低 10 | 保存并在 Edge 列表显示 | Edge 实际新增 `S18BEDGE`；`tests/test_sprint13_models_services.py::test_supply_item_rules_decimal_company_and_protected_references` | PASS |
| UAT-SUP-006 | equipment/warehouse；`S18BCHAIR` durable_quantity/把 | 保存且保管提示可见 | 同上；Chromium Dashboard | PASS |
| UAT-SUP-007 | 已过账物品修改模式 | 拒绝，历史不变 | `tests/test_sprint14_services.py::test_posted_item_code_and_mode_are_frozen` | PASS |
| UAT-SUP-010 | 下载物品模板 | 固定列、数值示例和校验正确 | `tests/test_sprint13_imports.py::test_item_master_template_has_fixed_headers_validations_and_numeric_example` | PASS |
| UAT-SUP-011 | 重复编码/无分类/非法模式/负最低库存/空单位 | 逐行错误，未建物品 | `tests/test_sprint13_imports.py::test_item_import_reports_row_errors_and_writes_no_items` | PASS |
| UAT-SUP-012 | 有效物品文件重复确认 | 首次创建、再次幂等 | `tests/test_sprint13_imports.py::test_item_import_confirmation_is_atomic_audited_and_idempotent` | PASS |
| UAT-SUP-013 | 仓库期初导入纸 10、椅 20 | 只生成草稿；过账后正确 | `tests/test_sprint14_imports.py::test_opening_import_confirmation_groups_by_warehouse_creates_only_drafts_and_is_idempotent` | PASS |
| UAT-SUP-014 | 员工期初保管椅 2、80 元 | 建保管/流水，不增库存，幂等 | `tests/test_sprint16_imports.py::test_opening_custody_template_and_confirmation_are_atomic_idempotent_and_stock_neutral` | PASS |

## 3. 入库、领退、保管、调拨与冲销

| UAT | 数据与角色 / 步骤 | 实际 | 自动证据 | 状态 |
|---|---|---|---|---|
| UAT-SUP-020 | 期初 10×100 | 数量 10、金额 1000、平均 100、单流水 | `tests/test_sprint14_services.py::test_opening_and_receipt_posting_build_immutable_ledger_and_moving_average` | PASS |
| UAT-SUP-021 | 再入 10×120 | 数量 20、金额 2200、平均 110 | 同上 | PASS |
| UAT-SUP-022 | 0 成本无/有原因 | 无原因拒绝，有原因允许 | `tests/test_sprint14_services.py::test_zero_cost_requires_reason_and_valid_zero_cost_posts` | PASS |
| UAT-SUP-023 | 修改已过账单 | UI 无入口，直接写拒绝 | `tests/test_sprint14_services.py::test_posted_documents_lines_and_ledgers_reject_ordinary_mutation_and_delete` | PASS |
| UAT-SUP-024 | 重复/并发过账 | 仅一组余额与流水 | `tests/test_sprint14_services.py::test_duplicate_post_is_idempotent_and_cancelled_draft_cannot_recover` | PASS |
| UAT-SUP-030 | 领用纸 5 | 550 元，余额 15/1650，无保管 | `tests/test_sprint15_services.py::test_issue_draft_is_neutral_partial_cost_and_consumable_has_no_custody` | PASS |
| UAT-SUP-031 | 超库存领用 | 中文拒绝，全部回滚 | `tests/test_sprint15_services.py::test_issue_shortage_rolls_back_and_employee_rules_are_enforced` | PASS |
| UAT-SUP-032 | 最后全部领用 | 数量/金额/平均成本均清零 | `tests/test_sprint15_services.py::test_full_issue_clears_quantity_amount_and_average_without_tail` | PASS |
| UAT-SUP-033 | 原领用部分退回 | 沿用原成本并增加库存 | `tests/test_sprint15_services.py::test_consumable_partial_return_uses_original_cost_and_caps_cumulative_quantity` | PASS |
| UAT-SUP-034 | 累计超量退回 | 拒绝且余额不变 | 同上 | PASS |
| UAT-SUP-035 | 无原领用退回 | 后端拒绝 | `tests/test_sprint15_services.py::test_durable_return_without_custody_source_is_rejected_by_backend` | PASS |
| UAT-SUP-040 | 耐用品领用 3×80 | 库存减 240、开放保管 240、总管理额守恒 | `tests/test_sprint15_services.py::test_durable_issue_creates_exact_custody_and_preserves_managed_amount` | PASS |
| UAT-SUP-041 | 跨部门/离职/停用员工 | 后端拒绝 | `tests/test_sprint15_services.py::test_issue_shortage_rolls_back_and_employee_rules_are_enforced` | PASS |
| UAT-SUP-042 | 部分归还 | 保管与库存同事务、原成本 | `tests/test_sprint16_services.py::test_partial_full_durable_return_conserves_amount_and_reversal_restores_custody` | PASS |
| UAT-SUP-043 | 全部归还 | 保管关闭且余额 0 | 同上 | PASS |
| UAT-SUP-044 | 部分转交 | 来源减少、目标新建、仓库不变 | `tests/test_sprint16_services.py::test_transfer_partial_full_parent_chain_idempotency_and_no_stock_change` | PASS |
| UAT-SUP-045 | 转交为部门保管 | 员工为空可保存并追踪 | 同上 | PASS |
| UAT-SUP-046 | 超量转交 | 拒绝并回滚 | 同上 | PASS |
| UAT-SUP-047 | 报损 0.5 | 保管减、无库存/凭证 | `tests/test_sprint16_services.py::test_loss_scrap_amounts_close_and_permissions_enforce_department_scope` | PASS |
| UAT-SUP-048 | 全部报废 | 保管关闭，金额可报表 | 同上 | PASS |
| UAT-SUP-049 | 报损无原因 | 后端拒绝 | 同上 | PASS |
| UAT-SUP-050 | A→B 调拨 | 两条等额流水、双余额同事务 | `tests/test_sprint15_services.py::test_transfer_writes_two_equal_ledgers_and_updates_both_balances` | PASS |
| UAT-SUP-051 | 同仓调拨 | 拒绝 | `tests/test_sprint15_services.py::test_transfer_same_warehouse_and_shortage_are_atomic` | PASS |
| UAT-SUP-052 | 调拨库存不足 | 双仓均不变化 | 同上 | PASS |
| UAT-SUP-053 | A→B/B→A 并发 | 稳定锁序，无永久死锁 | `tests/test_sprint15_concurrency.py::test_postgresql_opposite_direction_transfers_use_stable_lock_order` | PASS |
| UAT-SUP-060 | 冲销最新普通入库 | 反向单/流水，余额恢复 | `tests/test_sprint15_services.py::test_reverse_receipt_restores_snapshot_and_is_idempotent` | PASS |
| UAT-SUP-061 | 重复冲销 | 返回既有结果，不重复 | 同上 | PASS |
| UAT-SUP-062 | 有后续转交的耐用品领用冲销 | 明确拒绝 | `tests/test_sprint15_services.py::test_reverse_issue_rejects_active_return_and_reversed_return_no_longer_counts` | PASS |
| UAT-SUP-063 | 冲销历史查询 | 原/反向均保留，净额 0 | `tests/test_sprint18_reports.py::test_cross_period_reversal_is_negative_original_business_bucket` | PASS |

## 4. 逐件资产、盘点与离职清退

| UAT | 数据与角色 / 步骤 | 实际 | 证据 | 状态 |
|---|---|---|---|---|
| UAT-SUP-070 | 逐件快捷入口 | 跳现有资产草稿，数量 1 | `tests/test_sprint16_finance_assets.py::test_asset_list_four_way_accounting_filter_and_individual_durable_shortcuts` | PASS |
| UAT-SUP-071 | 认定 controlled_non_fixed、原值 2000 | 正式编号/QR，无固定类别/Profile | `tests/test_sprint16_finance_assets.py::test_controlled_non_fixed_is_rejected_by_preview_direct_services_batch_and_urls` | PASS |
| UAT-SUP-072 | 尝试所有折旧入口 | 全部拒绝；固定资产正常 | 上项及 `::test_fixed_asset_depreciation_still_generates_and_confirms_normally` | PASS |
| UAT-SUP-073 | 逐件盘点/清退 | 只走现有资产域，不进数量型重复清单 | `tests/test_sprint18_reports.py::test_controlled_non_fixed_report_uses_assets_and_asset_finance_permission` | PASS |
| UAT-SUP-080 | 发布仓库盘点 | 保存数量/金额快照 | `tests/test_sprint17_services.py::test_warehouse_count_draft_does_not_freeze_publish_freezes_and_cancel_releases` | PASS |
| UAT-SUP-081 | 无差异盘点 | 关闭且无无意义流水 | `tests/test_sprint17_services.py::test_warehouse_count_mixed_gain_loss_posts_one_document_and_no_difference_posts_none` | PASS |
| UAT-SUP-082 | 非零库存盘盈 | 快照平均成本、同事务过账 | 同上 | PASS |
| UAT-SUP-083 | 零库存盘盈 | 要求成本和原因 | `tests/test_sprint17_services.py::test_zero_stock_gain_requires_cost_and_zero_reason` | PASS |
| UAT-SUP-084 | 仓库盘亏 | 按冻结成本，不成负库存 | `tests/test_sprint17_services.py::test_warehouse_count_mixed_gain_loss_posts_one_document_and_no_difference_posts_none` | PASS |
| UAT-SUP-085 | 保管盘点差异 | 必须关联真实动作证据 | `tests/test_sprint17_services.py::test_custody_count_negative_difference_requires_and_links_real_action` | PASS |
| UAT-SUP-086 | 重复关闭 | 单一调整单/证据 | `tests/test_sprint17_concurrency.py::test_concurrent_close_creates_one_adjustment_document_and_one_ledger` | PASS |
| UAT-SUP-090 | 同时有逐件资产、耐用品、易耗品历史 | 清退只纳入前两者并分别计数 | `tests/test_sprint17_services.py::test_offboarding_supply_item_partial_action_stays_pending_and_final_action_resolves` | PASS |
| UAT-SUP-091 | 只清资产未清耐用品 | 清退完成被阻止 | 同上 | PASS |
| UAT-SUP-092 | 归还后完成 | 真流水解决后才可完成 | 同上 | PASS |
| UAT-SUP-093 | 转交耐用品 | 清退项引用转交流水 | `tests/test_sprint17_services.py::test_offboarding_all_real_custody_actions_resolve_with_final_movement` | PASS |
| UAT-SUP-094 | 已完成后发现遗漏 | 建补充清退，不改原单 | `tests/test_sprint17_services.py::test_completed_clearance_uses_existing_supplement_for_missed_custody` | PASS |

## 5. 权限、报表、一致性与容量

| UAT | 数据与角色 / 步骤 | 实际 | 证据 | 状态 |
|---|---|---|---|---|
| UAT-SUP-100 | warehouse | 档案/库存/成本/盘点权限正确 | `tests/test_sprint18_reports.py::test_supply_report_http_permissions_pagination_and_dashboard_drilldown` | PASS |
| UAT-SUP-101 | finance | 全公司数量金额、冲销、导出 | `tests/test_sprint18_reports.py::test_completed_export_is_reauthorized_against_cost_columns` | PASS |
| UAT-SUP-102 | department_manager | 仅授权部门领用/保管，无公司库存 | `tests/test_sprint18_reports.py::test_department_manager_management_and_unassigned_role_report_boundaries` | PASS |
| UAT-SUP-103 | employee | 仅本人；SQL/HTML/XLSX 无成本 | `tests/test_sprint18_reports.py::test_employee_scope_excludes_cost_projection_and_company_stock`；Chromium 员工报表 | PASS |
| UAT-SUP-104 | management | 公司只读、含获准成本、无写按钮 | `tests/test_sprint18_reports.py::test_department_manager_management_and_unassigned_role_report_boundaries` | PASS |
| UAT-SUP-105 | 跨公司 ID/直接 URL | 后端拒绝，无业务写入 | `tests/test_sprint13_http_permissions.py::test_direct_cross_company_post_is_rejected_and_lists_are_paginated` | PASS |
| UAT-SUP-110 | 当前库存余额 | 缓存一致；浏览器 3 行 | `tests/test_sprint18_reports.py::test_dashboard_source_reports_low_stock_stock_and_unit_grouping` | PASS |
| UAT-SUP-111 | 月度收发存 | 数量/金额勾稽；跨期冲销为负原业务桶 | `tests/test_sprint18_reports.py::test_stock_movement_and_issue_summaries_reconcile_exactly`、`::test_cross_period_reversal_is_negative_original_business_bucket`；截图 `var/sprint18-uat/stock-movement-cross-period-reversal.png` | PASS |
| UAT-SUP-112 | 部门领用汇总 | 领用/退回/净额正确 | 同上 | PASS |
| UAT-SUP-113 | 耐用品在管 | 与保管流水重建一致 | `tests/test_sprint18_reports.py::test_custody_reports_and_management_amount_keep_sources_separate` | PASS |
| UAT-SUP-114 | 逐件受控非固定资产 | 来自 Asset/Finance，无折旧/数量型重复 | `tests/test_sprint18_reports.py::test_controlled_non_fixed_report_uses_assets_and_asset_finance_permission` | PASS |
| UAT-SUP-115 | 12 个 XLSX | 日期 `d`、数量/成本/金额 `n`；日志行数=工作表行数；SHA 全匹配；员工成本列删除 | `tests/test_sprint18_reports.py::test_all_twelve_supply_reports_generate_real_xlsx_workbooks`；`var/sprint18-uat/export-inspection.json` | PASS |
| UAT-SUP-120 | 库存 reconcile/rebuild | dry-run 1 差异不写；confirm 修复；重复 confirm 幂等 | `tests/test_sprint18_rebuild.py::test_stock_rebuild_dry_run_confirm_ledger_immutability_and_idempotency`；S18BROWSER 命令实测 | PASS |
| UAT-SUP-121 | 保管 reconcile/rebuild | 同上且不改成本/来源/流水 | `tests/test_sprint18_rebuild.py::test_custody_rebuild_dry_run_confirm_preserves_cost_and_history` | PASS |
| UAT-SUP-122 | 并发超发 | 最多一笔成功、余额非负 | `tests/test_sprint15_concurrency.py::test_postgresql_concurrent_issues_do_not_oversell` | PASS |
| UAT-SUP-123 | 并发归还 | 不超量 | `tests/test_sprint16_concurrency.py`、Sprint 16 累积回归 | PASS |
| UAT-SUP-124 | 第 2 流水失败 | 单据/余额/流水/保管/审计全回滚 | `tests/test_sprint14_services.py::test_posting_failure_rolls_back_document_balances_ledgers_lines_and_audit` | PASS |
| UAT-SUP-125 | 现有资产回归 | 报表/财务/资产相关回归通过 | PostgreSQL 完整回归记录见第 7 节 | PASS |
| UAT-SUP-130 | 10,000 物品与大量流水分页 | 页面每页 50，查询数不随行数增长 | `tests/test_sprint18_performance.py::test_dashboard_and_report_page_query_counts_are_bounded` | PASS |
| UAT-SUP-131 | 常用筛选和计划 | 最高 520,000 流水；分页索引将首屏 5.28s 降至 0.096s | `var/sprint18-performance-500k-indexed.json` | PASS |
| UAT-SUP-132 | Chromium 与 Edge | Chromium 12 页/12 下载；Edge Dashboard、低库存和新增物品；控制台错误 0 | `var/sprint18-uat/dashboard-warehouse.png`、`edge-low-stock.png` | PASS |
| UAT-SUP-133 | 390×844 手机视口 | Dashboard 375/375、本人保管 390/390，无页面横向溢出 | `dashboard-employee-mobile.png`、`my-custodies-employee-mobile.png` | PASS |

## 6. 余额重建、迁移与浏览器附加证据

- 活动仓库盘点时 `rebuild_supply_balances --confirm` 实际返回拒绝；自动证据：
  `tests/test_sprint18_rebuild.py::test_active_stock_and_custody_counts_block_confirmed_rebuild`。
- 重建与正常过账/保管报损并发后 reconcile 一致：
  `tests/test_sprint18_concurrency.py` 两项 PostgreSQL 用例。
- Sprint 17 → 18 及当前默认迁移基线 → 18：
  `tests/test_sprint18_migrations.py`，2 项通过。
- 本次浏览器导出记录 12/12 状态 completed，行数分别为
  `3,1,3,7,4,3,3,1,1,1,1,6`；员工无成本领用导出 2 行。
- 截图：`var/sprint18-uat/dashboard-warehouse.png`、
  `stock-movement-cross-period-reversal.png`、`dashboard-employee-mobile.png`、
  `my-custodies-employee-mobile.png`、`edge-low-stock.png`。
- 截图 SHA-256：Dashboard 仓库角色
  `cb2e9a2526ec245831b767c4380c65bc014a5869f2af65bcd8972ac3940d4ac8`；跨期冲销
  `b5e134a6d3a40e3ca1a1232ff2a9c316e4092080e6f444ff52eaedaf42db72ea`；员工手机
  `03246e1dab036c3a8e71e4e15c208211a4c034f07c45ff443495b9715a9e6893`；本人保管手机
  `ac439fd5c5f8246806bc214b3a4d769b1bb33d619fda42a5c036e9c1ac371c44`；Edge 低库存
  `37170e20a267586d335bfd665054ce286b04ee1d77d906c324bddbf976ec974a`。

## 7. 自动测试批次

- Sprint 18 PostgreSQL 专项：23 passed（后续最终全套同时覆盖新增测试）。
- Sprint 13–17 累积专项：152 passed。
- 既有 Sprint 11 报表/T+ 回归：66 passed, 1 existing conditional skipped。
- 最终 PostgreSQL 18.4 全仓结果：`1262 passed, 3 skipped, 0 failed`，耗时
  `1259.37s (20:59)`。3 个 skip 均为既有 SQLite 条件分支：
  `test_correction_lifecycle_services.py:36` 两项、
  `test_sprint11_tplus_reporting.py:282` 一项；Sprint 18 新增测试无 skip。

## 8. 当前结论

截至本证据生成时，`docs/16` 全部适用编号均为 PASS，无 FAIL、NOT_APPLICABLE 或
MANUAL_PENDING。是否进入维护窗口仍以最终全仓测试、工作区状态和人工部署审批为准；默认业务数据库尚未迁移。
