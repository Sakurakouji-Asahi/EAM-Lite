# EAM-Lite V1.2 低值物品数据字典与状态机

## 1. 目的

本文固定低值物品扩展的字段名称、枚举值、约束和状态转换，避免 Codex 在不同 Sprint 中自行发明相近但不一致的字段。

业务含义以 `13-Low-Value-Goods-Requirements.md` 为准，技术实现以 `14-Low-Value-Goods-Technical-Design.md` 为准。

## 2. 命名约定

- Python/Django 字段使用英文 snake_case。
- 页面使用简体中文。
- 主键统一 UUID，除非现有被扩展模型已使用其他主键。
- 所有业务表显式保存 `company_id`。
- 外键删除规则优先 `PROTECT`；操作人外键允许 `SET_NULL`。
- 业务历史表不物理删除。
- `normalized_*` 字段使用现有 `normalize_identifier()`。
- 金额字段后缀统一：`_amount`；单价字段后缀统一：`_unit_cost`；数量字段后缀统一：`_quantity`。

## 3. 枚举

### 3.1 物品管理模式

| 值 | 中文 | 含义 |
|---|---|---|
| `consumable` | 低值易耗品 | 领用后通常消耗，不形成长期保管余额 |
| `durable_quantity` | 数量型低值耐用品 | 领用后形成保管余额，需要归还、转交或处置 |

不得增加 `serialized`。逐件物品使用现有 `Asset`。

### 3.2 单据类型

| 值 | 中文 | 库存作用 |
|---|---|---|
| `opening` | 期初入库 | 目标仓库增加 |
| `receipt` | 日常入库 | 目标仓库增加 |
| `issue` | 领用出库 | 来源仓库减少；耐用品建立保管 |
| `return` | 领用退回 | 目标仓库增加；耐用品减少保管 |
| `transfer` | 仓库调拨 | 来源减少、目标增加 |
| `count_adjustment` | 盘点调整 | 按行方向增加或减少 |
| `reversal` | 冲销 | 反向复制原单库存影响 |

### 3.3 单据状态

| 值 | 中文 | 可编辑 | 影响库存 |
|---|---|---:|---:|
| `draft` | 草稿 | 是 | 否 |
| `posted` | 已过账 | 否 | 是 |
| `reversed` | 已冲销 | 否 | 原流水保留，另有反向流水 |
| `cancelled` | 已取消 | 否 | 否 |

### 3.4 调整方向

| 值 | 中文 |
|---|---|
| `increase` | 盘盈/增加 |
| `decrease` | 盘亏/减少 |

### 3.5 保管状态

| 值 | 中文 | 条件 |
|---|---|---|
| `open` | 在管 | 当前数量 > 0 |
| `closed` | 已结清 | 当前数量 = 0 且当前金额 = 0 |

### 3.6 保管动作

| 值 | 中文 | from_custody | to_custody | 仓库流水 |
|---|---|---:|---:|---:|
| `issue` | 领用建立 | 空 | 有 | 领用出库 |
| `opening` | 期初建立 | 空 | 有 | 无 |
| `return` | 归还仓库 | 有 | 空 | 退回入库 |
| `transfer` | 责任转交 | 有 | 有 | 无 |
| `loss` | 报损 | 有 | 空 | 无 |
| `scrap` | 报废 | 有 | 空 | 无 |
| `correction` | 受控更正 | 按场景 | 按场景 | 按场景明确 |
| `reversal` | 冲销 | 与原动作相反 | 与原动作相反 | 视原动作 |

### 3.7 盘点域

| 值 | 中文 |
|---|---|
| `warehouse_stock` | 仓库库存盘点 |
| `custody` | 耐用品保管盘点 |

### 3.8 盘点状态

| 值 | 中文 |
|---|---|
| `draft` | 草稿 |
| `in_progress` | 进行中 |
| `reconciliation` | 差异处理中 |
| `closed` | 已关闭 |
| `cancelled` | 已取消 |

### 3.9 导入类型

| 值 | 中文 |
|---|---|
| `item_master` | 物品档案导入 |
| `opening_stock` | 仓库期初导入 |
| `opening_custody` | 耐用品期初保管导入 |

### 3.10 导入状态

| 值 | 中文 |
|---|---|
| `uploaded` | 已上传 |
| `validated` | 已校验 |
| `confirmed` | 已确认 |
| `failed` | 失败 |
| `cancelled` | 已取消 |

## 4. 模型字段

### 4.1 `SupplyCategory`

| 字段 | 类型 | 必填 | 规则 |
|---|---|---:|---|
| `id` | UUID | 是 | 主键 |
| `company` | FK Company | 是 | `PROTECT` |
| `code` | varchar(100) | 是 | 保存清理后的显示编码 |
| `normalized_code` | varchar(100) | 是 | 同公司唯一，只读 |
| `name` | varchar(200) | 是 | 去除首尾空格后非空 |
| `parent` | self FK | 否 | 同公司，不得循环 |
| `default_item_type` | enum | 否 | 两个批准值之一 |
| `is_active` | bool | 是 | 默认 True |
| `remark` | text | 否 | 纯文本 |
| `created_by` | FK User | 否 | `SET_NULL` |
| `updated_by` | FK User | 否 | `SET_NULL` |
| `created_at` | datetime | 是 | auto_now_add |
| `updated_at` | datetime | 是 | auto_now |

### 4.2 `SupplyWarehouse`

| 字段 | 类型 | 必填 | 规则 |
|---|---|---:|---|
| `id` | UUID | 是 | 主键 |
| `company` | FK Company | 是 | `PROTECT` |
| `code` | varchar(100) | 是 | 显示编码 |
| `normalized_code` | varchar(100) | 是 | 同公司唯一 |
| `name` | varchar(200) | 是 | 非空 |
| `location` | FK Location | 否 | 同公司、启用 |
| `manager_employee` | FK Employee | 否 | 同公司、在职、启用 |
| `is_active` | bool | 是 | 默认 True |
| `remark` | text | 否 |  |
| 审计字段 |  |  | 同上 |

### 4.3 `SupplyItem`

| 字段 | 类型 | 必填 | 规则 |
|---|---|---:|---|
| `id` | UUID | 是 | 主键 |
| `company` | FK Company | 是 | `PROTECT` |
| `item_code` | varchar(100) | 是 | 显示物品编码 |
| `normalized_item_code` | varchar(100) | 是 | 同公司唯一 |
| `name` | varchar(200) | 是 | 非空 |
| `category` | FK SupplyCategory | 是 | 同公司、启用 |
| `item_type` | enum | 是 | `consumable` / `durable_quantity` |
| `unit` | varchar(32) | 是 | 非空，如个、盒、卷、套 |
| `specification` | varchar(200) | 否 |  |
| `model` | varchar(100) | 否 |  |
| `brand` | varchar(100) | 否 |  |
| `minimum_stock_quantity` | decimal(18,4) | 是 | 默认 0，非负 |
| `default_warehouse` | FK SupplyWarehouse | 否 | 同公司、启用 |
| `is_active` | bool | 是 | 默认 True |
| `remark` | text | 否 |  |
| 审计字段 |  |  |  |

冻结规则：存在已过账业务后，`item_code`、`normalized_item_code`、`item_type` 不可改。

### 4.4 `SupplyDocumentSequence`

| 字段 | 类型 | 必填 | 规则 |
|---|---|---:|---|
| `company` | FK | 是 |  |
| `sequence_type` | varchar(32) | 是 | 单据类型 |
| `year` | smallint | 是 | 四位年份 |
| `current_value` | bigint | 是 | 非负 |
| `updated_at` | datetime | 是 |  |

唯一键：`company + sequence_type + year`。

### 4.5 `SupplyDocument`

| 字段 | 类型 | 必填 | 规则 |
|---|---|---:|---|
| `id` | UUID | 是 | 主键 |
| `company` | FK Company | 是 |  |
| `document_no` | varchar(64) | 是 | 同公司唯一 |
| `document_type` | enum | 是 | 固定枚举 |
| `business_date` | date | 是 | 上海业务日；允许补录历史日 |
| `source_warehouse` | FK Warehouse | 条件 | 类型决定 |
| `target_warehouse` | FK Warehouse | 条件 | 类型决定 |
| `department` | FK Department | 条件 | 领用必填 |
| `employee` | FK Employee | 否 | 与部门一致、在职 |
| `external_reference` | varchar(200) | 否 | 外部单号 |
| `counterparty_name` | varchar(200) | 否 | 纯文本来源单位 |
| `remark` | text | 否 |  |
| `status` | enum | 是 | 默认 draft |
| `idempotency_key` | varchar(128) | 是 | 同公司唯一 |
| `reversal_of` | self O2O | 条件 | 冲销单必填 |
| `source_count_task` | O2O CountTask | 条件 | 盘点调整单使用 |
| `created_by/at` |  |  |  |
| `posted_by/at` |  | 条件 | posted/reversed 原单有值 |
| `cancelled_by/at` |  | 条件 | cancelled 有值 |
| `cancellation_reason` | text | 条件 | cancelled 必填 |
| `reversed_by/at` |  | 条件 | reversed 原单有值 |

### 4.6 `SupplyDocumentLine`

| 字段 | 类型 | 必填 | 规则 |
|---|---|---:|---|
| `id` | UUID | 是 |  |
| `company` | FK Company | 是 | 与单据一致 |
| `document` | FK Document | 是 | `PROTECT` |
| `line_no` | int | 是 | 单据内唯一，从 1 开始 |
| `item` | FK Item | 是 | 同公司、启用或历史引用 |
| `quantity` | decimal(18,4) | 是 | > 0 |
| `entered_unit_cost` | decimal(18,6) | 条件 | 入库/期初/零库存盘盈需要 |
| `posted_unit_cost` | decimal(18,6) | 过账后 | 草稿为空 |
| `posted_amount` | decimal(18,2) | 过账后 | 草稿为空 |
| `adjustment_direction` | enum | 条件 | 仅盘点调整 |
| `source_issue_line` | self FK | 条件 | 退回必填 |
| `source_custody` | FK Custody | 条件 | 耐用品退回必填 |
| `line_remark` | text | 否 | 盘点差异原因等 |

### 4.7 `SupplyStockBalance`

| 字段 | 类型 | 必填 | 规则 |
|---|---|---:|---|
| `company` | FK | 是 |  |
| `warehouse` | FK | 是 |  |
| `item` | FK | 是 |  |
| `quantity` | decimal(18,4) | 是 | 非负 |
| `amount` | decimal(18,2) | 是 | 非负 |
| `average_unit_cost` | decimal(18,6) | 是 | 非负 |
| `updated_at` | datetime | 是 |  |

唯一键：`company + warehouse + item`。

一致性：数量为 0 时金额和平均单价必须为 0。

### 4.8 `SupplyStockLedger`

| 字段 | 类型 | 必填 | 规则 |
|---|---|---:|---|
| `id` | UUID | 是 |  |
| `company` | FK | 是 |  |
| `warehouse` | FK | 是 |  |
| `item` | FK | 是 |  |
| `document` | FK | 是 | 已过账单据 |
| `document_line` | FK | 是 |  |
| `movement_type` | enum | 是 |  |
| `quantity_delta` | decimal(18,4) | 是 | 非 0，可正负 |
| `amount_delta` | decimal(18,2) | 是 | 可正负 |
| `unit_cost` | decimal(18,6) | 是 | 非负 |
| `quantity_before` | decimal(18,4) | 是 | 非负 |
| `quantity_after` | decimal(18,4) | 是 | 非负 |
| `amount_before` | decimal(18,2) | 是 | 非负 |
| `amount_after` | decimal(18,2) | 是 | 非负 |
| `occurred_at` | datetime | 是 | 过账时间 |
| `created_by` | FK User | 否 |  |
| `reverses_ledger` | self O2O | 否 | 冲销流水使用 |

该表 append-only。QuerySet `update()` 和 `delete()` 必须拒绝。

### 4.9 `SupplyCustody`

| 字段 | 类型 | 必填 | 规则 |
|---|---|---:|---|
| `id` | UUID | 是 |  |
| `company` | FK | 是 |  |
| `item` | FK Item | 是 | 必须 durable_quantity |
| `origin_issue_line` | FK DocLine | 二选一 | 领用来源 |
| `origin_import_row` | FK ImportRow | 二选一 | 期初来源 |
| `parent_custody` | self FK | 否 | 转交来源链 |
| `department` | FK Department | 是 | 当前责任部门 |
| `employee` | FK Employee | 否 | 当前责任人 |
| `current_quantity` | decimal(18,4) | 是 | 非负 |
| `current_amount` | decimal(18,2) | 是 | 非负 |
| `unit_cost_snapshot` | decimal(18,6) | 是 | 非负 |
| `started_on` | date | 是 |  |
| `status` | enum | 是 | open / closed |
| `remark` | text | 否 |  |
| `created_at/updated_at` |  |  |  |

### 4.10 `SupplyCustodyMovement`

| 字段 | 类型 | 必填 | 规则 |
|---|---|---:|---|
| `id` | UUID | 是 |  |
| `company` | FK | 是 |  |
| `item` | FK | 是 | durable_quantity |
| `from_custody` | FK Custody | 条件 | 动作决定 |
| `to_custody` | FK Custody | 条件 | 动作决定 |
| `action` | enum | 是 |  |
| `quantity` | decimal(18,4) | 是 | > 0 |
| `amount` | decimal(18,2) | 是 | >= 0 |
| `unit_cost` | decimal(18,6) | 是 | >= 0 |
| `business_date` | date | 是 |  |
| `source_document_line` | FK DocLine | 否 | 领用/退回时使用 |
| `reason` | text | 条件 | 报损、报废、更正必填 |
| `created_by/at` |  |  |  |
| `reverses_movement` | self O2O | 否 |  |

append-only。

### 4.11 `SupplyCountTask`

| 字段 | 类型 | 必填 | 规则 |
|---|---|---:|---|
| `id` | UUID | 是 |  |
| `company` | FK | 是 |  |
| `task_no` | varchar(64) | 是 | 同公司唯一 |
| `name` | varchar(200) | 是 |  |
| `count_domain` | enum | 是 | warehouse_stock / custody |
| `warehouse` | FK | 条件 | 仓库盘点必填 |
| `department` | FK | 条件 | 保管盘点必填 |
| `employee` | FK | 否 | 保管盘点可选 |
| `planned_start/end` | date | 是 | end >= start |
| `snapshot_at` | datetime | 发布后 |  |
| `status` | enum | 是 |  |
| `idempotency_key` | varchar(128) | 是 | 同公司唯一 |
| 人员/时间字段 |  | 条件 | 与状态一致 |
| `remark` | text | 否 |  |

### 4.12 `SupplyCountLine`

| 字段 | 类型 | 必填 | 规则 |
|---|---|---:|---|
| `count_task` | FK | 是 |  |
| `item` | FK | 是 |  |
| `stock_balance` | FK | 条件 | 仓库盘点 |
| `custody` | FK | 条件 | 保管盘点 |
| `expected_quantity` | decimal(18,4) | 是 | 快照 |
| `expected_amount` | decimal(18,2) | 是 | 快照 |
| `counted_quantity` | decimal(18,4) | 录入后 | 非负 |
| `difference_quantity` | decimal(18,4) | 录入后 | counted - expected |
| `resolution_type` | varchar | 条件 | 差异处理 |
| 解决证据 FK |  | 条件 | 调整单行或保管流水 |
| `remark` | text | 否 | 差异时必填 |
| `counted_by/at` |  | 条件 |  |

### 4.13 `SupplyImportBatch` / `SupplyImportRow`

字段按技术设计。批次确认后状态不可返回。行级原始值和错误必须保留。

### 4.14 `EmployeeSupplyClearanceItem`

| 字段 | 类型 | 必填 | 规则 |
|---|---|---:|---|
| `clearance` | FK Existing Clearance | 是 |  |
| `company` | FK | 是 | 与清退单一致 |
| `custody` | FK | 是 | 快照来源 |
| `item_code_snapshot` | varchar | 是 |  |
| `item_name_snapshot` | varchar | 是 |  |
| `quantity_snapshot` | decimal(18,4) | 是 | > 0 |
| `amount_snapshot` | decimal(18,2) | 是 | >= 0 |
| `department_snapshot` | varchar | 是 |  |
| `employee_snapshot` | varchar | 是 |  |
| `resolution` | enum | 是 | pending/returned/transferred/lost/scrapped |
| `resolved_by/at` |  | 条件 | pending 时为空 |
| `custody_movement` | FK | 条件 | 已解决时必填 |
| `remark` | text | 否 |  |

唯一键：`clearance + custody`。

## 5. 单据字段矩阵

| 类型 | 来源仓库 | 目标仓库 | 部门 | 员工 | 录入单价 | 原领用行 | 原保管 | 调整方向 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| opening | 空 | 必填 | 空 | 空 | 必填 | 空 | 空 | 空 |
| receipt | 空 | 必填 | 空 | 空 | 必填 | 空 | 空 | 空 |
| issue | 必填 | 空 | 必填 | 可选 | 禁止 | 空 | 空 | 空 |
| return consumable | 空 | 必填 | 快照 | 可选 | 禁止 | 必填 | 空 | 空 |
| return durable | 空 | 必填 | 快照 | 可选 | 禁止 | 必填 | 必填 | 空 |
| transfer | 必填 | 必填 | 空 | 空 | 禁止 | 空 | 空 | 空 |
| count_adjustment | 按方向 | 按方向 | 空 | 空 | 条件 | 空 | 空 | 必填 |
| reversal | 系统生成 | 系统生成 | 系统生成 | 系统生成 | 系统生成 | 系统生成 | 系统生成 | 系统生成 |

## 6. 状态机

### 6.1 单据

```text
                cancel
DRAFT ----------------------> CANCELLED
  |
  | post
  v
POSTED ---------------------> REVERSED
              reverse
```

禁止：

- `posted -> draft`
- `reversed -> posted`
- `cancelled -> draft`
- 直接修改状态字段

### 6.2 保管

```text
ISSUE / OPENING
       |
       v
     OPEN ---- partial return/transfer/loss/scrap ---> OPEN
       |
       | current_quantity becomes 0
       v
     CLOSED
```

关闭后不可恢复；错误使用反向保管流水形成新开放记录，不直接改历史。

### 6.3 盘点

```text
DRAFT --publish--> IN_PROGRESS --stop--> RECONCILIATION --close--> CLOSED
  |                       |                       |
  +------cancel---------->+----------cancel------+----> CANCELLED
```

- 发布后快照行不可删除。
- 关闭后不可重新录入。
- 仓库盘点关闭和调整单过账必须同事务。

### 6.4 导入

```text
UPLOADED --validate--> VALIDATED --confirm--> CONFIRMED
    |                      |
    +------cancel--------->CANCELLED
    |
    +------parse error---->FAILED
```

确认失败事务回滚后，批次可保持 `validated` 并显示失败原因；不要一部分创建成功。

## 7. 核心数据库约束清单

至少实现：

1. 分类、仓库、物品的公司内规范化编码唯一。
2. 单据编号和幂等键公司内唯一。
3. 单据行号单据内唯一。
4. 余额 `(company, warehouse, item)` 唯一。
5. 数量和余额非负。
6. 数量为零时余额金额和平均成本为零。
7. 单据状态值合法。
8. 单据状态对应时间字段合法。
9. 来源仓库和目标仓库不同。
10. 库存流水唯一键阻止重复过账。
11. 原单和冲销单一对一。
12. 保管开放/关闭状态和余额一致。
13. 保管来源领用行和期初导入行二选一。
14. 清退单与保管记录唯一。
15. 盘点范围字段与盘点域一致。

复杂的跨行合计和同公司检查同时在 service 中完成；不要仅依赖表单。

## 8. 不可变字段

### 已过账 `SupplyDocument`

不可变：

- 单据类型；
- 业务日期；
- 仓库；
- 部门/员工；
- 明细；
- 过账金额；
- 外部参考号和备注。

更正方式：冲销后重做。

### `SupplyStockLedger` / `SupplyCustodyMovement`

全部业务字段不可变。只允许操作人账户删除时由 `SET_NULL` 清空操作人引用。

### 已发生业务的 `SupplyItem`

不可变：物品编码和管理模式。

## 9. 快照字段

历史记录不得依赖后来可能改名的主数据才能解释。以下场景保存快照：

- 单据行：过账物品编码、名称、单位可通过额外快照字段保存；最低要求报表导出能读取历史主数据，即使物品停用仍可访问。
- 保管记录：单位成本、金额和开始责任。
- 清退项目：物品编码、名称、部门、员工、数量和金额。
- 盘点行：应有数量和金额。

若 Codex选择增加 `*_snapshot` 字段，应一次性在对应 Sprint 文档和迁移中明确，不得每个页面重复临时拼接 JSON。

## 10. 错误信息基线

后端应返回可执行的中文错误，例如：

- `库存不足：A4复印纸在办公用品仓可用 3.0000 箱，本次领用 5.0000 箱。`
- `退回数量超过原领用未退数量。`
- `该物品已发生库存业务，不能修改管理模式；请停用后新建物品。`
- `责任员工已离职或不属于目标部门，不能接收耐用品。`
- `该单据已过账，不能编辑；请使用冲销。`
- `原领用单已发生后续转交，不能直接完整冲销。`
- `盘点差异尚未处理，不能关闭任务。`

不得只返回数据库异常、英文字段名或“操作失败”。
