# Sprint 1 验收证据映射

本表把 `tasks/Sprint-1-Master-Data.md` 的自动测试 1–22 和验收场景 1–6
映射到可重复执行的 pytest node ID。数据库约束、并发和本表的
`test_sprint1_acceptance_evidence.py` 节点必须在 PostgreSQL 18.4 执行；SQLite
跳过结果不能作为验收证据。

## 自动测试 1–22

| # | 主要 pytest node ID | 证明内容 |
|---|---|---|
| 1 | `tests/test_sprint1_models.py::test_company_code_nfkc_casefold_unique_and_single_active` | Company code 标准化、唯一和单活动公司。 |
| 2 | `tests/test_sprint1_acceptance_evidence.py::test_company_scoped_identifiers_and_department_tree_guards_are_database_backed` | Department code 同公司规范化唯一、跨公司可复用。 |
| 3 | `tests/test_sprint1_acceptance_evidence.py::test_company_scoped_identifiers_and_department_tree_guards_are_database_backed`；`tests/test_sprint1_models.py::test_tree_validation_rejects_cross_company_and_deep_cycles_in_postgresql`；`tests/test_sprint1_masterdata_concurrency.py::test_concurrent_opposite_tree_reparents_cannot_create_cycle` | 自环、深层循环、跨公司父级和并发循环均拒绝。 |
| 4 | `tests/test_sprint1_models.py::test_manager_rule_and_employee_status_constraints`；`tests/test_sprint1_services.py::test_employee_status_service_clears_manager_and_audits_with_company`；`tests/test_sprint1_masterdata_concurrency.py::test_concurrent_manager_bind_and_employee_disable_preserve_validity` | 经理同公司/在职启用/所属部门启用，失效清空、审计和并发不变量。 |
| 5 | `tests/test_sprint1_acceptance_evidence.py::test_employee_status_candidate_and_user_activation_are_independent`；`tests/test_sprint1_models.py::test_employee_user_unique_and_scope_company_database_guard` | 无 User 可建、User 唯一、任职组合、termination date、候选谓词及 User/Employee 启停独立。 |
| 6 | `tests/test_sprint1_acceptance_evidence.py::test_company_scoped_identifiers_and_department_tree_guards_are_database_backed` | employee_no 在公司范围规范化唯一。 |
| 7 | `tests/test_sprint1_models.py::test_location_and_category_tree_level_and_database_cycle[Location-kwargs0-level]`；`tests/test_sprint1_acceptance.py::test_tree_reparent_recalculates_all_descendant_levels_and_audits`；`tests/test_sprint1_database_evidence.py::test_reverse_company_reference_guards_reject_existing_links` | Location 层级、循环、跨公司与后代 level 重算。 |
| 8 | `tests/test_sprint1_models.py::test_location_and_category_tree_level_and_database_cycle[AssetCategory-kwargs1-category_level]`；`tests/test_sprint1_acceptance.py::test_asset_category_is_physical_only_and_has_no_finance_fields` | 实物分类树/编码约束及实物与会计分类分离。 |
| 9 | `tests/test_sprint1_acceptance_evidence.py::test_masterdata_update_deactivation_and_protect_are_audited_without_deletion` | 停用保留记录，受引用对象 PROTECT。 |
| 10 | `tests/test_sprint1_acceptance_evidence.py::test_user_department_scope_active_uniqueness_and_revoked_history`；`tests/test_sprint1_services.py::test_scope_union_descendants_revoke_and_role_does_not_come_from_scope` | 同公司、活动唯一、后代、多个根、撤销历史。 |
| 11 | `tests/test_sprint1_acceptance_evidence.py::test_scope_without_role_is_denied_and_manager_http_revocation_is_immediate`；`tests/test_sprint1_acceptance_evidence.py::test_last_login_capable_system_admin_and_finance_are_protected`；`tests/test_sprint1_masterdata_concurrency.py::test_concurrent_opposite_tree_reparents_cannot_create_cycle`；`tests/test_sprint1_masterdata_concurrency.py::test_concurrent_manager_bind_and_employee_disable_preserve_validity`；`tests/test_sprint1_masterdata_concurrency.py::test_concurrent_admin_removals_leave_one_login_capable_admin` | 仅范围无角色仍拒绝、经理越界拒绝/撤权即时、末位高风险角色与并发保护。 |
| 12 | `tests/test_sprint1_acceptance_evidence.py::test_system_admin_cannot_write_hr_data_but_hr_http_and_service_can`；`tests/test_sprint1_acceptance.py::test_setup_and_mutating_urls_reject_ordinary_user_and_get_status_change` | system_admin/HR 职责分离，无权角色后端拒绝。 |
| 13 | `tests/test_sprint1_acceptance.py::test_setup_progress_steps_1_to_5_and_8_persist_but_never_complete`；`tests/test_sprint1_acceptance_evidence.py::test_setup_progress_recomputes_real_data_persists_and_blocks_unscoped_manager`；`tests/test_sprint1_acceptance_evidence.py::test_setup_counts_application_users_not_recovery_superuser` | 步骤 1–5/8 来自真实数据并持久化，users/permissions 精确，未授权经理阻断，recovery superuser 不计入，整体仍 false。 |
| 14 | `tests/test_sprint1_imports.py::test_department_import_preview_errors_do_not_write_business_rows`；`tests/test_sprint1_imports.py::test_department_import_confirm_is_atomic_and_audited`；`tests/test_sprint1_imports.py::test_confirm_failure_rolls_back_objects_rows_batch_and_audit` | 部门导入解析、预览、原子确认、回滚和审计。 |
| 15 | `tests/test_sprint1_imports.py::test_employee_import_unknown_department_invalid_and_unauthorized_rejected`；`tests/test_sprint1_imports.py::test_employee_import_confirm_success_is_audited_and_idempotent`；`tests/test_sprint1_imports.py::test_employee_confirm_failure_rolls_back_all_objects_and_audit` | 人员导入解析、预览、确认、幂等和回滚。 |
| 16 | `tests/test_sprint1_imports.py::test_department_import_preview_errors_do_not_write_business_rows`；`tests/test_sprint1_imports.py::test_import_rejects_formula_and_unknown_columns_as_structured_errors[options0]` | 错误具有行号、字段、值、原因。 |
| 17 | `tests/test_sprint1_imports.py::test_import_rejects_invalid_date_and_database_duplicate`；`tests/test_sprint1_imports.py::test_employee_import_unknown_department_invalid_and_unauthorized_rejected` | 文件/数据库重复、日期和未知部门拒绝。 |
| 18 | `tests/test_sprint1_imports.py::test_employee_import_unknown_department_invalid_and_unauthorized_rejected`；`tests/test_sprint1_acceptance.py::test_cross_company_post_ids_and_invalid_manager_are_rejected`；`tests/test_sprint1_acceptance.py::test_setup_and_mutating_urls_reject_ordinary_user_and_get_status_change` | 导入越权及跨公司/跨部门 ID 篡改拒绝。 |
| 19 | `tests/test_sprint1_acceptance_evidence.py::test_system_setting_registry_types_sprint_boundary_and_audits_are_exact`；`tests/test_sprint1_acceptance_evidence.py::test_system_setting_http_exposes_only_two_sprint1_attachment_fields`；`tests/test_sprint1_database_evidence.py::test_database_rejects_values_outside_fixed_enums`；`tests/test_sprint1_database_evidence.py::test_database_rejects_non_finite_and_negative_decimal_setting` | 三个固定 key/type、页面仅两项附件设置、边界/Secret/未知/错类型/财务阈值拒绝和数据库兜底。 |
| 20 | `tests/test_sprint1_acceptance_evidence.py::test_masterdata_update_deactivation_and_protect_are_audited_without_deletion`；`tests/test_sprint1_acceptance_evidence.py::test_high_risk_role_changes_require_reason_password_and_safe_audit`；`tests/test_sprint1_acceptance_evidence.py::test_system_setting_registry_types_sprint_boundary_and_audits_are_exact`；`tests/test_sprint1_imports.py::test_department_import_confirm_is_atomic_and_audited`；`tests/test_sprint1_imports.py::test_employee_import_confirm_success_is_audited_and_idempotent` | CRUD、角色/范围、设置、停用、导入确认审计。 |
| 21 | `tests/test_sprint1_audit.py::test_pre_initialization_audit_may_remain_without_company`；`tests/test_sprint1_audit.py::test_business_audit_requires_real_company`；`tests/test_sprint1_audit.py::test_inactive_existing_company_is_used_for_security_and_bootstrap_audit`；`tests/test_sprint1_database_evidence.py::test_audit_log_rejects_raw_sql_update_and_delete` | 业务审计 company 必填，合法预初始化事件可 NULL，审计行在 ORM 和 PostgreSQL 层均只追加。 |
| 22 | `tests/test_accounts_and_roles.py::test_custom_user_model_is_active_from_initial_migration`；`tests/test_accounts_and_roles.py::test_eight_fixed_roles_exist_and_seed_migration_is_idempotent`；`tests/test_audit.py::test_audit_rows_are_append_only`；`tests/test_auth.py::test_user_can_log_in_with_correct_password`；`tests/test_auth.py::test_logout_post_requires_csrf_token`；`tests/test_settings.py::test_business_locale_timezone_currency_and_selected_database`；`tests/test_static_assets.py::test_templates_do_not_reference_public_cdn` | Sprint 0 关键身份、角色、审计、登录/退出、配置和本地静态资源回归；完整 `python -m pytest -q` 证明全部 Sprint 0 节点未回归。 |

## 验收场景 1–6

| # | 自动证据 | 浏览器证据 |
|---|---|---|
| 1 | `tests/test_sprint1_acceptance.py::test_setup_progress_steps_1_to_5_and_8_persist_but_never_complete` | Chrome 建公司、三级部门、人员、三级分类、三级位置并检查 setup。 |
| 2 | `tests/test_sprint1_acceptance.py::test_setup_progress_steps_1_to_5_and_8_persist_but_never_complete`；`tests/test_sprint1_acceptance_evidence.py::test_setup_progress_recomputes_real_data_persists_and_blocks_unscoped_manager` | Chrome 退出、重登、再次进入 `/setup/`。 |
| 3 | `tests/test_sprint1_imports.py::test_mixed_valid_and_invalid_preview_writes_no_masterdata` | 未完成：Chrome 扩展拒绝本机文件注入；PostgreSQL 自动节点证明混合预览不写正式表。 |
| 4 | `tests/test_sprint1_imports.py::test_department_import_confirm_is_atomic_and_audited`；`tests/test_sprint1_imports.py::test_employee_import_confirm_success_is_audited_and_idempotent` | 未完成：Chrome 扩展拒绝本机文件注入；PostgreSQL 自动节点证明修正文件原子确认、幂等和审计。 |
| 5 | `tests/test_sprint1_acceptance.py::test_department_manager_department_pages_are_read_only_and_scope_limited`；`tests/test_sprint1_acceptance.py::test_department_reparent_requires_impact_preview_and_explicit_confirmation`；`tests/test_sprint1_acceptance_evidence.py::test_scope_without_role_is_denied_and_manager_http_revocation_is_immediate` | Chrome/Edge 检查授权树、范围外拒绝、改挂预览和撤销后立即失权。 |
| 6 | `tests/test_sprint1_acceptance.py::test_setup_and_mutating_urls_reject_ordinary_user_and_get_status_change` | 普通用户直接构造 setup、编辑、导入确认 URL 均拒绝。 |

## 本轮附件和导入清理附加证据

- `tests/test_sprint1_import_cleanup.py::test_upload_is_unavailable_until_atomic_publication_and_failure_is_private`
- `tests/test_sprint1_import_cleanup.py::test_automatic_cleanup_retention_status_mapping_and_repeat_are_safe`
- `tests/test_sprint1_import_cleanup.py::test_confirmed_created_and_referenced_evidence_are_protected`
- `tests/test_sprint1_import_cleanup.py::test_validated_requires_explicit_admin_abandon_reason_and_is_audited`
- `tests/test_sprint1_import_cleanup.py::test_orphan_cleanup_waits_rechecks_references_and_is_idempotent`
- `tests/test_sprint1_import_cleanup.py::test_legacy_temp_and_unreferenced_private_cleanup_are_bounded_and_repeatable`
- `tests/test_sprint1_import_cleanup.py::test_source_download_has_no_public_route_and_rejects_unauthorized_users`
- `tests/test_sprint1_import_cleanup.py::test_cleanup_management_command_is_dry_run_by_default_and_executes_explicitly`

上述八个清理节点分别证明上传不可用到事务发布、30 天批次保留、confirmed/validated 保护、
显式放弃、孤儿附件、遗留临时/无元数据私有文件、重复清理、活动上传锁保护、management
command dry-run/execute、鉴权下载和公开 URL 拒绝。
- `tests/test_sprint1_imports.py::test_import_rejects_formula_hidden_beyond_forged_dimension`
  等 XLSX 安全节点：真实单元格边界、公式、外部关系、单成员/总量/压缩比限制。
- `tests/test_sprint1_database_evidence.py::test_confirmed_import_evidence_rejects_queryset_and_raw_sql_mutation`：
  confirmed 批次、created Row 与原附件证据不可篡改/删除。

浏览器冒烟的准确页面、状态码和结果在本次 Completion Report 中记录；该人工记录不替代
上述自动测试。

## 2026-08-12 实际执行记录

- PostgreSQL 18.4 Sprint 1 映射套件以 `python -m pytest <本表所列 Sprint 1 文件> -vv`
  最终执行，结果为 `83 passed in 42.92s`、退出码 0。完整仓库套件另以
  `python -m pytest -q` 执行并在 Completion Report 记录数量与退出码。
- Chrome `151.0.7922.76`：登录、首页、公司、三级部门、人员、三级实物分类、三级位置、
  setup、POST 退出/重登均完成；列表/表单/详情返回 200，保存动作经 302 重定向后返回 200，
  setup 数据在退出重登后保持。应用运行时资源仅请求本机 `/static/`。
- Edge `151.0.4129.72`：公司/部门/人员/实物分类/位置列表与中文表单返回 200；公司币种
  `USD` 被中文错误拒绝且数据未变；三级树显示正常；实物分类停用确认成功；system_admin
  人员维护返回 403，HR 表单返回 200；应用运行时资源仅请求本机 `/static/`。
- Chrome 扩展明确拒绝把自动化侧本机路径注入 `<input type=file>`；因此真实浏览器中的
  XLSX 文件选择、混合预览和修正文件确认未伪造为已完成。对应上传请求、混合预览不落正式表、
  原子确认、幂等和审计由本表 PostgreSQL 自动节点覆盖；此项作为 Completion Report 的已知
  人工冒烟限制明确披露。
