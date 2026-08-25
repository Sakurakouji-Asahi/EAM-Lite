# Database Design V1.1

本文件定义 EAM-Lite V1.1 的逻辑数据模型和不可省略的数据库约束。字段名可以按 Django 规范做小幅调整，但数据含义、公司边界、唯一约束、删除策略、事务和不可变历史不得弱化。

## 1. 全局约定

### 1.1 技术与数据类型

- 生产数据库：PostgreSQL；SQLite 只用于明确标注的非并发本地开发，不能作为并发或约束验收环境。
- 主键推荐 UUID；不得把连续主键暴露为二维码凭据。
- 所有时间戳为 timezone-aware，数据库存 UTC，业务输入与展示使用 `Asia/Shanghai`。
- 币种 V1 固定显示 CNY，但仍保存配置；金额使用 `Decimal` / `NUMERIC(18,2)`。
- 比率使用 `NUMERIC(12,8)`，工作量使用 `NUMERIC(20,4)`，不得使用 float。
- 金额按 `docs/08-Depreciation-Calculation-Spec.md` 使用 `ROUND_HALF_UP` 量化到 2 位。
- JSON 仅用于快照、选项或不可查询扩展，不得用 JSON 代替关键外键、金额列或状态列。

### 1.2 单公司 V1 与公司作用域

V1 的部署只允许一个活动公司，但业务表仍显式保存 `company_id`。至少包括编码、资产、财务、盘点、保养、处置、离职清理、导入导出、设置和幂等记录。

要求：

1. 初始化完成后只允许一个 `Company.is_active=true`；尝试创建第二个活动公司必须被后端和数据库部分唯一约束阻止。
2. 每次业务查询先按当前公司过滤，再应用部门范围；不得依靠前端隐藏。
3. 所有跨表写入在 Service 中校验公司一致；生产 PostgreSQL 使用复合唯一键/外键或延迟约束触发器阻止绕过 Service 的跨公司引用。
4. 对需要复合校验的表建立 `UNIQUE(id, company_id)`，引用双方均带公司；若 Django 版本不便表达复合 FK，迁移中建立 PostgreSQL constraint trigger，并有数据库测试。
5. V1 不提供切换公司 UI。保留 `company_id` 是隔离边界，不代表已支持多租户。

### 1.3 删除策略

- `PROTECT/RESTRICT`：公司、正式资产、已发编号、财务分类、已使用政策、已确认折旧、盘点快照、保养记录、处置记录、外部引用和审计历史。
- `SET_NULL`：责任人、操作人等离职或账号停用后仍需保留业务事实的可选引用；同时保存名称/编号快照。
- `CASCADE`：严格从属于尚未正式使用的配置片段、草稿暂存行、批次明细；父记录本身受状态和权限保护。
- 正式业务通过状态、归档、冲销和更正处理，不提供普通物理删除入口。

## 2. 身份、公司与主数据

### 2.1 `User`

使用 Sprint 0 创建的自定义 Django User。角色使用 Django Group/Permission；公司和部门数据范围按 `docs/07-Permissions-and-Workflows.md` 实施。

### 2.2 `Company`

字段：`id, code, normalized_code, name, short_name, currency, timezone, is_active, created_at, updated_at`。

约束：

- `UNIQUE(normalized_code)`；代码 NFKC 规范化且不区分大小写。
- `currency='CNY'`、`timezone='Asia/Shanghai'` 为 V1 初始化值。
- PostgreSQL 部分唯一索引保证最多一个 `is_active=true`。
- 被任何业务记录引用后 `on_delete=PROTECT`。

### 2.3 `Department`

字段：`id, company_id, code, normalized_code, name, parent_id, manager_employee_id, is_active, created_at, updated_at`。

外键：`company -> Company PROTECT`；`parent -> Department PROTECT/NULL`；`manager_employee -> Employee SET_NULL/NULL`。

约束：`UNIQUE(company_id, normalized_code)`；父级与本部门同公司；`parent_id != id`；递归校验和数据库触发器禁止任意深度循环。已有资产或子部门时只能停用，不能删除。

新绑定的 `manager_employee` 必须与部门属于同一公司，并同时满足
`employment_status='active' AND is_active=true`，且该员工当前所属部门必须启用；经理可以来自
公司内任意启用部门，不要求具备登录账号或 `department_manager` 角色。经理进入
`leaving/resigned`、员工被停用或其所属部门被停用时，只能由受控 Service 在同一事务中清空其
全部 `Department.manager_employee` 关联，并对每一项清空分别写入带公司的 AuditLog；不得通过
普通表单、批量 `update()` 或绕过 Service 的方式留下失效经理关联。

### 2.4 `Employee`

字段：`id, company_id, employee_no, normalized_employee_no, name, department_id, user_id, employment_status, hire_date, termination_date, mobile, remark, is_active, created_at, updated_at`。

外键：`company -> Company PROTECT`；`department -> Department PROTECT`；`user -> User SET_NULL/NULL`。

约束：

- `UNIQUE(company_id, normalized_employee_no)`；一个 User 在同一公司最多绑定一个 Employee。
- `employment_status in (active, leaving, resigned)`。
- `employment_status` 是 HR 任职流程状态；`is_active` 是该人员主数据能否参与新业务的启停标志，两者不得由表单互相猜测。可接收新责任、调拨或内部借用的唯一谓词为 `employment_status='active' AND is_active=true`，并同时要求公司、部门有效。
- `active` 允许 `is_active=true/false`（后者用于尚未离职但已被管理停用的记录）；进入 `leaving` 的受控事务必须明确把 `is_active=false`，`leaving/resigned` 均不得再改回 true。V1 不提供 `resigned -> active` 普通转换，纠错须另行批准并留痕。User 账号的 `is_active` 独立管理，不与 Employee 静默联动。
- 部门必须同公司；仅 `resigned` 时 termination_date 必填，`active/leaving` 时必须为空。
- 账号停用或员工离职不级联删除历史。

### 2.4.1 `UserDepartmentScope`

字段：`id, company_id, user_id, department_id, include_descendants, is_active, assigned_by_id, assigned_at, revoked_by_id, revoked_at`。

外键：`company -> Company PROTECT`；`user -> User PROTECT`；`department -> Department PROTECT`；分配/撤销人 `SET_NULL`。

约束与行为：

- PostgreSQL 部分唯一索引保证同一 `(company_id, user_id, department_id)` 最多一个 `is_active=true` 授权。
- 部门必须属于同一公司；V1 只有一个活动公司，若 User 已绑定 Employee，该 Employee 也必须属于同一公司。
- `include_descendants=true` 表示查询时包含该部门当前全部下级；不自动包含上级或同级。部门改挂父级时必须在同一受控 Service 中重新计算受影响授权、显示影响摘要并写安全审计。
- 只有 `system_admin` 可分配或撤销；撤销使用状态和时间字段，不物理删除历史授权。角色允许动作仍由 Group/Permission 决定，有部门授权不等于自动获得 `department_manager` 角色。
- 所有部门范围 QuerySet、对象 Service、导入、导出、附件和扫码使用同一授权范围解析器，禁止各页面自行拼接过滤条件。

### 2.5 `Location`

字段：`id, company_id, code, normalized_code, name, parent_id, level, location_type, is_active, created_at, updated_at`。

外键：`company -> Company PROTECT`；`parent -> Location PROTECT/NULL`。

约束：`UNIQUE(company_id, normalized_code)`；父级同公司；禁止循环；`level>=1` 并由父路径计算。`location_type in (site, workshop, department_area, warehouse, office, position, other)`。资产只保存一个叶级位置 FK，三级选择器不是三列位置。

### 2.6 `AssetCategory`（物理/管理分类）

字段：

`id, company_id, code, normalized_code, name, parent_id, category_level, category_type, default_coding_scheme_id, default_depreciation_policy_id, is_maintenance_required_default, is_active, created_at, updated_at`。

外键：`company -> Company PROTECT`；`parent -> AssetCategory PROTECT/NULL`；默认编码方案和折旧政策均 `SET_NULL`，但必须同公司且为可选用版本。

约束：

- `UNIQUE(company_id, normalized_code)`；父级同公司且无循环。
- `category_level` 从路径计算，可支持大类、二级小类和更深叶级。
- `category_type in (equipment, mold, tool, inspection_tool, office_equipment, other)`，该字段只表示物理类型。
- 该表只表达实物管理分类。不得在此存 `fixed_asset/controlled_asset` 混合枚举，也不得由 `category_type` 自动推断会计分类。

### 2.7 `FixedAssetCategory`（会计分类）

字段：`id, company_id, code, normalized_code, name, useful_life_months_default, note, is_active, created_at, updated_at`。

外键：`company -> Company PROTECT`。约束：`UNIQUE(company_id, normalized_code)`；默认年限大于 0。被 `AssetFinance` 引用后只能停用，不能删除。

## 3. 编码登记

完整行为见 `docs/03-Asset-Coding-Rules.md`。

### 3.1 `AssetCodingScheme`

字段：`id, company_id, scheme_key, version, name, description, status, is_default, reset_mode, sequence_start, category_scope_level, effective_from, effective_to, previous_version_id, created_by_id, created_at, updated_at`。

外键：`company -> Company PROTECT`；`previous_version -> self PROTECT/NULL`；`created_by -> User SET_NULL`。

约束：

- `UNIQUE(company_id, scheme_key, version)`。
- 同公司最多一个当前活动默认版本（部分唯一索引）。
- 同一 `scheme_key` 活动生效区间不能重叠（PostgreSQL exclusion constraint 或等效触发器）。
- `version>=1`、`sequence_start>=0`、`effective_to>=effective_from`。
- `status in (draft, active, retired)`；`reset_mode` 和分类层级取值以编码规范为准。
- 存在 `IssuedCode` 后禁止 UPDATE 影响规则的字段，禁止 DELETE。

`effective_from/effective_to` 均为上海业务日 `DateField`，采用闭区间；草稿两者可空，active
必须有 `effective_from` 且允许未来生效、过期或无结束日。同公司 `status=active AND
is_default=true` 至多一行，当前可用性再由 Service 按上海业务日判断；闭区间端点相同算重叠。

### 3.2 `AssetCodingSegment`

字段：`id, coding_scheme_id, sequence_order, segment_type, fixed_value, format_string, sequence_length, zero_pad, created_at`。

外键：`coding_scheme -> AssetCodingScheme CASCADE`（仅未使用 draft 可删除）。

约束：`UNIQUE(coding_scheme_id, sequence_order)`；`sequence_order>=1`；每方案恰有一个 `sequence` 片段由延迟触发器/启用 Service 校验；方案使用后片段不可增删改。

字段组合必须由数据库 CHECK 和 Service 同时按以下矩阵执行：

| 片段类型 | `fixed_value` | `format_string` | `sequence_length` | `zero_pad` |
|---|---|---|---|---|
| `fixed_text`、`custom_text` | 必填 | NULL | NULL | NULL |
| `separator` | 必填，且精确为单个 `-`、`_`、`.` 或 `/` | NULL | NULL | NULL |
| `sequence` | NULL | NULL | 必填，范围 1–12 | 必填布尔值，`true`/`false` 均合法 |
| 其余来源片段和日期片段 | NULL | NULL | NULL | NULL |

`format_string` 在 V1 对全部片段强制为 NULL，且不得由 Form、UI 或 API 暴露或接受。`fixed_text/custom_text` 不得为空，不得含首尾空白、控制字符或花括号。日期输出固定为 `YYYY`、`YYYYMM`、`YYYYMMDD`，不能配置自定义格式。`custom_field` 不得进入 V1 数据库枚举、Form、UI 或 API。最终渲染编号不得超过 64 字符；超过时方案不得启用。

### 3.3 `SequenceCounter`

字段：`id, company_id, coding_scheme_id, scope_key, current_value, created_at, updated_at`。

外键：公司和方案均 `PROTECT`。约束：

- `UNIQUE(company_id, coding_scheme_id, scope_key)`。
- `current_value>=sequence_start-1`。
- 公司与方案一致。

首次并发创建使用 `INSERT ... ON CONFLICT DO NOTHING`，随后 `SELECT FOR UPDATE`；禁止 `MAX+1`。

### 3.4 `IssuedCode`

字段：

`id, company_id, coding_scheme_id, scope_key, sequence_value, display_code, normalized_code, effective_date, effective_date_reason, status, idempotency_key, issued_by_id, issued_at, replaced_or_voided_reason, replaced_or_voided_at`。

外键：`company -> Company PROTECT`；`coding_scheme -> AssetCodingScheme PROTECT`；`issued_by -> User SET_NULL`。

约束：

- `UNIQUE(company_id, normalized_code)`：所有 active/replaced/voided 行共同参与。
- `UNIQUE(company_id, idempotency_key)`。
- `UNIQUE(company_id, coding_scheme_id, scope_key, sequence_value)`。
- `status in (active, replaced, voided)`；原因字段与状态匹配。
- `effective_date` 不得为未来上海业务日；早于签发日时 `effective_date_reason` 必填，普通当日签发时可空。
- 应用无删除入口；数据库 `REVOKE DELETE` 给应用业务角色。

### 3.5 `AssetCodeHistory`

字段：`id, company_id, asset_id, event_type, old_issued_code_id, new_issued_code_id, reason, effective_at, operated_by_id, created_at`。

外键：资产和两个编号均 `PROTECT`，操作人 `SET_NULL`。约束：

- `event_type in (issued, corrected, voided)`。
- 首发：old 为空、new 必填；更正：old/new 均必填且不同；作废：old 必填、new 为空。
- 所有对象同公司；历史只追加。

## 4. 资产主档、二维码与外部引用

### 4.1 `Asset`

字段：

`id, company_id, asset_code, current_issued_code_id, requested_coding_scheme_id, asset_status, record_status, asset_name, category_id, brand, model, manufacturer, serial_number, factory_number, historical_code, tracking_mode, quantity, unit, description, department_id, responsible_employee_id, location_id, acquisition_date, commissioning_date, is_maintenance_required, initialization_source, initialization_date, initialized_by_id, notes, created_by_id, created_at, updated_by_id, updated_at`。`asset_code` 在财务确认前为 NULL，不得用空字符串代替。`commissioning_date` 是达到可使用状态日期的唯一业务字段，财务确认时核对后锁定；不得在 `AssetFinance` 再存一份同义日期。

外键：

- `company -> Company PROTECT`
- `current_issued_code -> IssuedCode PROTECT/NULL`，OneToOne；`IssuedCode` 是永久占号真源，`Asset.asset_code` 是受保护的当前显示镜像
- `requested_coding_scheme -> AssetCodingScheme PROTECT/NULL`；仅表示财务确认前 system_admin 明确选择的具体活动版本，最终真源仍为 `IssuedCode.coding_scheme_id`
- `category -> AssetCategory PROTECT`
- `department -> Department PROTECT/NULL`
- `responsible_employee -> Employee PROTECT/NULL`
- `location -> Location PROTECT/NULL`
- 用户引用均 `SET_NULL`

约束：

- `asset_status in (draft, pending_finance, pending_label, in_use, idle, loaned, under_repair, pending_disposal, disposed, sold, other_disposed)`。
- `record_status in (active, archived)`。
- V1 `tracking_mode='single_item'` 且 `quantity=1`；不实现批量资产、部分调拨或部分处置。
- `asset_code` 为 NULL 当且仅当 `current_issued_code_id` 为 NULL；非空时必须等于该登记的 `display_code`。使用可延迟 PostgreSQL constraint trigger 保证事务提交时一致，普通表单不得直接编辑该字段；另建 `UNIQUE(company_id,asset_code)`，永久防复用仍由 `IssuedCode` 的全状态唯一约束承担。
- `draft`、`pending_finance` 必须没有正式编号；只有财务确认的 `pending_finance -> pending_label` 原子事务才正式发号并建立 QR 身份。`pending_label` 及其后状态必须有正式编号和 active QR 身份。
- `pending_label` 及其后正式状态必须存在同公司、已确认的 OneToOne `AssetFinance`；无论会计处理为固定资产还是受控非固定资产，都必须有可用于处置和对账的原值。
- `requested_coding_scheme` 必须同公司且在正式化生效日可用，只能由 system_admin 在 draft/pending_finance 阶段设置；正式发号后锁定。为空时才按物理类别默认、公司默认顺序解析，不得静默替换已明确但失效的版本。
- `in_use/idle/loaned/under_repair/pending_disposal` 必须有同公司部门、责任人和叶级位置。
- 会计认定不存于 Asset，唯一来源是 `AssetFinance.accounting_treatment`；不能按 5,000 元自动写入或从物理分类推断。
- `disposed/sold/other_disposed` 必须有已确认处置记录；正式资产不物理删除。

所有状态转换必须走 Service 并写 AuditLog。draft↔pending_finance 由提交/退回审计留痕，pending_finance→pending_label 另由不可变财务确认、编号和 QR 记录留痕；从 pending_label→in_use/idle 开始的实物/生命周期状态变化必须写 `AssetMovement`，处置还必须写 Disposal。`record_status` 的归档/恢复不改变 asset_status，由专门 Service 校验终态并写 AuditLog，不伪造成实物 Movement。跨公司外键由数据库约束触发器拒绝。

### 4.2 `AssetCustomField` / `AssetCustomValue`

`AssetCustomField`：`id, company_id, category_id, name, code, field_type, required, options_json, display_order, is_active`；公司/类别 `PROTECT`；`UNIQUE(company_id, code)`。

`field_type` 精确取值为 `text/decimal/date/boolean/select`。`select` 时 `options_json` 必须是至少含一项、去重后的非空字符串 JSON 数组；其他类型时 `options_json` 必须为 NULL。code 使用 NFKC 规范化后在公司内唯一，类别必须同公司。`required=true` 表示资产提交财务确认前必须存在合法值，不表示为每个草稿预建空 Value 行。

`AssetCustomValue`：`id, company_id, asset_id, custom_field_id, value_text, value_decimal, value_date, value_boolean`；`asset -> Asset CASCADE`（只有可删除草稿会触发）、`custom_field -> AssetCustomField PROTECT`；`UNIQUE(asset_id, custom_field_id)`；本行 CHECK 使用 `num_nonnulls(...)=1`，再由可延迟 PostgreSQL constraint trigger 校验该列与被引用 Field 的 field_type/选项对应；所有对象同公司。不得编写引用另一张表而 PostgreSQL 实际无法执行的普通 CHECK。

值列映射固定为：`text/select -> value_text`、`decimal -> value_decimal`、`date -> value_date`、`boolean -> value_boolean`；其余三列必须为空。select 的 value_text 必须精确属于该字段当前批准选项；字段已被正式资产使用后，删除/重命名既有选项必须克隆或停用字段并保留历史显示，不能让旧值失去解释。

### 4.3 `AssetQrIdentity`

字段：`id, company_id, asset_id, public_token, status, label_status, issued_at, issued_by_id, revoked_at, revoked_by_id, revoke_reason, attached_at, attached_by_id, version`。

外键：`asset -> Asset PROTECT`；用户 `SET_NULL`。约束：

- 每资产最多一个 `status='active'` 身份（部分唯一索引）。
- `UNIQUE(public_token)`；Token 至少包含 128 bit CSPRNG 随机熵。它必须可供授权的标签打印流程重复读取，因此作为受保护的公开标识保存；它不是授权凭据，扫码后仍须登录和鉴权。
- `status in (active, revoked)`。
- `label_status in (not_generated, ready_to_print, printed, attached)`，时间字段必须与状态一致。
- `printed -> attached` 可由扫描当前 Token 或有标签操作权限的 Web 资产页逐项确认触发；两种入口必须调用同一原子 Service，校验当前 active 身份、打印状态、资产责任资料和幂等键，并在 AuditLog 中记录确认方式。Web 页面不得提供无实物核对的批量附着动作。
- 换标在一个事务内先锁资产并撤销旧身份，再创建/激活新版本，满足任一时点最多一个 active 的约束；事务提交前外部不可见。旧 Token 永久失效且记录保留。

### 4.4 `AssetLabelPrintBatch` / `AssetLabelPrintItem`

批次字段：`id, company_id, batch_code, template_version, status, created_by_id, created_at, printed_by_id, printed_at, idempotency_key`；`UNIQUE(company_id,batch_code)`、`UNIQUE(company_id,idempotency_key)`；状态 `draft/generated/printed/cancelled`。

明细字段：`id, batch_id, qr_identity_id, page_no, position_no, print_status, created_at`；外键批次 `CASCADE`、QR `PROTECT`；`UNIQUE(batch_id, qr_identity_id)` 和 `UNIQUE(batch_id,page_no,position_no)`；`print_status in (generated,printed,cancelled)` 并与父批次最终状态一致。Service 可在同一事务内部短暂建立 `generated` 批次/明细以固定快照，但用户点击打印并生成 A4 预览的事务提交前必须直接写入操作人/时间，并把批次、明细和当前 QR 转为 `printed`；页面不再提供独立“确认打印”。历史遗留的单项 `generated` 预览可在实际贴标确认时自动取消并保留审计，多资产旧预览仍要求在批次页明确处理。打印只更新标签状态，不改变资产编号。

### 4.5 `AssetExternalReference`

该模型在 Sprint 11 随 T+ 对账导出建立；前序 Sprint 不创建空壳表或 T+ 写入入口。

字段：`id, company_id, asset_id, external_system, reference_type, reference_value, normalized_value, note, created_by_id, created_at, updated_at`。

外键：`asset -> Asset PROTECT`；公司 `PROTECT`；用户 `SET_NULL`。

约束：

- `UNIQUE(company_id, external_system, reference_type, normalized_value)`。
- `UNIQUE(asset_id, external_system, reference_type)`。
- V1 允许 `external_system='TPLUS'`、`reference_type='asset_card_code'`；没有外部卡片号时不创建行，不得以空字符串占位，且缺失不阻断建档。
- 仅用于 Excel 导出和人工对账；此表不授权、触发或暗示写入 T+。

## 5. 财务与折旧

### 5.1 `AssetFinance`

字段：

`id, company_id, asset_id, accounting_treatment, accounting_treatment_reason, recognition_threshold_snapshot, fixed_asset_category_id, original_cost, capitalization_date, impairment_balance_cache, finance_confirmed_by_id, finance_confirmed_at, finance_remark, created_at, updated_at`。

外键：资产 OneToOne `PROTECT`；会计分类 `PROTECT/NULL`；确认人 `SET_NULL`。

约束：

- `accounting_treatment` 允许 NULL 仅表示 draft/pending_finance 阶段尚未认定；数据库/API 不保存第三个字符串 `unconfirmed`。非空值只允许 `fixed_asset/controlled_non_fixed`，财务确认时必须非空。
- `finance_confirmed_by_id/finance_confirmed_at` 必须同时为空或同时非空：两者为空且 Asset 仍为 draft/pending_finance 时，该行只是 finance 可编辑、业务查询不得当作已确认账面值的财务草稿；两者非空才是确认财务真源，并要求 Asset 已在同一事务转为 pending_label。财务草稿不得拥有 active Profile、Schedule 或任何 Entry。
- 确认时保存当时生效的 `recognition_threshold_snapshot`，用于以后解释警告口径；设置变更不回写历史快照。
- 所有通过财务确认并进入正式状态的资产都必须有非空 `original_cost>=0`；`AssetFinance.original_cost` 是当前原值权威余额。`fixed_asset` 另要求固定资产类别、资本化日期、Asset.commissioning_date 及确认信息必填。
- `controlled_non_fixed` 时固定资产类别必须为空，且不得存在 active 折旧 Profile 或任何折旧 Entry；V1 的实际累计折旧和累计减值固定为 0，不创建减值类 Adjustment。其处置快照因此明确为 `book_value_snapshot = original_cost_snapshot - 0 - 0`，不会因缺少成本或折旧 Profile 而无法完成。
- 若原值达到/超过确认时公司提示阈值却选择 `controlled_non_fixed`，`accounting_treatment_reason` 必填并随确认快照保存；阈值以后改变不得追溯清空该原因。
- 残值、方法、寿命和起止规则的唯一来源是生效的 `AssetDepreciationProfile`，不在 AssetFinance 重复保存。
- `impairment_balance_cache` 只能由所有已过账（status 为 confirmed 或因已存在反向记录而标为 reversed）的 opening/减值/减值转回调整按财务效果代数汇总生成，只读且可重建；减值真源是完整 `AssetValueAdjustment` 链。
- 不保存可被手工修改的“实际累计折旧/当前净值”真源；它们从已确认 `DepreciationEntry` 和已确认价值调整汇总，可有带刷新时间的只读缓存/数据库视图。

### 5.2 `DepreciationPolicy`

字段：

`id, company_id, policy_key, version, name, method, posting_period, start_rule, stop_rule, default_useful_life_months, default_salvage_mode, default_salvage_rate, default_salvage_amount, annual_posting_month, work_unit, status, is_default, effective_from, effective_to, previous_version_id, created_by_id, created_at, updated_at`。

外键：公司 `PROTECT`、前版 `PROTECT/NULL`、用户 `SET_NULL`。

约束：`UNIQUE(company_id,policy_key,version)`；状态 `draft/active/retired`；同一 `policy_key` active 生效期不得重叠，同公司最多一个当前 active `is_default=true` 版本；`posting_period in (monthly,yearly)`、`default_salvage_mode in (rate,amount)`、`stop_rule in (event_date,next_month)`，方法和起算规则的精确 token 以折旧规范为准。rate 模式要求 `0<=default_salvage_rate<=1` 且 amount 为空；amount 模式要求 amount 非负且 rate 为空。yearly 必须有 `annual_posting_month 1..12`，monthly 时该字段为空；已被 Profile 使用后只允许克隆新版本。政策选择顺序为单项明确政策 → `AssetCategory.default_depreciation_policy` → 公司当前默认政策；找不到或不唯一时阻止确认。选定政策后，Profile 参数的默认顺序为单项显式值优先；仅 useful_life_months 可再取 `FixedAssetCategory.useful_life_months_default`；其余及仍缺失的寿命取选定 Policy 默认。最终确认值只存入不可变 Profile，不把多个默认来源当并行账面真源。

### 5.3 `AssetDepreciationProfile`

字段：

`id, company_id, asset_id, depreciation_policy_id, version, method, posting_period, start_rule, stop_rule, start_date, useful_life_months, salvage_mode, salvage_rate, salvage_amount, opening_book_value, opening_actual_accumulated_depreciation, expected_total_units, work_unit, annual_posting_month, effective_from, effective_to, status, change_reason, created_by_id, created_at`。

外键：资产、政策均 `PROTECT`；用户 `SET_NULL`。

约束：

- `UNIQUE(asset_id,version)`；同资产有效期不得重叠。
- `status in (draft, active, suspended, completed, stopped)`；数值和方法必填组合按折旧规范 CHECK。
- `posting_period in (monthly,yearly)`、`salvage_mode in (rate,amount)`；rate 模式要求 `0<=salvage_rate<=1` 且 salvage_amount 为空，amount 模式要求 salvage_amount 非负且 salvage_rate 为空。yearly 必须有 `annual_posting_month 1..12`，monthly 时该字段为空。
- opening book/累计折旧只允许在初始化首版填写；确认后生成唯一 opening 来源分录，不得重复计入。
- 已产生确认分录的 Profile 不可编辑；参数变更克隆新版本并前瞻生效。

### 5.4 `DepreciationSchedule`

字段：`id, company_id, asset_id, depreciation_profile_id, sequence_no, period_start, period_end, opening_book_value, calculated_unrounded, planned_amount, planned_accumulated, closing_book_value, planned_units, eligible_fraction, formula_snapshot_json, status, created_at`。

外键：资产/Profile 均 `PROTECT`。约束：`UNIQUE(depreciation_profile_id,sequence_no)` 和 `UNIQUE(depreciation_profile_id,period_start,period_end)`；公司一致；状态 `planned/superseded`。Schedule 是 Profile 确认时形成的未来计划/理论排程，不是实际分录；Profile 新版本产生新 Schedule，旧行改为 superseded 而不覆盖。BatchItem 可以引用其适用行，但只有已确认 Entry 才进入实际累计。

### 5.5 `DepreciationProfileEvent`

字段：`id, company_id, asset_id, depreciation_profile_id, event_type, effective_date, reason, source_disposal_id, previous_profile_status, reverses_event_id, created_by_id, created_at`。

外键：资产/Profile `PROTECT`；`source_disposal -> AssetDisposal PROTECT/NULL`；`reverses_event -> self PROTECT/NULL`；用户 `SET_NULL`。基础事件 `suspend/resume/stop` 只追加，source_disposal/reverses_event/previous_profile_status 为空，状态顺序、日期不倒退且公司一致。

Sprint 4 初建基础事件；`source_disposal` 的目标模型到 Sprint 7 才存在，因此 Sprint 7 以跟踪迁移增加该真实 FK，同时启用 `disposal_stop/disposal_restore` 两个 event_type，禁止用裸整数或自由文本 reason 代替来源。处置完成只对当时 `active/suspended` Profile 新增一条 `disposal_stop`：source_disposal 必填、previous_profile_status 保存原值、effective_date 按 stop_rule 解析，并把 Profile 置 stopped。处置冲销新增唯一 `disposal_restore`，source_disposal 相同、reverses_event 一对一指向该 stop、effective_date 精确等于被反向 stop 的日期，并把 Profile 恢复为 previous_profile_status；不得删除 stop 事件。

字段组合固定：disposal_stop 要求 source_disposal + previous_profile_status，reverses_event 为空；disposal_restore 要求 source_disposal + reverses_event，previous_profile_status 为空；基础三类全部为空。previous_profile_status 只允许 active/suspended，restore 只能反向 disposal_stop。

数据库部分唯一/触发器保证每个 disposal/profile 最多一条 disposal_stop、每条 stop 最多一条 restore，且资产、公司、Profile、Disposal 全部一致。存在 stop 后的新 Profile 版本、人工事件、已确认期间或其他冲突时必须先按折旧规范处理或阻断冲销；恢复只重建未确认计划/资格，不自动创建实际 Entry。

Profile.status 在处置事务中立即置 stopped 以禁止新配置/普通完成，但具体期间是否仍应计提必须按事件 effective_date 判断，不能仅因当前 status=stopped 跳过 next_month 规则下尚应确认的处置当月。

### 5.6 `AssetWorkUsage`

字段：`id, company_id, asset_id, depreciation_profile_id, period_start, period_end, work_unit, opening_accumulated_units, current_units, closing_accumulated_units, entered_by_id, entered_at, remark`。

外键：资产/Profile `PROTECT`；用户 `SET_NULL`。

约束：`UNIQUE(depreciation_profile_id,period_start,period_end)`；数值非负；opening + current = closing；累计不得倒退或超过预计总工作量（最后期允许由规则封顶）。

### 5.7 `DepreciationBatch`

所有月度确认通过批次完成。字段：

`id, company_id, period_start, period_end, generation_no, batch_type, status, idempotency_key, request_hash, generated_by_id, generated_at, confirmed_by_id, confirmed_at, reverses_batch_id, supersedes_batch_id, reversal_reason, created_at`。

外键：公司 `PROTECT`；用户 `SET_NULL`；被冲销/替代批次 `PROTECT/NULL`。

约束：

- `UNIQUE(company_id,period_start,generation_no,batch_type)`、`UNIQUE(company_id,idempotency_key)`。
- 月批次必须覆盖完整自然月；`batch_type in (regular,reversal)`；`status in (draft,confirmed,reversed,cancelled)`。
- reversal 必须一对一指向一个已确认 regular 批次；一个原批次只能有一个已确认 reversal（部分唯一索引）。
- 同公司同月份最多一个未被冲销的 confirmed regular 批次；重算以更高 generation_no 和 supersedes 链保存。
- confirmed 后批次与明细不可编辑；冲销创建反向批次，不把原行改回 draft。

### 5.8 `DepreciationBatchItem`

字段：

`id, company_id, batch_id, asset_id, depreciation_profile_id, depreciation_schedule_id, calculation_method, opening_book_value, depreciable_floor, eligible_fraction, usage_units, manual_amount, manual_reason, manual_entered_by_id, manual_entered_at, calculated_unrounded, planned_amount, closing_book_value, calculation_snapshot_json, status, error_message, created_at`。

外键：`batch -> DepreciationBatch CASCADE`（仅草稿父批次可删除）；资产、Profile 均 `PROTECT`；Schedule `PROTECT/NULL`；manual 录入人 `SET_NULL`；所有对象同公司。

约束：`UNIQUE(batch_id,asset_id)`；金额为 2 位 Decimal；`0<=eligible_fraction<=1`；状态 `ready/skipped/error`。manual 方法必须有 Decimal `manual_amount`、原因、录入人/时间，其他方法这些字段为空；手工 0 也要求原因。确认批次前不允许 error，且所有快照完整。

### 5.9 `AssetValueAdjustment`

字段：`id, company_id, asset_id, adjustment_type, effective_date, amount, old_values_json, new_values_json, reason, status, confirmed_by_id, confirmed_at, reversal_of_id, created_by_id, created_at`。

外键：资产和 `reversal_of -> AssetValueAdjustment PROTECT/NULL`；用户 `SET_NULL`。类型至少 `opening_impairment/impairment/impairment_reversal/cost_correction/depreciation_adjustment`；状态 `draft/confirmed/reversed`。`opening_impairment` 仅允许初始化财务确认时创建一次。非空 reversal_of 全局唯一，保证一个原调整最多一条冲销；只能指向同资产同公司的 confirmed 原行，冲销行自身不能再被冲销。

金额符号固定：opening_impairment、impairment、impairment_reversal 保存正数幅度，累计减值 `I = opening + impairment - impairment_reversal`；cost_correction 保存对原值的有符号增量，并在同一事务更新权威 `AssetFinance.original_cost`；depreciation_adjustment 保存对实际累计折旧的有符号增量并生成同额 DepreciationEntry。任何类型都不得使原值、累计折旧、累计减值或账面值违反折旧规范。

已确认行不可编辑。错误冲销新建带 reversal_of 的 confirmed 行并把原行标为 reversed：冲销 opening_impairment/impairment 使用同幅度正数 `impairment_reversal`；冲销 impairment_reversal 使用同幅度正数 `impairment`；冲销 cost_correction/depreciation_adjustment 使用同类型、金额精确相反的行。depreciation_adjustment 冲销同时生成反向 DepreciationEntry，并令其 reversal_of 指向原调整 Entry。原行与反向行都参与代数汇总；`reversed` 只表示已有反向记录，绝不表示从真源查询中删除原效果。报表和处置读取 `AssetFinance.original_cost` 当前余额，Adjustment 链用于解释和重建该余额，不得把当前余额再与所有 cost_correction 重复相加。

### 5.10 `DepreciationEntry`（实际折旧唯一真源）

字段：

`id, company_id, asset_id, depreciation_profile_id, entry_date, period_start, period_end, source_type, batch_item_id, opening_profile_id, value_adjustment_id, amount, accumulated_depreciation_after, book_value_after, reversal_of_id, posted_by_id, posted_at, created_at`。

外键：资产/Profile/所有来源均 `PROTECT`；用户 `SET_NULL`。

来源约束：

- `source_type='batch'`：`batch_item_id` 必填，其余 opening/adjustment 来源为空。
- `source_type='opening'`：`opening_profile_id` 必填且只能是该资产首版 Profile。
- `source_type='adjustment'`：`value_adjustment_id` 必填，且只能引用 `adjustment_type='depreciation_adjustment'`。`opening_impairment/impairment/impairment_reversal/cost_correction` 不得生成折旧 Entry；它们分别进入减值或原值真源，避免重复计入累计折旧。
- `reversal_of_id` 是对来源分录的反向关系，不算第二业务来源；反向金额必须等于原金额负数。
- PostgreSQL 部分唯一索引分别保证一个 batch item、opening profile、value adjustment 只产生一条实际分录；一个原分录最多一个有效 reversal。
- 除 reversal/明确调整外 amount 不得为负；任何确认后分录只追加、不可 UPDATE/DELETE。

`实际累计折旧 = SUM(所有已确认 DepreciationEntry.amount)`。计划表、草稿批次、理论运行和可编辑缓存一律不得进入实际累计折旧。

### 5.11 `TheoreticalDepreciationRun` / `TheoreticalDepreciationLine`

Run 字段：`id, company_id, asset_id, as_of_date, parameter_snapshot_json, status, requested_by_id, requested_at, completed_at, idempotency_key`；`UNIQUE(company_id,idempotency_key)`。

Line 字段：`id, run_id, period_start, period_end, theoretical_amount, theoretical_accumulated, theoretical_book_value, formula_snapshot_json`；`UNIQUE(run_id,period_start,period_end)`。

外键 Run -> Asset `PROTECT`，Line -> Run `CASCADE`，用户 `SET_NULL`。理论运行是带参数快照的参考结果，不生成 `DepreciationEntry`，不覆盖实际数。若财务接受差额，必须另建已确认调整或后续批次。

## 6. 资产变动历史

### 6.1 `AssetMovement`

该表在 Sprint 6 为 `label_activation` 建立；Sprint 7 在同一模型上启用其余生命周期类型。Sprint 6 不得因 Sprint 7 尚未开始而省略贴标状态历史，Sprint 7 也不得新建第二套 Movement。

字段：

`id, company_id, asset_id, movement_type, effective_at, from_department_id, to_department_id, from_employee_id, to_employee_id, from_location_id, to_location_id, from_status, to_status, reason, remark, idempotency_key, operated_by_id, created_at`。

所有资产/主数据外键 `PROTECT`，用户 `SET_NULL`。`UNIQUE(company_id,idempotency_key)`；`movement_type in (assignment,assignment_return,transfer,loan,loan_return,idle,activate,repair_start,repair_complete,label_activation,disposal_start,disposal_cancel,disposal_complete,disposal_reversal)`。from/to 至少一组变化且与保存前后值完全一致；公司一致。

V1 不执行未来定时生效，`effective_at` 不得晚于当前上海业务时间；普通操作不得早于该资产已存在的相关维度最新生效记录，追溯纠错必须走有原因、权限和链路的专门更正。调拨 Service 锁资产，在同一事务更新主档、写 movement 和 AuditLog。

### 6.2 `AssetLoan`

借出/归还的结构化业务记录不能塞进 `AssetMovement.remark`。字段：

`id, company_id, asset_id, borrower_type, borrower_employee_id, borrower_name_snapshot, borrower_name, borrower_organization, loan_date, expected_return_date, handled_by_id, previous_asset_status, reason, status, returned_at, received_by_employee_id, return_department_id, return_responsible_employee_id, return_location_id, return_asset_status, return_remark, loan_movement_id, return_movement_id, loan_idempotency_key, return_idempotency_key, created_by_id, created_at, updated_at`。

外键：资产、`borrower_employee`、接收/责任员工、部门、位置和两个 Movement 均 `PROTECT`；操作用户 `SET_NULL`。约束：

- `status in (active, returned)`；一项资产最多一条 active 借出记录（PostgreSQL 部分唯一索引）。
- `UNIQUE(company_id,loan_idempotency_key)`；非空 `return_idempotency_key` 也按公司唯一。
- `borrower_type in (internal_employee,external)`。
- `borrower_type=internal_employee` 时 `borrower_employee_id` 必填且员工必须同公司并满足 `employment_status='active' AND is_active=true`；员工 ID 是离职清退和查询的权威关联，`borrower_name_snapshot` 由服务端从 Employee 保存且不可编辑，外部输入字段 `borrower_name/borrower_organization` 必须为空，不能只靠姓名文本匹配员工。
- `borrower_type=external` 时 `borrower_employee_id`、`borrower_name_snapshot` 必须为空，`borrower_name` 必填，`borrower_organization` 按实际情况填写；外部文本不得反向解析或自动绑定 Employee。类型与字段组合用数据库 CHECK 兜底。
- 借出日、预计归还日、经办人、原因必填；预计归还日不早于借出日。内部员工进入 leaving 时，未归还且 `borrower_employee_id` 指向该员工的 Loan 必须进入离职清退查询。
- active 时返回字段和 return_movement 为空，Asset 状态必须为 `loaned`；returned 时实际归还时间、接收人、目标部门/责任人/叶级位置、归还状态及 return_movement 必填。
- `loan_movement_id` 为非空 OneToOne（或非空唯一 FK），只能引用同公司同资产 `movement_type='loan'`；`return_movement_id` 为空仅限 active，returned 时为唯一 FK 且只能引用同公司同资产 `movement_type='loan_return'`。两列不得相同，任何 Movement 不得被另一 Loan 复用。
- 普通归还先恢复 `previous_asset_status`；如同一操作还需在 `in_use/idle` 间变化，必须额外通过批准的状态变更并在 Movement 中完整记录，不能静默覆盖。
- 借出和归还各自在单一事务内锁 Asset/Loan，写结构化记录、对应 `AssetMovement`、更新 Asset 当前值/状态并写 AuditLog；借出记录完成后不可删除或普通编辑。

## 7. 通用附件

### 7.1 `Attachment`

字段：`id, company_id, storage_key, original_filename, safe_filename, file_size, mime_type, sha256, uploaded_by_id, uploaded_at, malware_scan_status, is_available`。

外键：公司 `PROTECT`、用户 `SET_NULL`。`UNIQUE(company_id,storage_key)`；文件大小正数；MIME/扩展名/文件签名按安全文档校验。`storage_key` 不能是用户可控路径。附件不放公开 static 目录。

### 7.2 `AttachmentLink`

使用通用文件元数据 + 有真实 FK 的受控多目标关联，而不是无约束的 `object_type/object_id`。字段：

`id, company_id, attachment_id, role, security_class, status, void_reason, voided_by_id, voided_at, asset_id, maintenance_record_id, maintenance_problem_id, asset_disposal_id, inventory_surplus_id, inventory_scan_id, inventory_resolution_id, clearance_id, clearance_item_id, created_by_id, created_at`。

外键：`attachment -> Attachment PROTECT`；各业务目标列均允许 NULL，但其 `on_delete=CASCADE`（仅业务父对象按自身状态规则获准物理删除时连带删除 Link，正式父对象本身受保护）；用户 `SET_NULL`。CHECK 使用 PostgreSQL `num_nonnulls(...) = 1`，保证恰好一个业务目标。所有目标同公司。每个 Attachment 在 V1 只归属一个 Link（`UNIQUE(attachment_id)`）；需复用文件时创建新的受控附件记录。

`status in (active,voided)`；voided 时原因、操作人和时间必填，active 时为空。业务作废只改变 Link 状态并保留 Attachment 文件、摘要和元数据；`Attachment.is_available` 表示底层对象是否可安全提供，不得拿它覆盖业务作废事实。普通列表/下载排除 voided，审计查看按原对象及安全分类权限提供。

`role` 至少支持 `cover/photo/invoice/contract/acceptance/certificate/manual/maintenance/disposal/surplus_evidence/inventory_evidence/clearance/other`，并校验角色与目标匹配。`security_class in (A0,A1)`，对应权限文档普通/财务附件；invoice/contract 及包含财务金额的 acceptance/disposal 证据默认 A1，其他角色可由 finance 明确标记。A1 的创建、查看、下载、作废和导出都执行 finance/management 字段权限，不得只按业务对象权限放行。

上面是 V1.1 最终字段集合，不要求 Sprint 3 为尚不存在的模型创建假外键：Sprint 3 初建 AttachmentLink 及 `asset_id`；Sprint 7 以跟踪迁移增加 `asset_disposal_id`；Sprint 8 增加 inventory_surplus/scan/resolution；Sprint 9 增加 maintenance_record/maintenance_problem；Sprint 10 增加 clearance/clearance_item。每次迁移都建立真实 FK、公司一致性校验和目标唯一 CHECK 的更新版本，并验证从上一 Sprint 升级；禁止用裸整数、GenericForeignKey 或提前创建空壳业务模型绕过依赖。

这使尚无 `asset_id` 的盘盈记录可以先保存照片，保养、盘点扫描、处置和离职清理也能直接关联附件。业务目标被允许物理删除的仅限未确认草稿；删除 Link 后文件先进入孤儿保留队列，经过审计的清理任务才能删除存储对象。

## 8. 盘点

### 8.1 `InventoryTask`

字段：`id, company_id, task_code, name, inventory_type, scope_type, scope_department_id, scope_location_id, scope_category_id, scope_definition_json, planned_start, planned_end, remark, snapshot_at, expected_asset_count, status, idempotency_key, created_by_id, created_at, scanning_stopped_by_id, scanning_stopped_at, closed_by_id, closed_at, cancelled_by_id, cancelled_at, cancellation_reason`。

公司和范围主数据 `PROTECT`，用户 `SET_NULL`。`UNIQUE(company_id,task_code)`、`UNIQUE(company_id,idempotency_key)`；类型 `department/full/special`；scope 支持 company/department/category/location/selected_assets，字段组合以 CHECK 约束；`scope_definition_json` 只保存发布时的筛选/所选 ID 快照，不代替 `InventoryTaskAsset`。状态 `draft/in_progress/reconciliation/closed/cancelled`。进入 in_progress 时一次性固化范围、执行人、快照与 `expected_asset_count`；进入 reconciliation 后停止扫码并允许处理差异，快照始终不可改。cancelled 时取消人、时间和原因必填；取消不删除执行人、快照、扫描、结论或盘盈。

### 8.2 `InventoryTaskAssignee`

字段：`id, company_id, inventory_task_id, user_id, assigned_by_id, assigned_at`。

任务 `PROTECT`，执行用户 `PROTECT`，分配人 `SET_NULL`；`UNIQUE(inventory_task_id,user_id)`。发布前可随 draft 编辑，发布后不可删除或替换；执行用户必须启用并拥有权限矩阵允许的至少一个执行角色。任务指派只授予该任务的非财务快照/扫码权限，不扩大资产总账或其他任务范围；角色或账号被撤销后立即失去执行能力，但指派历史保留。

### 8.3 `InventoryTaskAsset`

字段：`id, company_id, inventory_task_id, asset_id, expected_department_id, expected_employee_id, expected_location_id, expected_asset_status, expected_code_snapshot, expected_name_snapshot, expected_category_snapshot, expected_department_snapshot, expected_employee_snapshot, expected_location_path_snapshot, inventory_status`。

所有外键 `PROTECT`；`UNIQUE(inventory_task_id,asset_id)`。`inventory_status in (pending,normal,exception,missing,resolved)`。snapshot 文本保存任务发布时的代码、名称和完整位置路径，即使后续主数据改名也不改变原任务显示；外键用于受控差异处理。所有快照字段在发布后不可变，后续调拨不得回写；`inventory_status` 只能由扫码、停止扫码和差异处理 Service 按证据更新，是可重建状态缓存。

### 8.4 `InventoryScan`

字段：`id, company_id, inventory_task_id, task_asset_id, asset_id, scan_mode, supplement_reason, scanned_by_id, scanned_at, actual_location_id, actual_employee_id, actual_status, result, note, is_effective, supersedes_scan_id, idempotency_key`。

`inventory_task/task_asset/asset -> PROTECT`；`scanned_by -> User SET_NULL`；actual location/employee 均 `PROTECT, nullable`；`supersedes_scan -> InventoryScan PROTECT, nullable`。`UNIQUE(company_id,idempotency_key)`；`scan_mode in (normal,supplemental)`；`result in (normal,location_mismatch,responsible_mismatch,status_mismatch,multiple_mismatch,other_mismatch)`；资产必须在快照中且公司一致。

位置、责任人、状态三维差异必须由 actual 字段与 TaskAsset snapshot 独立派生，不能由用户任意选一个后丢掉其他差异：三维均相同且无其他说明才是 normal；恰有一个维度不同使用对应单项枚举；两个或三个维度同时不同使用 `multiple_mismatch`，页面/报表仍逐维显示全部 before/actual；无法由三维表达的异常才使用 `other_mismatch` 并强制 note。后端重算 result，前端选择不得覆盖该规则。

normal 只允许任务为 in_progress，supplement_reason 为空，扫码人必须是当前有效 InventoryTaskAssignee 或矩阵允许的全域执行角色。supplemental 只允许任务为 reconciliation，由具备该任务差异处理权限的 finance/equipment/范围内 department_manager 显式发起，必须重新扫描当前有效 QR 并填写 supplement_reason；任务保持 reconciliation，普通 Assignee 不因指派获得补盘权。每次重扫/补盘都新增事件，旧行保留并改为 is_effective=false，新行指向 supersedes；部分唯一索引保证每个 task_asset 只有一条有效结果。替换在锁定 task/task_asset 的事务中完成，重算 normal/exception 状态并写 AuditLog；补盘变为异常后仍需 InventoryResolution，正常则以补盘 Scan 为完成证据。

### 8.5 `InventoryResolution`

字段：`id, company_id, inventory_task_asset_id, resolution_type, conclusion, movement_id, status, supersedes_resolution_id, correction_reason, idempotency_key, resolved_by_id, resolved_at, created_at`。

快照行、Movement、前一结论均 `PROTECT`，处理人 `SET_NULL`。约束：

- `resolution_type in (master_updated, master_confirmed, loss_confirmed, other)`；`status in (active,superseded)`。
- `UNIQUE(company_id,idempotency_key)`；每个 task_asset 最多一个 active 结论（部分唯一索引）。
- `supersedes_resolution_id` 非空时 `correction_reason` 必填；普通首次结论该字段为空。
- `master_updated` 必须引用由 Sprint 7 正式 Service 生成的同资产 Movement；其他类型不得伪造 Movement。
- 异常或 missing 快照行关闭前必须有 active 结论；normal 行不需要虚构处理记录。
- 关闭后原结论不可编辑/删除；批准纠错新增一条带原因、指向原结论的记录并保留完整链。附件通过 `AttachmentLink.inventory_resolution_id` 关联。

### 8.6 `InventorySurplus`

字段：`id, company_id, inventory_task_id, temporary_name, temporary_category_text, temporary_location_text, found_by_id, found_at, resolution_status, linked_asset_id, resolved_by_id, resolved_at, remark, idempotency_key`。

任务 `PROTECT`；linked_asset `PROTECT/NULL`；用户 `SET_NULL`。`UNIQUE(company_id,idempotency_key)`；状态 `pending/converted_to_draft/not_company/duplicate/other`，除 pending 外均要求处理人、时间和说明。盘盈照片通过 `AttachmentLink.inventory_surplus_id` 关联，不伪造 asset_id。转建档事务只创建一个 Asset 草稿并写回 linked_asset；重试不得重复创建。

## 9. 预防性保养

### 9.1 `MaintenancePlan`

字段：`id, company_id, asset_id, name, cycle_value, cycle_unit, advance_notice_days, responsible_employee_id, standard_content, first_due_date, last_maintenance_date, next_maintenance_date, status, ended_reason, ended_by_disposal_id, status_before_disposal, ended_at, created_at, updated_at`。

资产/责任人 `PROTECT`；`ended_by_disposal -> AssetDisposal PROTECT/NULL`。约束：`cycle_value>0`、提醒天数 `>=0`、`first_due_date` 必填、所有引用同公司；新建/改派责任人必须满足 `employment_status='active' AND is_active=true`；`status in (active,suspended,ended)`；`ended_reason in (manual,asset_disposal,other)`；`status_before_disposal in (active,suspended)`；V1 `cycle_unit in (day,week,month,year)`。

- 正常 `status in (active,suspended)` 时 ended_reason、ended_by_disposal、status_before_disposal、ended_at 必须全部为空。普通手工终止要求 `status=ended`、非空 ended_reason/ended_at，且 `ended_by_disposal`、`status_before_disposal` 为空。
- 处置完成只终止当时为 active/suspended 的计划：同一事务保存原状态到 `status_before_disposal`，设置 `status=ended`、`ended_reason=asset_disposal`、`ended_by_disposal` 和 `ended_at`。已经手工 ended 的计划不覆盖。
- 处置冲销时，只恢复 `ended_by_disposal` 正好指向该被冲销处置的计划；先读取 `status_before_disposal`，将 status 恢复为该值，再清空 ended_reason、ended_by_disposal、status_before_disposal、ended_at。自动终止/恢复的永久历史由 AssetDisposal、AssetDisposalReversal、AssetMovement 和 AuditLog 保留；这些字段只表达当前自动终止状态，清空后同一计划才能安全参与以后新的处置。其他组合由数据库约束触发器/Service 拒绝。
- MaintenancePlan 在 Sprint 9 建立，因此 Sprint 9 必须把上述终止/恢复逻辑接入 Sprint 7 既有的处置完成和处置冲销领域 Service，并补回归测试；不得要求 Sprint 7 提前创建未来模型，也不得只在页面隐藏计划。

尚无 confirmed Record 时 `next_maintenance_date=first_due_date`；此后只从最近未作废 confirmed Record 的实际完成日按日历规则计算。`runtime_hour` 延后至 V2，V1 不显示无法维护的运行小时选项。

### 9.2 `MaintenanceRecord`

字段：`id, company_id, maintenance_plan_id, asset_id, scheduled_date, completed_date, completed_by_id, content_snapshot, result, status, void_reason, voided_by_id, voided_at, remark, idempotency_key, created_at`。

计划/资产 `PROTECT`，员工和作废人 `SET_NULL`；`UNIQUE(company_id,idempotency_key)`；PostgreSQL 部分唯一索引保证每个 `(maintenance_plan_id,scheduled_date)` 最多一条 `status='confirmed'` 记录，作废后才允许重建；`status in (confirmed,voided)`、`result in (normal,problem_found)`，且作废字段与状态匹配；完成日期不早于合理业务边界。result=problem_found 时必须在同一事务创建恰好一条 open MaintenanceProblem，normal 时不得创建问题；使用延迟约束触发器或等效 Service + 数据库唯一约束保证。记录确认、更新计划最近/下次日期和发现问题在同一事务；作废后按未作废记录重算计划日期，不删除证据。完成照片/附件通过 `AttachmentLink.maintenance_record_id` 关联。

来源 Record 被作废时，其 Problem 和附件仍原样保留作历史证据，但从“当前 open 问题”、待办和可关闭集合中派生失效；UI 显示“来源保养记录已作废”，不得伪造 closure_note/closed_at，也不得继续关闭。重建 Record 如再次 problem_found，创建属于新 Record 的新 Problem。所有当前问题查询必须同时要求 `MaintenanceProblem.status='open' AND MaintenanceRecord.status='confirmed'`。

### 9.3 `MaintenanceProblem`

字段：`id, company_id, maintenance_record_id, asset_id, description, status, owner_employee_id, target_date, closed_by_id, closed_at, closure_note, created_at`。

保养记录/资产 `PROTECT`，员工/用户 `SET_NULL`。约束：`UNIQUE(maintenance_record_id)`；description 非空；`status in (open,closed)`；发现人和发现日从不可变的 `MaintenanceRecord.completed_by/completed_date` 取得，不重复存列。owner_employee/target_date 为可空的跟进信息，填写时员工必须同公司且 target_date 不得早于该 completed_date；closed 时关闭人、时间和说明必填，open 时三项关闭字段为空；公司一致。V1 不收集 severity，也不扩展为维修工单。跟进证据通过 `AttachmentLink.maintenance_problem_id` 关联并沿用该问题及资产的数据范围；源 Record 作废时按 9.2 节派生为历史失效。

## 10. 处置

### 10.1 `AssetDisposal`

字段：

`id, company_id, asset_id, disposal_type, application_date, planned_disposal_date, actual_disposal_date, reason, description, recipient_name, disposal_income, original_cost_snapshot, actual_accumulated_depreciation_snapshot, impairment_snapshot, book_value_snapshot, previous_asset_status, status, initiated_by_id, finance_locked_by_id, finance_locked_at, handled_by_id, confirmed_by_id, confirmed_at, cancelled_by_id, cancelled_at, cancellation_reason, idempotency_key, created_at`。

资产 `PROTECT`；用户 `SET_NULL`。约束：

- 一项资产最多一个未冲销的 confirmed disposal（部分唯一索引）。
- `disposal_type in (scrap,sale,other)`；`status in (draft,finance_locked,confirmed,cancelled,reversed)`；cancelled 时取消人、时间、原因必填且 confirmed 字段为空。
- 发起 draft 时 `application_date`、`planned_disposal_date` 必填且计划日不得早于申请日，`actual_disposal_date` 为空，资产进入 pending_disposal。计划日只用于安排，永不作为处置财务快照或终态报表的实际日期。
- 实物处置发生后，由授权经办人在财务锁定前填写 `actual_disposal_date`；实际日不得早于 application_date，也不得为未来上海业务日。财务执行 finance_locked 时要求实际日已存在，并以该实际日为唯一截止日，从截至该日的 confirmed 权威记录生成原值、累计折旧、减值、净值和收入快照。实际日和快照锁定后均不可普通修改。
- 若 stop_rule 表示实际处置日之前/所在期间仍应计提折旧，而相应折旧尚未确认，finance_locked 必须阻断并列出缺失期间；不得用理论折旧补快照，也不得先锁旧净值再由后续批次改变处置日实际值。
- 金额非负且快照满足 `book_value = original_cost - actual_accumulated_depreciation - impairment`（允许 0.01 尾差按规范校正）。`controlled_non_fixed` 的折旧和减值均为 0，但仍使用必填 original_cost 形成完整快照。
- 设备/经办完成记录后，由有权限的设备/财务操作把 finance_locked 记录变为 confirmed，并写资产终态、历史和审计；confirmed 时 actual_disposal_date、经办人、证据及对应类型必填字段必须完整。这是职责分步，不是 V1 审批流。
- 未完成取消把状态置为 cancelled 并恢复 `previous_asset_status`，不得删除已填写快照或附件。
- 终态错误通过一对一 `AssetDisposalReversal` 恢复 `previous_asset_status`，并把原处置标为 reversed；planned/actual 两个日期、快照和证据全部保留，不得复用取消字段或删除原记录。

附件通过 `AttachmentLink.asset_disposal_id` 关联。

### 10.2 `AssetDisposalReversal`

字段：`id, company_id, asset_disposal_id, reason, restored_asset_status, idempotency_key, reversed_by_id, reversed_at, created_at`。

原处置 `OneToOne PROTECT`，用户 `SET_NULL`；`UNIQUE(company_id,idempotency_key)`。只能引用同公司、`status='confirmed'` 的原处置，且必须确认不存在后续冲突业务记录。终态 Asset 保留的最后部门/责任人/位置只是历史显示；冲销恢复非终态时，原责任员工必须满足 `employment_status='active' AND is_active=true`，否则 finance 必须在同一受控动作指定同公司可接收资产的替代责任人并由 Movement 保存前后值，不能把资产恢复给 leaving/resigned/停用员工。创建 reversal、把原处置标为 reversed、恢复 Asset、按 DepreciationProfileEvent/MaintenancePlan 结构化来源恢复允许资格、写 Movement/AuditLog 必须在同一事务完成。冲销记录与原处置均不可编辑或删除。

Sprint 10 建立 ClearanceItem 后，任何 `resolution='disposed'` 的 Item 引用了本处置，就属于后续冲突业务记录；V1 阻断该处置冲销，避免改写已完成清退证据。需要同时纠正清退与处置时必须另行批准数据更正方案，不得静默把 completed Item 改回 pending。

## 11. 离职资产清理

### 11.1 `EmployeeAssetClearance`

字段：`id, company_id, employee_id, supplements_clearance_id, supplement_reason, initiated_at, initiated_by_id, total_assets_snapshot, unresolved_assets, status, completed_at, completed_by_id, remark, idempotency_key`。

员工、被补充清退单 `PROTECT`，用户 `SET_NULL`；`UNIQUE(company_id,idempotency_key)`；状态 `open/blocked/completed/cancelled`。PostgreSQL 部分唯一索引 `UNIQUE(company_id, employee_id) WHERE status IN ('open','blocked')`，保证同一员工最多一个活动清理流程；blocked 仍是活动且未完成的清理单，重复发起必须返回原单，不能另建 open 记录。

普通首次清退 `supplements_clearance_id/supplement_reason` 均为空，由 Employee active→leaving 原子创建。之后对已完成清退发现遗漏资产时，不修改或重新打开原记录；仅 hr 可带原因新建一张补充清退，`supplements_clearance_id` 必须指向同公司、同员工的 completed 清退，员工保持 resigned 且已有 termination_date。补充清退只收纳后补异常资产，按同一活跃唯一、解决和审计规则处理；完成补充清退不重复改人员状态或清空原离职日期。status=completed 时 completed_by/at 必须同时存在，open/blocked 时为空。

首次清退完成 Service 必须接收 HR 明确填写的 `termination_date`（不早于 hire_date、不晚于当前上海业务日），在同一事务重算 unresolved=0、把 Employee leaving→resigned、保存 termination_date 并完成 Clearance；不得从服务器日期静默猜值。补充清退完成时 Employee 已为 resigned，只校验并保留原 termination_date，不再次改写。

### 11.2 `EmployeeAssetClearanceItem`

字段：`id, company_id, clearance_id, asset_id, source_type, source_loan_id, association_effective_at, discovered_at, addition_reason, asset_code_snapshot, asset_name_snapshot, original_department_id, original_employee_id, original_location_id, original_department_snapshot, original_employee_snapshot, original_location_path_snapshot, original_status, added_during_clearance, resolution, resolved_by_id, resolved_at, movement_id, disposal_id, remark`。

清理单、资产、来源 Loan、原部门/人员/位置、变动记录和处置记录 `PROTECT`，用户 `SET_NULL`；`UNIQUE(clearance_id,asset_id)`；来源、时间及 snapshot 字段创建后不可改。

`source_type in (responsibility,internal_loan,both)`。internal_loan/both 时 `source_loan_id` 必须引用发起时或补充发现时 `borrower_employee_id` 为该离职员工的同资产内部 Loan；responsibility 时 source_loan 为空。`association_effective_at` 保存所有纳入来源中最晚的关系生效时间，初始责任关系使用建立该当前归属的正式 Movement/贴标启用时间，内部借用使用 loan_date；这使 Service 能证明所有纳入关系都不晚于 clearance.initiated_at，而不是按姓名或当前页面状态猜测。

`discovered_at` 必填。首次清退初始批次 `added_during_clearance=false`、addition_reason 为空；活动清退后补发现时必须 `added_during_clearance=true`、`association_effective_at <= clearance.initiated_at`、`discovered_at > clearance.initiated_at` 且 addition_reason 非空。补充清退中的异常资产以该新单发起时快照为初始 Item，并在 Clearance.supplement_reason 解释来源。发起后的普通新关系一律拒绝，不能借刷新创建 Item。returned/transferred Item 完成前必须重新确认该员工既不再是当前责任人，也不存在以其为 borrower_employee 的 active Loan；同时命中 both 时只解决其中一个关系不得标为完成。

`resolution in (pending,disposal_in_progress,returned,transferred,disposed)`。returned/transferred 必须引用同资产 Movement；disposal_in_progress/disposed 必须引用同资产 Disposal。发起处置只进入 `disposal_in_progress` 且仍计入 unresolved；处置 confirmed 终态后改为 disposed 并解决，处置取消则回到 pending。disposed 是“员工不再承担活动责任/借用”的明确终态例外：Asset 可保留最后责任人/部门/位置供历史显示，但终态下这些值不是活动领用关系，且借出资产本就不得处置。完成清理前 unresolved 必须从非终结 Item 重算为 0。附件可关联清理单或明细。

## 12. 导入、导出和设置

### 12.1 `ImportBatch` / `ImportRow`

Batch：`id, company_id, import_type, template_version, file_attachment_id, file_sha256, status, total_rows, valid_rows, error_rows, warning_rows, request_hash, idempotency_key, uploaded_by_id, uploaded_at, validated_at, confirmed_by_id, confirmed_at`；`UNIQUE(company_id,idempotency_key)`；`status in (uploaded,validated,invalid,confirmed,failed)`。

Row：`id, batch_id, row_number, raw_data_json, normalized_data_json, validation_status, errors_json, warnings_json, created_object_type, created_object_id`；`UNIQUE(batch_id,row_number)`；`validation_status in (pending,valid,invalid,created)`，invalid 时 errors 非空，created 只允许确认成功后；errors/warnings 均为结构化数组，至少保存字段、原值和原因，不以普通日志代替。

附件 `PROTECT`，Batch 删除仅限未确认草稿并级联 Row。`template_version` 和 64 位十六进制 `file_sha256` 上传后不可改，且 file_sha256 必须等于所引用 Attachment 的摘要，避免验证/确认期间换文件。

计数在 uploaded 时允许为空；完成验证后四个计数均非负，`total_rows = valid_rows + error_rows`，`warning_rows <= total_rows`（警告行可与 valid/error 行重叠，不能再加到 total）。`status=validated` 要求 error_rows=0、validated_at 非空；`status=invalid` 要求 error_rows>0、validated_at 非空；`status=confirmed` 只能从 validated 进入并要求确认人/时间；解析或确认的不可恢复失败进入 failed，不能伪装为 invalid 数据行。

确认采用 all-or-nothing 事务；相同批次重试不得重复建 Asset 草稿。`created_object_type/id` 是结果定位快照，不作为业务外键；各 import_type 的 Service 同时保存相应真实对象映射或审计事件。批次状态、行状态、创建映射、计数和 AuditLog 必须在对应验证/确认事务中同步，不能出现 confirmed 但行仍 valid、或计数与 Row 不一致。

### 12.2 `ExportLog` / `ExportLogTotal`

Log 字段：`id, company_id, export_type, filters_json, data_snapshot_at, row_count, output_attachment_id, output_sha256, totals_schema_version, request_hash, idempotency_key, requested_by_id, requested_at, completed_at, status, error_summary`。公司/附件 `PROTECT`、用户 `SET_NULL`；`UNIQUE(company_id,idempotency_key)`；`status in (pending,completed,failed,expired)`。completed 时输出附件、摘要、行数、快照/完成时间、合计 schema 版本及该 export_type 要求的全部 Total 行必填；子表完整性使用可延迟 constraint trigger + 唯一 Service 验证，不能假装用普通单表 CHECK；failed 不得关联可下载半文件。

Total 字段：`id, company_id, export_log_id, metric_key, amount, currency`；Log `CASCADE`（父 Log 受状态保护），公司 `PROTECT`；金额 `NUMERIC(18,2)`，`currency='CNY'`，`UNIQUE(export_log_id,metric_key)`，公司一致。metric_key 必须来自服务端按 export_type/version 固定的 registry，禁止自由文本扩展或把关键金额塞入 filters/JSON。

T+ 对账 V1.1 的 schema 至少逐项保存 `original_cost/opening_accumulated_depreciation/automatic_depreciation/manual_depreciation/adjustment_net/reversal_net/ending_accumulated_depreciation/impairment/ending_book_value/disposal_income`，均保留代数符号并与工作簿、明细精确勾稽。Log、全部 Total、输出附件摘要和 completed 状态在同一发布事务内形成；导出只读，且不得发起任何 T+ 写操作。

### 12.3 `SystemSetting`

字段：`id, company_id, key, value, value_type, description, updated_by_id, updated_at`；`UNIQUE(company_id,key)`，`value_type in (integer,decimal,string_list)`。value 保存类型化 Service 规范化后的文本（string_list 使用无重复的规范 JSON 字符串数组），读取时必须按登记类型解析，禁止调用方自行猜类型。

V1 只允许以下固定 registry；未知 key、错误 value_type、越权角色或越界值均由后端拒绝，并以 key/type 条件 CHECK 或等效数据库约束兜底：

| key | value_type | 写角色 | V1 校验与默认 |
|---|---|---|---|
| `attachment_allowed_extensions` | string_list | system_admin | 默认 `jpg/jpeg/png/webp/pdf/xlsx/docx`；只能从部署批准的非宏安全集合中取小写扩展名且至少一项 |
| `attachment_max_size_bytes` | integer | system_admin | `1..20971520`，默认 `20971520`（20 MiB） |
| `fixed_asset_warning_amount` | decimal | finance | Decimal、`>=0`，默认 `5000.00` CNY；只产生认定提示 |

敏感 Secret 永不存此表。Company.currency/timezone 是币种和业务时区唯一来源，不得另建 `currency/business_timezone` key；残值、寿命、方法、起止和期间默认的唯一配置来源是版本化 DepreciationPolicy（寿命可按上文读取 FixedAssetCategory 默认），不得另建 `default_salvage_rate` 等重复 key。description 由 registry 提供而非用户任意改写。设置变更必须类型校验、按角色审计，不追溯改写历史确认快照。

### 12.4 `InitializationSetting`

字段：`id, company_id, initialization_completed, company_configured, departments_configured, employees_configured, categories_configured, locations_configured, coding_scheme_configured, finance_rules_configured, permissions_configured, users_configured, completed_by_id, completed_at`；公司 OneToOne `PROTECT`，用户 `SET_NULL`。九步向导的每一步都有对应可验证状态；标志必须由实际数据校验后写入，不能只因访问页面置为 true。完成前后转换需事务和审计；完成后不能通过普通 UI 退回未初始化。

## 13. 审计与幂等

### 13.1 `AuditLog`

字段：`id, company_id (NULL allowed only for pre-initialization system events), user_id, action, object_type, object_id, old_data_json, new_data_json, ip_address, user_agent, correlation_id, created_at`。

公司 `PROTECT/NULL`、User `SET_NULL`。`company_id` 仅允许 Sprint 0 在 Company 建立之前产生的登录、安全或首批账号引导等系统级事件为空；Company 建立后，所有业务事件及所有可关联当前公司的系统事件都必须写当前公司，统一审计 Service 必须拒绝缺失 company 的业务事件。不得为绕过公司隔离把普通业务审计写成 NULL。

`object_type/object_id` 是跨模型不可变描述，不用假外键；old/new 保存业务快照并排除密码、Secret、Token、会话和文件内容。应用只追加，禁止普通 UPDATE/DELETE。关键业务与审计在同一事务提交。

### 13.2 `IdempotencyRecord`

除模型自带唯一幂等键外，跨步骤服务可使用：`id, company_id, operation, idempotency_key, request_hash, status, result_type, result_id, created_by_id, created_at, completed_at`。

约束：`UNIQUE(company_id,operation,idempotency_key)`。相同 key + 相同 request_hash 返回原结果；相同 key + 不同 hash 报冲突。状态 `processing/succeeded/failed`；失败是否允许重试由具体 Service 明确，不能产生半笔业务。

## 14. 必须原子化的业务事务

以下操作必须由 Service 层使用 `transaction.atomic()`，锁定主对象，并在同一事务写审计：

| 操作 | 同一事务内必须完成 |
|---|---|
| 正式发号/更正 | 资产锁、计数器、IssuedCode、当前绑定、CodeHistory；更正时同时撤销旧 QR、创建新 QR/待打印状态；AuditLog |
| 财务确认 | Asset/Finance/Profile 校验、Counter、IssuedCode、Asset.asset_code/当前绑定、CodeHistory、QR 身份、状态转换、opening 分录、AuditLog |
| QR 换标 | 锁资产、撤销旧身份、建立新身份、标签状态、AuditLog |
| 折旧批次确认 | 批次/资产锁、所有 BatchItem、实际 Entry、汇总校验、AuditLog |
| 折旧批次冲销 | 反向批次、逐行反向 Entry、原批次状态、AuditLog |
| 调拨/借还 | 资产主档、结构化 Loan（含内部 borrower_employee）、Movement、必要的清理项、AuditLog |
| 盘点开盘 | Task 状态和完整 TaskAsset 快照 |
| 盘盈转草稿 | Surplus 锁、单一 Asset 草稿、关联附件/结果、AuditLog |
| 保养完成 | Record、计划最近/下次日期、附件关联、AuditLog |
| 处置/处置冲销 | planned/actual 日期校验、截至 actual 日的确认数据、财务快照、Disposal、资产状态、DepreciationProfileEvent 结构化停止/恢复、MaintenancePlan 自动终止/恢复（Sprint 9 接入）、历史、AuditLog |
| 离职清理完成 | 所有 Item、未处理数、Clearance 状态、Employee 状态与 termination_date 校验 |
| 导入验证/确认 | 文件/模板摘要、全部 Row 错误警告与计数；确认时对象创建、映射、批次/行状态和 AuditLog；默认全有或全无 |
| 导出发布 | 一致性数据快照、ExportLog、全部 Decimal ExportLogTotal、受保护输出附件/摘要、completed 状态和 AuditLog |

外部文件写入不能与数据库真正形成同一事务：先写不可公开的临时对象，数据库提交后原子改名/标记可用；失败清理由幂等任务完成。绝不先返回成功再补业务审计。

## 15. 关键数据库验收

至少以 PostgreSQL 自动测试验证：

1. 跨公司 Department/Employee/Location/Category/Asset/Finance 引用均失败。
2. 正式在用资产缺部门、责任人或叶级位置无法保存；quantity 不等于 1 失败。
3. 物理分类和会计分类可独立组合，不互相推断。
4. 已替换、作废的旧代码仍占用；首个 Counter 并发创建无重复。
5. QR public Token 唯一且高熵，完整值不进入日志；撤销 Token 不能访问，附件不能通过静态 URL 绕过权限。
6. 盘盈照片可在没有 Asset 时保存并在转草稿后保留。
7. 同一折旧来源不能产生两条实际分录；理论结果不进入实际累计。
8. 确认批次不可修改，冲销产生等额反向批次/分录且保留原记录。
9. 处置财务快照固定，处置冲销恢复原状态而不删记录。
10. 所有幂等操作重复提交只产生一个业务结果；key 参数冲突被拒绝。
11. PROTECT/RESTRICT、部分唯一索引、CHECK、exclusion/constraint trigger 在数据库层生效，而不是只测表单。
12. Employee 任职状态/启停组合、termination_date 和新业务候选谓词一致；User 停用不静默改 Employee。
13. AssetCustomField 精确类型、options 组合和值列映射在数据库/Service 生效。
14. Loan 的借出/归还 Movement 分别唯一且公司、资产、类型正确；多维盘点异常不会丢失任一维度。
15. 处置 stop/restore 事件按结构化来源一对一配对，人工 stop 不被误恢复；源保养记录作废的问题退出当前 open 查询但历史仍在。
16. completed ExportLog 必须有按 schema 注册的完整 Decimal Total 集，金额与输出明细勾稽。

## 16. 禁止实现

- 硬编码资产编号、5,000 阈值或 5% 残值率。
- 以 `MAX+1` 生成流水，或只依赖 `Asset` 当前编号防重用。
- 用 `AssetCategory.category_type` 混合实物类别和固定资产会计类别。
- 用 float 计算金额，直接修改已确认折旧或处置快照。
- 把理论折旧写入实际累计折旧。
- 使用无真实 FK 且无目标约束的通用附件关系。
- 物理删除正式资产、已发编号、确认批次、实际分录、处置和审计日志。
- 只做前端权限/公司过滤。
- 把附件放公开 static/media URL，或在二维码中嵌入敏感字段。
- V1 直接写 T+、自动制证或把外部卡片号当作 EAM-Lite 主键。
