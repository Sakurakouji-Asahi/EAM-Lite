# EAM-Lite V1.2 低值物品技术设计

## 1. 设计结论

### 1.1 不修改现有资产数量模型

现有 `Asset` 已通过模型和数据库约束固定为单件追踪、数量 `1`。低值物品扩展不得删除、放宽或绕过这些约束。

逐件低值耐用品继续使用：

```text
Asset
  └─ AssetFinance(accounting_treatment="controlled_non_fixed")
      ├─ 不创建 AssetDepreciationProfile
      ├─ 使用现有二维码、调拨、盘点、处置
      └─ 使用现有 EmployeeAssetClearance
```

数量型物品使用独立领域：

```text
apps.supplies
  ├─ 基础档案
  ├─ 库存单据
  ├─ 不可变库存流水
  ├─ 库存余额缓存
  ├─ 数量型耐用品保管与保管流水
  ├─ 盘点
  ├─ Excel 导入导出
  └─ 与现有离职清退、报表、首页集成
```

### 1.2 单体应用，不引入新架构

继续使用：

- Django models/forms/views/services；
- PostgreSQL；
- Django Templates + Bootstrap + HTMX；
- pytest + pytest-django；
- openpyxl；
- 现有角色、组织、审计和上下文处理器。

不得引入：

- 微服务；
- REST 前后端分离；
- React/Vue；
- Redis/Celery，仅为此模块增加异步基础设施；
- 双写外部库存系统；
- 通用工作流引擎。

## 2. 仓库改动范围

新增：

```text
apps/supplies/
  __init__.py
  apps.py
  models.py
  domain.py
  permissions.py
  services.py
  forms.py
  views.py
  urls.py
  context_processors.py
  excel.py
  migrations/
  management/commands/

templates/supplies/
  dashboard.html
  category_list.html
  category_form.html
  item_list.html
  item_form.html
  warehouse_list.html
  warehouse_form.html
  stock_balance_list.html
  stock_ledger_list.html
  document_list.html
  document_detail.html
  document_form.html
  document_post_confirm.html
  custody_list.html
  custody_detail.html
  custody_action_form.html
  count_task_list.html
  count_task_detail.html
  count_entry.html
  import_*.html
  report_*.html
```

修改：

```text
config/settings.py             # 注册 SuppliesConfig 和 context processor
config/urls.py                 # path("supplies/", include(...))
templates/_app_navigation.html # 增加低值物品导航
apps/core/views.py             # 首页卡片或入口，最后一个 Sprint 完成
apps/reports/...               # 只在 Sprint 18 做报表集成
apps/offboarding/...           # 只在 Sprint 17 做数量型耐用品清退集成
apps/finance/...               # Sprint 16 增加 controlled_non_fixed 防折旧回归校验/筛选
```

不建议把数量库存模型塞入现有 `apps.inventory`。现有 `inventory` 是逐件资产盘点域，数据模型直接引用 `Asset`，与数量库存的余额、成本和调整规则不同。

## 3. 模型总览

建议模型：

```text
SupplyCategory
SupplyWarehouse
SupplyItem
SupplyDocumentSequence
SupplyDocument
SupplyDocumentLine
SupplyStockBalance
SupplyStockLedger
SupplyCustody
SupplyCustodyMovement
SupplyCountTask
SupplyCountLine
SupplyImportBatch
SupplyImportRow
EmployeeSupplyClearanceItem
```

所有业务模型必须包含 `company` 外键，引用的分类、仓库、物品、部门、员工、位置和单据必须属于同一公司。

## 4. 通用类型和精度

在 `apps/supplies/models.py` 或 `domain.py` 统一定义：

```python
QUANTITY = {"max_digits": 18, "decimal_places": 4}
UNIT_COST = {"max_digits": 18, "decimal_places": 6}
MONEY = {"max_digits": 18, "decimal_places": 2}
```

业务计算：

```python
from decimal import Decimal, ROUND_HALF_UP

ZERO_QTY = Decimal("0.0000")
ZERO_COST = Decimal("0.000000")
ZERO_MONEY = Decimal("0.00")

QTY_QUANT = Decimal("0.0001")
COST_QUANT = Decimal("0.000001")
MONEY_QUANT = Decimal("0.01")
```

- 所有输入先转换为 `Decimal`。
- 数量按 4 位、单位成本按 6 位、金额按 2 位 `ROUND_HALF_UP`。
- 不使用 `float`。
- 数据库约束要求数量和金额非负；流水增减方向通过单独字段或 `quantity_delta` 表示。

## 5. 基础档案模型

### 5.1 `SupplyCategory`

建议字段：

```python
id: UUIDField(primary_key=True)
company: ForeignKey(Company, PROTECT)
code: CharField(max_length=100)
normalized_code: CharField(max_length=100, editable=False)
name: CharField(max_length=200)
parent: ForeignKey("self", null=True, blank=True, PROTECT)
default_item_type: CharField(
    choices=("consumable", "durable_quantity"),
    null=True,
    blank=True,
)
is_active: BooleanField(default=True)
remark: TextField(blank=True)
created_by / updated_by: ForeignKey(User, SET_NULL, null=True)
created_at / updated_at
```

实现要求：

- 复用 `apps.masterdata.normalization.clean_display_identifier` 和 `normalize_identifier`。
- 唯一约束：`(company, normalized_code)`。
- 检查约束：非空编码；`parent_id != id`；管理模式合法。
- `clean()` 检查同公司和树循环。
- 被引用后不得物理删除；提供停用服务。

### 5.2 `SupplyWarehouse`

```python
id: UUIDField
company: ForeignKey(Company, PROTECT)
code / normalized_code
name: CharField(max_length=200)
location: ForeignKey(Location, null=True, blank=True, PROTECT)
manager_employee: ForeignKey(Employee, null=True, blank=True, PROTECT)
is_active: BooleanField(default=True)
remark
created_by / updated_by / timestamps
```

约束：

- `(company, normalized_code)` 唯一。
- 位置和负责人同公司。
- 负责人必须在职且启用。
- 有余额、单据或流水时不允许物理删除。

### 5.3 `SupplyItem`

```python
class ItemType(models.TextChoices):
    CONSUMABLE = "consumable", "低值易耗品"
    DURABLE_QUANTITY = "durable_quantity", "数量型低值耐用品"

id: UUIDField
company: ForeignKey(Company, PROTECT)
item_code: CharField(max_length=100)
normalized_item_code: CharField(max_length=100, editable=False)
name: CharField(max_length=200)
category: ForeignKey(SupplyCategory, PROTECT)
item_type: CharField(max_length=32, choices=ItemType.choices)
unit: CharField(max_length=32)
specification: CharField(max_length=200, blank=True)
model: CharField(max_length=100, blank=True)
brand: CharField(max_length=100, blank=True)
minimum_stock_quantity: DecimalField(default=0, **QUANTITY)
default_warehouse: ForeignKey(SupplyWarehouse, null=True, blank=True, PROTECT)
is_active: BooleanField(default=True)
remark: TextField(blank=True)
created_by / updated_by / timestamps
```

约束：

- `(company, normalized_item_code)` 唯一。
- `minimum_stock_quantity >= 0`。
- `item_type` 仅允许两个批准值。
- 分类和默认仓库同公司且启用。
- 一旦存在已过账单据行、库存流水或保管流水，`item_code` 和 `item_type` 只能通过受控服务拒绝修改。
- 单位不能为空。

不在 `SupplyItem` 中增加 `serialized` 类型。需要逐件管理的物品直接进入现有 `Asset`，避免两个模块同时代表同一实物。

## 6. 单据模型

### 6.1 `SupplyDocumentSequence`

用于并发安全生成单据号。

```python
company
sequence_type: CharField(max_length=32)
year: PositiveSmallIntegerField
current_value: PositiveBigIntegerField(default=0)
updated_at
```

唯一约束：`(company, sequence_type, year)`。

生成规则：

```text
期初       QC-YYYY-000001
入库       RK-YYYY-000001
领用       LY-YYYY-000001
退回       TH-YYYY-000001
调拨       DB-YYYY-000001
盘点调整   PD-YYYY-000001
冲销       CX-YYYY-000001
```

编号只在首次保存单据草稿时生成；使用 `transaction.atomic()` + `select_for_update()` 更新序号，禁止 `MAX()+1`。

### 6.2 `SupplyDocument`

```python
class DocumentType(models.TextChoices):
    OPENING = "opening", "期初入库"
    RECEIPT = "receipt", "日常入库"
    ISSUE = "issue", "领用出库"
    RETURN = "return", "领用退回"
    TRANSFER = "transfer", "仓库调拨"
    COUNT_ADJUSTMENT = "count_adjustment", "盘点调整"
    REVERSAL = "reversal", "冲销"

class Status(models.TextChoices):
    DRAFT = "draft", "草稿"
    POSTED = "posted", "已过账"
    REVERSED = "reversed", "已冲销"
    CANCELLED = "cancelled", "已取消"

id: UUIDField
company: ForeignKey(Company, PROTECT)
document_no: CharField(max_length=64)
document_type: CharField(max_length=32)
business_date: DateField
source_warehouse: ForeignKey(SupplyWarehouse, null=True, blank=True, PROTECT)
target_warehouse: ForeignKey(SupplyWarehouse, null=True, blank=True, PROTECT)
department: ForeignKey(Department, null=True, blank=True, PROTECT)
employee: ForeignKey(Employee, null=True, blank=True, PROTECT)
external_reference: CharField(max_length=200, blank=True)
counterparty_name: CharField(max_length=200, blank=True)
remark: TextField(blank=True)
status: CharField(max_length=16, default="draft")
idempotency_key: CharField(max_length=128)
reversal_of: OneToOneField("self", null=True, blank=True, PROTECT, related_name="reversal_document")
source_count_task: OneToOneField(SupplyCountTask, null=True, blank=True, PROTECT)
created_by / created_at
posted_by / posted_at
cancelled_by / cancelled_at / cancellation_reason
reversed_by / reversed_at
```

主要约束：

- `(company, document_no)` 唯一。
- `(company, idempotency_key)` 唯一。
- `source_warehouse != target_warehouse`。
- `opening`、`receipt`：仅目标仓库必填。
- `issue`：仅来源仓库和部门必填。
- `return`：仅目标仓库必填，部门/员工从原领用/保管校验，可在头部保存快照。
- `transfer`：来源和目标仓库都必填。
- `count_adjustment`：一个来源盘点任务对应一张单。
- `reversal`：`reversal_of` 必填，且原单非冲销单。
- 状态时间字段与状态一致。

模型禁止普通 `save()` 修改状态、过账时间和冲销关系；状态转换只通过服务。

### 6.3 `SupplyDocumentLine`

```python
class AdjustmentDirection(models.TextChoices):
    INCREASE = "increase", "盘盈"
    DECREASE = "decrease", "盘亏"

id: UUIDField
company
document: ForeignKey(SupplyDocument, PROTECT, related_name="lines")
line_no: PositiveIntegerField
item: ForeignKey(SupplyItem, PROTECT)
quantity: DecimalField(**QUANTITY)
entered_unit_cost: DecimalField(null=True, blank=True, **UNIT_COST)
posted_unit_cost: DecimalField(null=True, blank=True, **UNIT_COST)
posted_amount: DecimalField(null=True, blank=True, **MONEY)
adjustment_direction: CharField(null=True, blank=True, choices=...)
source_issue_line: ForeignKey("self", null=True, blank=True, PROTECT, related_name="return_lines")
source_custody: ForeignKey(SupplyCustody, null=True, blank=True, PROTECT)
line_remark: TextField(blank=True)
```

约束：

- `(document, line_no)` 唯一。
- `quantity > 0`。
- 草稿行的 `posted_*` 为空；过账后必须有值。
- 期初/入库要求 `entered_unit_cost >= 0`。
- 领用/调拨的录入单价为空，由服务计算。
- 退回必须关联原领用行；耐用品退回还关联保管记录。
- 盘点调整必须填写 `adjustment_direction`。
- 非盘点调整行的 `adjustment_direction` 必须为空。
- 行的公司、物品公司和单据公司一致。

建议使用 FormSet 或独立 HTMX 行编辑，不引入通用前端表格框架。

## 7. 库存权威流水与余额

### 7.1 `SupplyStockBalance`

当前余额缓存：

```python
id: UUIDField
company
warehouse: ForeignKey(SupplyWarehouse, PROTECT)
item: ForeignKey(SupplyItem, PROTECT)
quantity: DecimalField(default=0, **QUANTITY)
amount: DecimalField(default=0, **MONEY)
average_unit_cost: DecimalField(default=0, **UNIT_COST)
updated_at
```

约束：

- `(company, warehouse, item)` 唯一。
- 数量和金额不得小于 0。
- `quantity == 0` 时 `amount == 0` 且 `average_unit_cost == 0`。
- 只允许库存服务更新，不提供普通 CRUD 页面。

### 7.2 `SupplyStockLedger`

不可变权威历史：

```python
class MovementType(models.TextChoices):
    OPENING_IN = "opening_in"
    RECEIPT_IN = "receipt_in"
    ISSUE_OUT = "issue_out"
    RETURN_IN = "return_in"
    TRANSFER_OUT = "transfer_out"
    TRANSFER_IN = "transfer_in"
    COUNT_GAIN = "count_gain"
    COUNT_LOSS = "count_loss"
    REVERSAL = "reversal"

id: UUIDField
company
warehouse
item
document: ForeignKey(SupplyDocument, PROTECT)
document_line: ForeignKey(SupplyDocumentLine, PROTECT)
movement_type
quantity_delta: DecimalField(**QUANTITY)  # 可正可负，不能为 0
amount_delta: DecimalField(**MONEY)        # 可正可负
unit_cost: DecimalField(**UNIT_COST)
quantity_before / quantity_after
amount_before / amount_after
occurred_at
created_by: ForeignKey(User, SET_NULL, null=True)
reverses_ledger: OneToOneField("self", null=True, blank=True, PROTECT)
```

约束：

- 每个过账业务行在一个仓库中最多产生一条对应方向流水；调拨行产生来源和目标各一条。
- 可使用唯一键 `(document_line, warehouse, movement_type)` 防止重复过账。
- `quantity_after = quantity_before + quantity_delta` 在服务和测试中验证；数据库可增加等式 CheckConstraint，但考虑 Decimal 表达兼容性，也可由 service + reconciliation command 保证。
- `quantity_delta != 0`。
- 不允许更新或删除；冲销创建新流水。

### 7.3 余额重建

提供管理命令：

```text
python manage.py reconcile_supply_balances --company <code>
python manage.py rebuild_supply_balances --company <code> --confirm
```

- `reconcile` 只读比较流水汇总和余额缓存。
- `rebuild` 仅由管理员在维护窗口显式执行，从流水重建余额；记录审计。
- 正常业务不得依赖定时重建才能正确。

## 8. 成本服务

在 `apps/supplies/domain.py` 放纯计算函数：

```python
quantize_quantity(value) -> Decimal
quantize_unit_cost(value) -> Decimal
quantize_money(value) -> Decimal
calculate_receipt(balance_qty, balance_amount, in_qty, in_unit_cost)
calculate_issue(balance_qty, balance_amount, out_qty)
calculate_return(balance_qty, balance_amount, return_qty, original_unit_cost)
```

### 8.1 入库

```python
in_amount = money(in_qty * in_unit_cost)
new_qty = qty(old_qty + in_qty)
new_amount = money(old_amount + in_amount)
new_avg = cost(new_amount / new_qty) if new_qty else ZERO_COST
```

### 8.2 出库

```python
if out_qty > old_qty:
    raise ValidationError("库存不足")

if out_qty == old_qty:
    out_amount = old_amount
    new_qty = 0
    new_amount = 0
    new_avg = 0
else:
    avg = cost(old_amount / old_qty)
    out_amount = money(out_qty * avg)
    new_qty = qty(old_qty - out_qty)
    new_amount = money(old_amount - out_amount)
    new_avg = cost(new_amount / new_qty)
```

该“全部出库取尽余额金额”规则必须有测试，防止金额残留 `0.01` 而数量为 0。

### 8.3 调拨

- 锁定来源和目标余额，锁顺序固定为按余额主键或 `(warehouse_id, item_id)` 排序。
- 先按来源余额计算调出金额。
- 目标按该调出金额作为入库金额，不重新用手工单价。
- 来源和目标流水在同一事务提交。

### 8.4 退回

- `return_amount = money(return_qty * source_cost)`。
- 若原保管批次最后一次全部归还，可以使用保管记录剩余金额，避免保管金额尾差。
- 退回仓库后根据“原库存金额 + 退回金额”重新计算平均成本。

## 9. 过账服务

所有状态动作位于 `apps/supplies/services.py`，View 不直接修改余额或状态。

建议公开服务：

```python
create_supply_document(...)
update_draft_document(...)
cancel_supply_document(...)
post_supply_document(*, document, actor, idempotency_key, request=None)
reverse_supply_document(*, document, actor, idempotency_key, reason, request=None)

create_custody_transfer(...)
return_custody_to_warehouse(...)
write_off_custody(...)

publish_supply_count_task(...)
record_supply_count(...)
close_supply_count_task(...)
```

### 9.1 `post_supply_document` 事务流程

```text
transaction.atomic
  1. select_for_update 锁定单据
  2. 校验公司、权限、状态、幂等键和所有行
  3. 按固定顺序锁定所有涉及的 SupplyStockBalance
  4. 逐行计算金额和过账单价
  5. 更新余额缓存
  6. 创建不可变库存流水
  7. durable_quantity 领用时创建保管记录和保管流水
  8. 写回单据行 posted_unit_cost / posted_amount
  9. 更新单据状态、过账人、过账时间
  10. 写入 write_business_audit_log
commit
```

任何一步失败必须回滚余额、流水、保管和审计记录。

### 9.2 幂等

- 单据创建使用 `(company, idempotency_key)` 唯一约束。
- 过账重复提交时，在同一单据已过账且请求键一致的情况下返回已有结果，不再次创建流水。
- 单据状态和唯一流水约束双重阻止重复过账。
- 冲销使用独立幂等键和原单一对一冲销关系。

### 9.3 冲销可行性校验

为保证移动加权平均成本的历史确定性，冲销不是任意历史单据的“反向插入”。服务必须：

1. 锁定原单及其全部库存流水。
2. 对每个“仓库 + 物品”检查原单流水是否为当前最新流水；只要存在后续库存流水即拒绝。
3. 对耐用品领用检查其保管记录是否存在后续保管流水；存在归还、转交、报损、报废、盘点解决或更正即拒绝。
4. 通过后，按原流水前后余额生成精确反向流水，而不是用当前平均成本重新估算。
5. 拒绝时提示使用当前日期的入库、退回、调整或保管动作更正。

该规则牺牲任意历史冲销能力，换取实现简单、成本可解释和流水可重建，是本版本的批准方案。

### 9.4 锁粒度

- 锁单据行的父单据。
- 锁涉及的余额行；余额不存在时，先按唯一键 `get_or_create`，捕获并发唯一冲突后重新获取并锁定。
- 调拨对多个余额按稳定排序锁定，避免 A→B 和 B→A 并发死锁。
- 保管动作锁定来源 `SupplyCustody`。

## 10. 数量型耐用品模型

### 10.1 `SupplyCustody`

```python
class Status(models.TextChoices):
    OPEN = "open", "在管"
    CLOSED = "closed", "已结清"

id: UUIDField
company
item: ForeignKey(SupplyItem, PROTECT)
origin_issue_line: ForeignKey(SupplyDocumentLine, null=True, blank=True, PROTECT)
origin_import_row: ForeignKey(SupplyImportRow, null=True, blank=True, PROTECT)
parent_custody: ForeignKey("self", null=True, blank=True, PROTECT)
department: ForeignKey(Department, PROTECT)
employee: ForeignKey(Employee, null=True, blank=True, PROTECT)
current_quantity: DecimalField(**QUANTITY)
current_amount: DecimalField(**MONEY)
unit_cost_snapshot: DecimalField(**UNIT_COST)
started_on: DateField
status: CharField(max_length=16)
remark: TextField(blank=True)
created_at / updated_at
```

约束：

- 物品必须为 `durable_quantity`。
- `current_quantity >= 0`、`current_amount >= 0`。
- 开放状态数量必须大于 0；关闭状态数量和金额必须为 0。
- 来源领用行和来源导入行二选一。
- 员工若非空，必须与部门一致且在职、启用。
- 不直接覆盖部门、员工或余额；通过服务和保管流水改变。

### 10.2 `SupplyCustodyMovement`

```python
class Action(models.TextChoices):
    ISSUE = "issue", "领用建立"
    OPENING = "opening", "期初建立"
    RETURN = "return", "归还仓库"
    TRANSFER = "transfer", "责任转交"
    LOSS = "loss", "报损"
    SCRAP = "scrap", "报废"
    CORRECTION = "correction", "受控更正"
    REVERSAL = "reversal", "冲销"

id
company
item
from_custody: ForeignKey(SupplyCustody, null=True, blank=True, PROTECT)
to_custody: ForeignKey(SupplyCustody, null=True, blank=True, PROTECT)
action
quantity
amount
unit_cost
business_date
source_document_line: ForeignKey(SupplyDocumentLine, null=True, blank=True, PROTECT)
reason
created_by / created_at
reverses_movement: OneToOneField("self", null=True, blank=True, PROTECT)
```

含义：

- 领用/期初：`from_custody = NULL`，`to_custody` 必填。
- 归还/报损/报废：`from_custody` 必填，`to_custody = NULL`。
- 转交：两者都必填且不同。
- 数量和金额为正；方向由 from/to 表示。
- 记录不可更新或删除。

### 10.3 转交服务

事务内：

1. 锁定来源保管记录。
2. 校验数量和新责任人。
3. 计算转交金额；若全部转交，取来源全部剩余金额。
4. 减少来源余额，必要时关闭。
5. 创建新的目标保管记录，不与其他来源批次自动合并，以保留成本和来源链路。
6. 创建一条 `TRANSFER` 保管流水。
7. 写审计。

### 10.4 归还服务

归还需要同时处理保管和库存，必须使用一个事务：

1. 锁定保管记录。
2. 锁定目标仓库余额。
3. 计算归还金额。
4. 减少/关闭保管记录。
5. 增加库存余额并创建库存退回流水。
6. 创建保管归还流水。
7. 更新退回单据和行。
8. 写审计。

## 11. 导入模型

### 11.1 `SupplyImportBatch`

```python
class ImportType:
    ITEM_MASTER
    OPENING_STOCK
    OPENING_CUSTODY

class Status:
    UPLOADED
    VALIDATED
    CONFIRMED
    FAILED
    CANCELLED

id, company, import_type, original_filename
stored_attachment 或现有 Attachment 引用
status
row_count, valid_count, invalid_count
idempotency_key
uploaded_by / uploaded_at
confirmed_by / confirmed_at
error_summary
```

### 11.2 `SupplyImportRow`

```python
batch
row_number
raw_data_json
normalized_data_json
is_valid
errors_json
created_item: FK nullable
created_document_line: FK nullable
created_custody: FK nullable
```

确认策略：

- 全有或全无。
- 使用 `transaction.atomic()`。
- 先锁批次并检查状态。
- 重复确认返回已创建结果。
- 物品导入创建档案。
- 期初库存导入创建一张或按仓库分组创建多张期初单草稿，不自动过账。
- 期初保管导入直接创建期初保管记录和流水，需在确认页明确提示；也可在 Sprint 16 采用“确认后生成待过账期初保管批次”。推荐直接确认建立，因为它不涉及仓库库存，但必须幂等。

## 12. 盘点模型

### 12.1 `SupplyCountTask`

```python
class CountDomain:
    WAREHOUSE_STOCK = "warehouse_stock"
    CUSTODY = "custody"

class Status:
    DRAFT
    IN_PROGRESS
    RECONCILIATION
    CLOSED
    CANCELLED

id, company, task_no, name, count_domain
warehouse nullable
department nullable
employee nullable
planned_start / planned_end
snapshot_at
status
idempotency_key
created_by / published_by / stopped_by / closed_by / cancelled_by
corresponding timestamps
remark
```

范围约束：

- 仓库库存盘点必须指定仓库，不指定部门/员工。
- 保管盘点可指定部门，员工可选，不指定仓库。

### 12.2 `SupplyCountLine`

```python
count_task
item
stock_balance nullable
custody nullable
expected_quantity
expected_amount
counted_quantity nullable
difference_quantity nullable
resolution_type nullable
resolution_reference_id / typed FKs as approved
remark
counted_by / counted_at
```

发布任务时创建快照行。关闭仓库盘点时，差异行生成一张 `count_adjustment` 单并在同一事务过账。关闭保管盘点时，所有差异必须关联实际归还、转交、报损、报废或更正动作。

## 13. 离职清退集成

### 13.1 新模型 `EmployeeSupplyClearanceItem`

放在 `apps/supplies/models.py`，引用现有 `EmployeeAssetClearance`：

```python
clearance: ForeignKey(EmployeeAssetClearance, PROTECT, related_name="supply_items")
company
custody: ForeignKey(SupplyCustody, PROTECT)
item_code_snapshot
item_name_snapshot
quantity_snapshot
amount_snapshot
department_snapshot
employee_snapshot
resolution: pending / returned / transferred / lost / scrapped
resolved_by / resolved_at
custody_movement: ForeignKey(SupplyCustodyMovement, null=True, PROTECT)
remark
```

唯一约束：`(clearance, custody)`。

### 13.2 修改现有清退头

对 `EmployeeAssetClearance` 做加法迁移：

```python
total_supply_custodies_snapshot = PositiveIntegerField(default=0)
unresolved_supply_custodies = PositiveIntegerField(default=0)
```

保留原 `total_assets_snapshot` 和 `unresolved_assets` 语义，不重命名、不迁移已有历史。

完成条件改为：

```text
unresolved_assets == 0
AND unresolved_supply_custodies == 0
```

发起清退时，在同一事务中建立资产项和所有当前个人保管余额大于 0 的数量型耐用品项。补充清退沿用现有机制。

### 13.3 循环依赖处理

`apps.supplies` 依赖 `apps.offboarding` 的模型，而 `offboarding.services` 需要调用 supplies 查询。避免模型级循环导入：

- 模型 ForeignKey 使用字符串形式：`"offboarding.EmployeeAssetClearance"`。
- offboarding 服务内部局部导入 supplies 服务或通过 `apps.get_model()`。
- 不让 `offboarding.models` 反向导入 supplies 模型。

## 14. 逐件低值耐用品集成

Sprint 16 不新增资产表。

实现内容：

1. 资产列表增加 `accounting_treatment` 筛选。
2. 增加“受控非固定资产”列表视图或复用通用资产列表。
3. 低值物品导航增加“逐件低值耐用品”入口。
4. 财务确认页面对 `controlled_non_fixed` 显示明确说明。
5. 在折旧服务所有入口增加统一断言：仅 `fixed_asset` 可创建/激活折旧配置或进入折旧批次。
6. 增加回归测试，确保既有 controlled_non_fixed 无折旧记录。
7. 报表口径不把 controlled_non_fixed 混入固定资产净值、累计折旧和 T+ 折旧导出。

不得通过删除现有财务约束或修改 `Asset.quantity` 实现低值耐用品。

## 15. 权限设计

新增 `apps/supplies/permissions.py`，沿用现有模式：

```python
role_names_for(user)
resolve_department_ids(user, company)
scoped_...()
can_...()
require_...()
```

建议函数：

```python
scoped_supply_items(user, company, queryset=None)
scoped_supply_documents(user, company, queryset=None)
scoped_supply_custodies(user, company, queryset=None)
scoped_supply_count_tasks(user, company, queryset=None)

require_manage_supply_master_data(...)
require_create_supply_document(...)
require_post_supply_document(...)
require_view_supply_cost(...)
require_manage_custody(...)
require_view_custody(...)
require_create_count_task(...)
require_close_count_task(...)
```

成本字段展示：

- `finance`、`warehouse`、`equipment`、`management`、`system_admin` 可查看。
- `department_manager` 和 `employee` 默认只看数量；如现有项目权限矩阵另有明确口径，以最新文档为准。

部门范围：

- 部门负责人只能查看其管理部门的领用、保管和保管盘点。
- 员工只能查看本人作为领用人或保管人的记录。
- 仓库库存总量对仓库、财务、设备、管理层开放；普通员工不开放全仓明细。

## 16. 审计范围

不做新安全审计模块，只复用现有 `write_business_audit_log`。

必须审计：

- 分类、仓库、物品新增、修改、停用；
- 单据过账、取消、冲销；
- 保管转交、归还、报损、报废、更正；
- 导入确认；
- 盘点发布、停止、关闭和取消；
- 余额重建命令。

库存流水和保管流水本身已经是业务证据，但关键动作仍记录 AuditLog。审计写入与业务事务尽量同事务提交。

## 17. 页面和 URL

建议 URL：

```text
/supplies/                              dashboard
/supplies/categories/
/supplies/items/
/supplies/warehouses/
/supplies/stock/
/supplies/stock/ledger/
/supplies/documents/
/supplies/documents/new/<type>/
/supplies/documents/<uuid>/
/supplies/documents/<uuid>/post/
/supplies/documents/<uuid>/cancel/
/supplies/documents/<uuid>/reverse/
/supplies/custodies/
/supplies/custodies/<uuid>/
/supplies/custodies/<uuid>/transfer/
/supplies/custodies/<uuid>/return/
/supplies/custodies/<uuid>/write-off/
/supplies/counts/
/supplies/counts/new/
/supplies/counts/<uuid>/
/supplies/imports/items/
/supplies/imports/opening-stock/
/supplies/imports/opening-custody/
/supplies/reports/...
```

UI 原则：

- 中文标签；
- 单据页面分“基本信息、明细、金额汇总、操作历史”；
- 草稿和已过账状态视觉区分；
- 已过账页面只读，只显示冲销入口；
- 数量不足、退回超量和员工状态异常给出明确中文错误；
- 移动端至少支持查看本人保管、归还/转交确认和盘点录入；
- 不要求扫码，因为数量型物品无逐件二维码。

## 18. Excel 设计

`apps/supplies/excel.py` 负责：

- 模板生成；
- 导入解析和行级校验；
- 库存、流水、领用、保管、盘点报表导出；
- 用户文本的公式注入防护复用现有导出工具；
- 日期和数值保持原生 Excel 类型。

不得把 Excel 解析逻辑写进 View。

导出量较大时使用 `openpyxl.Workbook(write_only=True)` 或当前项目批准方式，避免在内存中复制完整查询集。

## 19. 索引

至少建立：

```text
SupplyItem(company, normalized_item_code)
SupplyItem(company, item_type, is_active)
SupplyStockBalance(company, warehouse, item) UNIQUE
SupplyStockBalance(company, item)
SupplyStockLedger(company, warehouse, item, occurred_at)
SupplyStockLedger(company, document)
SupplyDocument(company, document_type, status, business_date)
SupplyDocument(company, department, business_date)
SupplyCustody(company, employee, status)
SupplyCustody(company, department, item, status)
SupplyCustodyMovement(company, item, created_at)
SupplyCountTask(company, status, count_domain)
```

列表 QuerySet 使用 `select_related` / `prefetch_related`，所有流水页面分页。

## 20. 迁移策略

### Sprint 13

- 新 app 和基础档案模型。
- 不修改现有资产、财务、盘点、清退表。

### Sprint 14

- 单据、单据行、序号、余额、库存流水、导入批次。
- 仅支持期初和日常入库。

### Sprint 15

- 领用、易耗品退回、仓库调拨、冲销。
- 增加耐用品保管和保管流水。

### Sprint 16

- 完成耐用品转交、归还、报损、报废、期初保管导入。
- 集成逐件 controlled_non_fixed 资产。

### Sprint 17

- 数量库存盘点、保管盘点。
- 现有离职清退表加法字段和 `EmployeeSupplyClearanceItem`。

### Sprint 18

- 报表、首页、Excel 导出、UAT、性能和余额重建验证。

所有迁移必须支持：

- 空库迁移；
- 从当前 `main` 数据库升级；
- 保留现有资产、财务、盘点、清退和审计历史；
- 不做 destructive migration。

## 21. 测试策略

### 21.1 单元测试

- Decimal 舍入；
- 入库移动平均；
- 部分出库；
- 全部出库金额清零；
- 退回原成本；
- 调拨金额一致；
- 保管全部/部分转交；
- 归还后库存和保管金额一致；
- 报损/报废余额；
- 状态机和字段约束。

### 21.2 服务测试

- 过账原子性；
- 重复过账幂等；
- 库存不足回滚；
- 调拨任一仓库失败整体回滚；
- 冲销一次性；
- 冲销被后续业务阻止；
- 导入确认幂等；
- 盘点关闭自动调整；
- 离职清退同时包含资产和耐用品保管。

### 21.3 权限测试

只测试本模块必要权限：

- 公司隔离；
- 仓库/财务过账；
- 部门负责人仅本部门；
- 员工仅本人；
- 无成本权限时不返回成本字段；
- 直接 URL 和 POST 仍受后端限制。

不把本 Sprint 扩展成全仓库安全复审。

### 21.4 PostgreSQL 并发测试

必须在 PostgreSQL 测试：

- 两个并发领用同一余额，不允许超发；
- 相反方向并发调拨不死锁或可安全重试；
- 并发创建单据号不重复；
- 并发重复过账只产生一组流水；
- 并发归还同一保管记录不超量。

### 21.5 回归测试

- 现有完整测试套件继续通过。
- `Asset.quantity=1` 约束仍在。
- 固定资产折旧不受影响。
- controlled_non_fixed 不计提折旧。
- 现有资产盘点和离职清退在没有 supplies 数据时行为不变。

## 22. 完成标准

每个 Sprint 完成前：

1. 生成并检查迁移。
2. 执行该 Sprint 新增测试。
3. 执行现有完整测试套件；若环境限制，至少执行所有受影响 app 的回归测试并明确说明。
4. PostgreSQL 专项并发测试通过后，才能关闭库存过账验收项。
5. 页面可在 Chrome/Edge 正常操作。
6. 汇报变更文件、迁移、测试结果、业务规则和未实现项。
7. 不自行继续下一个 Sprint。

## 23. 明确禁止的实现捷径

- 把数量型物品存入 `Asset.quantity > 1`。
- 新建与 `Asset` 重复的逐件低值资产表。
- 以 `MAX(document_no)+1` 发号。
- 只更新余额、不写库存流水。
- 允许已过账单据直接编辑或删除。
- 使用 `float` 计算成本。
- 通过前端禁用按钮代替库存锁、权限和后端校验。
- 为赶进度跳过迁移、幂等或回滚测试。
- 引入通用采购、审批、会计凭证或生产物料库存。
