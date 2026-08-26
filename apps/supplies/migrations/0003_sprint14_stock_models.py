import django.db.models.deletion
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import migrations, models


DOCUMENT_TYPES = [
    ("opening", "期初入库"),
    ("receipt", "日常入库"),
    ("issue", "领用出库"),
    ("return", "领用退回"),
    ("transfer", "仓库调拨"),
    ("count_adjustment", "盘点调整"),
    ("reversal", "冲销"),
]
DOCUMENT_STATUSES = [
    ("draft", "草稿"),
    ("posted", "已过账"),
    ("reversed", "已冲销"),
    ("cancelled", "已取消"),
]
MOVEMENT_TYPES = [
    ("opening_in", "期初入库"),
    ("receipt_in", "日常入库"),
    ("issue_out", "领用出库"),
    ("return_in", "领用退回"),
    ("transfer_out", "调拨出库"),
    ("transfer_in", "调拨入库"),
    ("count_gain", "盘盈"),
    ("count_loss", "盘亏"),
    ("reversal", "冲销"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("masterdata", "0011_sprint14_opening_stock_import"),
        ("supplies", "0002_postgresql_integrity_guards"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SupplyDocument",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("document_no", models.CharField(max_length=64, verbose_name="单据编号")),
                ("document_type", models.CharField(choices=DOCUMENT_TYPES, max_length=32, verbose_name="单据类型")),
                ("business_date", models.DateField(verbose_name="业务日期")),
                ("external_reference", models.CharField(blank=True, max_length=200, verbose_name="外部参考号")),
                ("counterparty_name", models.CharField(blank=True, max_length=200, verbose_name="来源或往来单位")),
                ("remark", models.TextField(blank=True, verbose_name="备注")),
                ("status", models.CharField(choices=DOCUMENT_STATUSES, default="draft", max_length=16, verbose_name="状态")),
                ("idempotency_key", models.CharField(max_length=128, verbose_name="创建幂等键")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("posted_at", models.DateTimeField(blank=True, null=True, verbose_name="过账时间")),
                ("cancelled_at", models.DateTimeField(blank=True, null=True, verbose_name="取消时间")),
                ("cancellation_reason", models.TextField(blank=True, verbose_name="取消原因")),
                ("reversed_at", models.DateTimeField(blank=True, null=True, verbose_name="冲销时间")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="supply_documents", to="masterdata.company", verbose_name="公司")),
                ("department", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="supply_documents", to="masterdata.department", verbose_name="领用/保管部门")),
                ("employee", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="supply_documents", to="masterdata.employee", verbose_name="领用/保管员工")),
                ("source_warehouse", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="source_supply_documents", to="supplies.supplywarehouse", verbose_name="来源仓库")),
                ("target_warehouse", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="target_supply_documents", to="supplies.supplywarehouse", verbose_name="目标仓库")),
                ("reversal_of", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="reversal_document", to="supplies.supplydocument", verbose_name="被冲销原单")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_supply_documents", to=settings.AUTH_USER_MODEL, verbose_name="创建人")),
                ("posted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="posted_supply_documents", to=settings.AUTH_USER_MODEL, verbose_name="过账人")),
                ("cancelled_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="cancelled_supply_documents", to=settings.AUTH_USER_MODEL, verbose_name="取消人")),
                ("reversed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="reversed_supply_documents", to=settings.AUTH_USER_MODEL, verbose_name="冲销人")),
            ],
            options={
                "verbose_name": "低值物品库存单据",
                "verbose_name_plural": "低值物品库存单据",
                "ordering": ("-business_date", "-document_no"),
            },
        ),
        migrations.CreateModel(
            name="SupplyDocumentLine",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("line_no", models.PositiveIntegerField(verbose_name="行号")),
                ("quantity", models.DecimalField(decimal_places=4, max_digits=18, verbose_name="数量")),
                ("entered_unit_cost", models.DecimalField(blank=True, decimal_places=6, max_digits=18, null=True, verbose_name="录入单位成本")),
                ("posted_unit_cost", models.DecimalField(blank=True, decimal_places=6, max_digits=18, null=True, verbose_name="过账单位成本")),
                ("posted_amount", models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True, verbose_name="过账金额")),
                ("adjustment_direction", models.CharField(blank=True, choices=[("increase", "盘盈/增加"), ("decrease", "盘亏/减少")], max_length=16, null=True, verbose_name="调整方向")),
                ("line_remark", models.TextField(blank=True, verbose_name="明细备注/0 成本原因")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="supply_document_lines", to="masterdata.company", verbose_name="公司")),
                ("document", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="lines", to="supplies.supplydocument", verbose_name="库存单据")),
                ("item", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="document_lines", to="supplies.supplyitem", verbose_name="物品")),
                ("source_issue_line", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="return_lines", to="supplies.supplydocumentline", verbose_name="原领用明细")),
            ],
            options={
                "verbose_name": "低值物品库存单据明细",
                "verbose_name_plural": "低值物品库存单据明细",
                "ordering": ("document_id", "line_no"),
            },
        ),
        migrations.CreateModel(
            name="SupplyDocumentSequence",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("sequence_type", models.CharField(choices=DOCUMENT_TYPES, max_length=32, verbose_name="序号类型")),
                ("year", models.PositiveSmallIntegerField(verbose_name="年度")),
                ("current_value", models.PositiveBigIntegerField(default=0, verbose_name="当前序号")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="supply_document_sequences", to="masterdata.company", verbose_name="公司")),
            ],
            options={
                "verbose_name": "低值物品单据序号",
                "verbose_name_plural": "低值物品单据序号",
            },
        ),
        migrations.CreateModel(
            name="SupplyStockBalance",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("quantity_on_hand", models.DecimalField(decimal_places=4, default=Decimal("0.0000"), max_digits=18, verbose_name="库存数量")),
                ("amount_on_hand", models.DecimalField(decimal_places=2, default=Decimal("0.00"), max_digits=18, verbose_name="库存金额")),
                ("average_unit_cost", models.DecimalField(decimal_places=6, default=Decimal("0.000000"), max_digits=18, verbose_name="移动平均成本")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="supply_stock_balances", to="masterdata.company", verbose_name="公司")),
                ("warehouse", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="stock_balances", to="supplies.supplywarehouse", verbose_name="仓库")),
                ("item", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="stock_balances", to="supplies.supplyitem", verbose_name="物品")),
            ],
            options={
                "verbose_name": "低值物品库存余额",
                "verbose_name_plural": "低值物品库存余额",
                "ordering": ("warehouse__normalized_code", "item__normalized_item_code"),
            },
        ),
        migrations.CreateModel(
            name="SupplyStockLedger",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("movement_type", models.CharField(choices=MOVEMENT_TYPES, max_length=32, verbose_name="流水类型")),
                ("quantity_delta", models.DecimalField(decimal_places=4, max_digits=18, verbose_name="数量变动")),
                ("amount_delta", models.DecimalField(decimal_places=2, max_digits=18, verbose_name="金额变动")),
                ("unit_cost", models.DecimalField(decimal_places=6, max_digits=18, verbose_name="单位成本")),
                ("quantity_before", models.DecimalField(decimal_places=4, max_digits=18, verbose_name="变动前数量")),
                ("quantity_after", models.DecimalField(decimal_places=4, max_digits=18, verbose_name="变动后数量")),
                ("amount_before", models.DecimalField(decimal_places=2, max_digits=18, verbose_name="变动前金额")),
                ("amount_after", models.DecimalField(decimal_places=2, max_digits=18, verbose_name="变动后金额")),
                ("average_unit_cost_before", models.DecimalField(decimal_places=6, max_digits=18, verbose_name="变动前平均成本")),
                ("average_unit_cost_after", models.DecimalField(decimal_places=6, max_digits=18, verbose_name="变动后平均成本")),
                ("occurred_at", models.DateTimeField(verbose_name="发生时间")),
                ("company", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="supply_stock_ledgers", to="masterdata.company", verbose_name="公司")),
                ("warehouse", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="stock_ledgers", to="supplies.supplywarehouse", verbose_name="仓库")),
                ("item", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="stock_ledgers", to="supplies.supplyitem", verbose_name="物品")),
                ("document", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="stock_ledgers", to="supplies.supplydocument", verbose_name="库存单据")),
                ("document_line", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="stock_ledgers", to="supplies.supplydocumentline", verbose_name="库存单据明细")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_supply_stock_ledgers", to=settings.AUTH_USER_MODEL, verbose_name="操作人")),
                ("reverses_ledger", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="reversal_ledger", to="supplies.supplystockledger", verbose_name="被冲销流水")),
            ],
            options={
                "verbose_name": "低值物品库存流水",
                "verbose_name_plural": "低值物品库存流水",
                "ordering": ("-occurred_at", "document__document_no", "document_line__line_no"),
            },
        ),
        migrations.AddIndex(model_name="supplydocument", index=models.Index(fields=["company", "document_type", "status", "business_date"], name="supply_doc_type_status_idx")),
        migrations.AddIndex(model_name="supplydocument", index=models.Index(fields=["company", "department", "business_date"], name="supply_doc_department_idx")),
        migrations.AddIndex(model_name="supplydocument", index=models.Index(fields=["company", "target_warehouse", "business_date"], name="supply_doc_target_idx")),
        migrations.AddConstraint(model_name="supplydocument", constraint=models.UniqueConstraint(fields=("company", "document_no"), name="uq_supply_document_company_no")),
        migrations.AddConstraint(model_name="supplydocument", constraint=models.UniqueConstraint(fields=("company", "idempotency_key"), name="uq_supply_document_company_idem")),
        migrations.AddConstraint(model_name="supplydocument", constraint=models.CheckConstraint(condition=models.Q(document_type__in=[value for value, _ in DOCUMENT_TYPES]), name="ck_supply_document_type_valid")),
        migrations.AddConstraint(model_name="supplydocument", constraint=models.CheckConstraint(condition=models.Q(status__in=[value for value, _ in DOCUMENT_STATUSES]), name="ck_supply_document_status_valid")),
        migrations.AddConstraint(model_name="supplydocument", constraint=models.CheckConstraint(condition=models.Q(source_warehouse__isnull=True) | models.Q(target_warehouse__isnull=True) | ~models.Q(source_warehouse=models.F("target_warehouse")), name="ck_supply_document_warehouses_differ")),
        migrations.AddConstraint(model_name="supplydocument", constraint=models.CheckConstraint(condition=~models.Q(document_type__in=("opening", "receipt")) | models.Q(source_warehouse__isnull=True, target_warehouse__isnull=False, department__isnull=True, employee__isnull=True, reversal_of__isnull=True), name="ck_supply_document_receipt_shape")),
        migrations.AddConstraint(
            model_name="supplydocument",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(status="draft", posted_at__isnull=True, posted_by__isnull=True, cancelled_at__isnull=True, cancelled_by__isnull=True, cancellation_reason="", reversed_at__isnull=True, reversed_by__isnull=True)
                    | models.Q(status="posted", posted_at__isnull=False, cancelled_at__isnull=True, cancelled_by__isnull=True, cancellation_reason="", reversed_at__isnull=True, reversed_by__isnull=True)
                    | models.Q(status="cancelled", posted_at__isnull=True, posted_by__isnull=True, cancelled_at__isnull=False, cancellation_reason__gt="", reversed_at__isnull=True, reversed_by__isnull=True)
                    | models.Q(status="reversed", posted_at__isnull=False, cancelled_at__isnull=True, cancelled_by__isnull=True, cancellation_reason="", reversed_at__isnull=False)
                ),
                name="ck_supply_document_status_fields",
            ),
        ),
        migrations.AddConstraint(model_name="supplydocumentline", constraint=models.UniqueConstraint(fields=("document", "line_no"), name="uq_supply_document_line_no")),
        migrations.AddConstraint(model_name="supplydocumentline", constraint=models.CheckConstraint(condition=models.Q(line_no__gte=1), name="ck_supply_document_line_positive_no")),
        migrations.AddConstraint(model_name="supplydocumentline", constraint=models.CheckConstraint(condition=models.Q(quantity__gt=0), name="ck_supply_document_line_positive_qty")),
        migrations.AddConstraint(model_name="supplydocumentline", constraint=models.CheckConstraint(condition=models.Q(entered_unit_cost__isnull=True) | models.Q(entered_unit_cost__gte=0), name="ck_supply_document_line_entered_cost")),
        migrations.AddConstraint(model_name="supplydocumentline", constraint=models.CheckConstraint(condition=models.Q(entered_unit_cost__isnull=True) | ~models.Q(entered_unit_cost=0) | ~models.Q(line_remark=""), name="ck_supply_document_line_zero_reason")),
        migrations.AddConstraint(model_name="supplydocumentline", constraint=models.CheckConstraint(condition=models.Q(posted_unit_cost__isnull=True) | models.Q(posted_unit_cost__gte=0), name="ck_supply_document_line_posted_cost")),
        migrations.AddConstraint(model_name="supplydocumentline", constraint=models.CheckConstraint(condition=models.Q(posted_amount__isnull=True) | models.Q(posted_amount__gte=0), name="ck_supply_document_line_posted_amount")),
        migrations.AddConstraint(model_name="supplydocumentline", constraint=models.CheckConstraint(condition=models.Q(posted_unit_cost__isnull=True, posted_amount__isnull=True) | models.Q(posted_unit_cost__isnull=False, posted_amount__isnull=False), name="ck_supply_document_line_posted_pair")),
        migrations.AddConstraint(model_name="supplydocumentline", constraint=models.CheckConstraint(condition=models.Q(adjustment_direction__isnull=True) | models.Q(adjustment_direction__in=("increase", "decrease")), name="ck_supply_document_line_direction")),
        migrations.AddConstraint(model_name="supplydocumentsequence", constraint=models.UniqueConstraint(fields=("company", "sequence_type", "year"), name="uq_supply_doc_sequence_scope")),
        migrations.AddConstraint(model_name="supplydocumentsequence", constraint=models.CheckConstraint(condition=models.Q(sequence_type__in=[value for value, _ in DOCUMENT_TYPES]), name="ck_supply_doc_sequence_type")),
        migrations.AddConstraint(model_name="supplydocumentsequence", constraint=models.CheckConstraint(condition=models.Q(year__gte=1900, year__lte=9999), name="ck_supply_doc_sequence_year")),
        migrations.AddConstraint(model_name="supplydocumentsequence", constraint=models.CheckConstraint(condition=models.Q(current_value__gte=0), name="ck_supply_doc_sequence_nonnegative")),
        migrations.AddIndex(model_name="supplystockbalance", index=models.Index(fields=["company", "item"], name="supply_balance_item_idx")),
        migrations.AddConstraint(model_name="supplystockbalance", constraint=models.UniqueConstraint(fields=("company", "warehouse", "item"), name="uq_supply_stock_balance_scope")),
        migrations.AddConstraint(model_name="supplystockbalance", constraint=models.CheckConstraint(condition=models.Q(quantity_on_hand__gte=0), name="ck_supply_stock_balance_qty")),
        migrations.AddConstraint(model_name="supplystockbalance", constraint=models.CheckConstraint(condition=models.Q(amount_on_hand__gte=0), name="ck_supply_stock_balance_amount")),
        migrations.AddConstraint(model_name="supplystockbalance", constraint=models.CheckConstraint(condition=models.Q(average_unit_cost__gte=0), name="ck_supply_stock_balance_average")),
        migrations.AddConstraint(model_name="supplystockbalance", constraint=models.CheckConstraint(condition=models.Q(quantity_on_hand__gt=0) | models.Q(quantity_on_hand=0, amount_on_hand=0, average_unit_cost=0), name="ck_supply_stock_balance_zero")),
        migrations.AddIndex(model_name="supplystockledger", index=models.Index(fields=["company", "warehouse", "item", "occurred_at"], name="supply_ledger_scope_at_idx")),
        migrations.AddIndex(model_name="supplystockledger", index=models.Index(fields=["company", "document"], name="supply_ledger_document_idx")),
        migrations.AddConstraint(model_name="supplystockledger", constraint=models.UniqueConstraint(fields=("document_line", "warehouse", "movement_type"), name="uq_supply_stock_ledger_posting")),
        migrations.AddConstraint(model_name="supplystockledger", constraint=models.CheckConstraint(condition=models.Q(movement_type__in=[value for value, _ in MOVEMENT_TYPES]), name="ck_supply_stock_ledger_type")),
        migrations.AddConstraint(model_name="supplystockledger", constraint=models.CheckConstraint(condition=~models.Q(quantity_delta=0), name="ck_supply_stock_ledger_delta")),
        migrations.AddConstraint(model_name="supplystockledger", constraint=models.CheckConstraint(condition=models.Q(unit_cost__gte=0), name="ck_supply_stock_ledger_cost")),
        migrations.AddConstraint(model_name="supplystockledger", constraint=models.CheckConstraint(condition=models.Q(quantity_before__gte=0, quantity_after__gte=0, amount_before__gte=0, amount_after__gte=0, average_unit_cost_before__gte=0, average_unit_cost_after__gte=0), name="ck_supply_stock_ledger_nonnegative")),
        migrations.AddConstraint(model_name="supplystockledger", constraint=models.CheckConstraint(condition=models.Q(quantity_after=models.F("quantity_before") + models.F("quantity_delta")), name="ck_supply_stock_ledger_qty_equation")),
        migrations.AddConstraint(model_name="supplystockledger", constraint=models.CheckConstraint(condition=models.Q(amount_after=models.F("amount_before") + models.F("amount_delta")), name="ck_supply_stock_ledger_amount_equation")),
    ]
