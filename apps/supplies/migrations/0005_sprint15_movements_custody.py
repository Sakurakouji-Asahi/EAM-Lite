import django.db.models.deletion
import uuid

from django.conf import settings
from django.db import migrations, models


def install_postgresql_ledger_equations(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            ALTER TABLE supplies_supplystockledger
            ADD CONSTRAINT ck_supply_stock_ledger_qty_equation
            CHECK (quantity_after = quantity_before + quantity_delta),
            ADD CONSTRAINT ck_supply_stock_ledger_amount_equation
            CHECK (amount_after = amount_before + amount_delta)
            """
        )


def uninstall_postgresql_ledger_equations(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            ALTER TABLE supplies_supplystockledger
            DROP CONSTRAINT IF EXISTS ck_supply_stock_ledger_qty_equation,
            DROP CONSTRAINT IF EXISTS ck_supply_stock_ledger_amount_equation
            """
        )


class Migration(migrations.Migration):
    dependencies = [
        ("supplies", "0004_sprint14_postgresql_guards"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SupplyCustody",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "current_quantity",
                    models.DecimalField(
                        decimal_places=4,
                        max_digits=18,
                        verbose_name="当前保管数量",
                    ),
                ),
                (
                    "current_amount",
                    models.DecimalField(
                        decimal_places=2,
                        max_digits=18,
                        verbose_name="当前保管金额",
                    ),
                ),
                (
                    "unit_cost_snapshot",
                    models.DecimalField(
                        decimal_places=6,
                        max_digits=18,
                        verbose_name="单位成本快照",
                    ),
                ),
                ("started_on", models.DateField(verbose_name="开始日期")),
                (
                    "status",
                    models.CharField(
                        choices=[("open", "在管"), ("closed", "已结清")],
                        default="open",
                        max_length=16,
                        verbose_name="状态",
                    ),
                ),
                ("remark", models.TextField(blank=True, verbose_name="备注")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                (
                    "company",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="supply_custodies",
                        to="masterdata.company",
                        verbose_name="公司",
                    ),
                ),
                (
                    "department",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="supply_custodies",
                        to="masterdata.department",
                        verbose_name="责任部门",
                    ),
                ),
                (
                    "employee",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="supply_custodies",
                        to="masterdata.employee",
                        verbose_name="责任员工",
                    ),
                ),
                (
                    "item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="custodies",
                        to="supplies.supplyitem",
                        verbose_name="物品",
                    ),
                ),
                (
                    "origin_issue_line",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="supply_custody",
                        to="supplies.supplydocumentline",
                        verbose_name="来源领用明细",
                    ),
                ),
            ],
            options={
                "verbose_name": "数量型低值耐用品保管",
                "verbose_name_plural": "数量型低值耐用品保管",
                "ordering": ("-started_on", "item__normalized_item_code"),
            },
        ),
        migrations.CreateModel(
            name="SupplyCustodyMovement",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("issue", "领用建立"),
                            ("opening", "期初建立"),
                            ("return", "归还仓库"),
                            ("transfer", "责任转交"),
                            ("loss", "报损"),
                            ("scrap", "报废"),
                            ("correction", "受控更正"),
                            ("reversal", "冲销"),
                        ],
                        max_length=16,
                        verbose_name="动作",
                    ),
                ),
                (
                    "quantity",
                    models.DecimalField(
                        decimal_places=4,
                        max_digits=18,
                        verbose_name="数量",
                    ),
                ),
                (
                    "amount",
                    models.DecimalField(
                        decimal_places=2,
                        max_digits=18,
                        verbose_name="金额",
                    ),
                ),
                (
                    "unit_cost",
                    models.DecimalField(
                        decimal_places=6,
                        max_digits=18,
                        verbose_name="单位成本",
                    ),
                ),
                ("business_date", models.DateField(verbose_name="业务日期")),
                ("reason", models.TextField(blank=True, verbose_name="原因")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                (
                    "company",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="supply_custody_movements",
                        to="masterdata.company",
                        verbose_name="公司",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_supply_custody_movements",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="操作人",
                    ),
                ),
                (
                    "from_custody",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="outgoing_movements",
                        to="supplies.supplycustody",
                        verbose_name="转出保管记录",
                    ),
                ),
                (
                    "item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="custody_movements",
                        to="supplies.supplyitem",
                        verbose_name="物品",
                    ),
                ),
                (
                    "reverses_movement",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reversal_movement",
                        to="supplies.supplycustodymovement",
                        verbose_name="被冲销保管流水",
                    ),
                ),
                (
                    "source_document_line",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="custody_movements",
                        to="supplies.supplydocumentline",
                        verbose_name="来源单据明细",
                    ),
                ),
                (
                    "to_custody",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="incoming_movements",
                        to="supplies.supplycustody",
                        verbose_name="转入保管记录",
                    ),
                ),
            ],
            options={
                "verbose_name": "数量型低值耐用品保管流水",
                "verbose_name_plural": "数量型低值耐用品保管流水",
                "ordering": ("-created_at",),
            },
        ),
        migrations.AddField(
            model_name="supplydocumentline",
            name="source_custody",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="source_document_lines",
                to="supplies.supplycustody",
                verbose_name="原保管记录",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="supplydocument",
            name="ck_supply_document_receipt_shape",
        ),
        migrations.AddConstraint(
            model_name="supplydocument",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        document_type__in=("opening", "receipt"),
                        source_warehouse__isnull=True,
                        target_warehouse__isnull=False,
                        department__isnull=True,
                        employee__isnull=True,
                        reversal_of__isnull=True,
                    )
                    | models.Q(
                        document_type="issue",
                        source_warehouse__isnull=False,
                        target_warehouse__isnull=True,
                        department__isnull=False,
                        reversal_of__isnull=True,
                    )
                    | models.Q(
                        document_type="return",
                        source_warehouse__isnull=True,
                        target_warehouse__isnull=False,
                        department__isnull=False,
                        reversal_of__isnull=True,
                    )
                    | models.Q(
                        document_type="transfer",
                        source_warehouse__isnull=False,
                        target_warehouse__isnull=False,
                        department__isnull=True,
                        employee__isnull=True,
                        reversal_of__isnull=True,
                    )
                    | models.Q(document_type="reversal", reversal_of__isnull=False)
                    | models.Q(
                        document_type="count_adjustment",
                        reversal_of__isnull=True,
                    )
                ),
                name="ck_supply_document_s15_shape",
            ),
        ),
        migrations.AddIndex(
            model_name="supplycustody",
            index=models.Index(
                fields=["company", "employee", "status"],
                name="supply_custody_employee_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="supplycustody",
            index=models.Index(
                fields=["company", "department", "item", "status"],
                name="supply_custody_dept_item_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="supplycustody",
            constraint=models.CheckConstraint(
                condition=models.Q(current_quantity__gte=0),
                name="ck_supply_custody_qty_nonnegative",
            ),
        ),
        migrations.AddConstraint(
            model_name="supplycustody",
            constraint=models.CheckConstraint(
                condition=models.Q(current_amount__gte=0),
                name="ck_supply_custody_amount_nonnegative",
            ),
        ),
        migrations.AddConstraint(
            model_name="supplycustody",
            constraint=models.CheckConstraint(
                condition=models.Q(unit_cost_snapshot__gte=0),
                name="ck_supply_custody_cost_nonnegative",
            ),
        ),
        migrations.AddConstraint(
            model_name="supplycustody",
            constraint=models.CheckConstraint(
                condition=models.Q(status__in=("open", "closed")),
                name="ck_supply_custody_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="supplycustody",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(status="open", current_quantity__gt=0)
                    | models.Q(
                        status="closed",
                        current_quantity=0,
                        current_amount=0,
                    )
                ),
                name="ck_supply_custody_status_balance",
            ),
        ),
        migrations.AddIndex(
            model_name="supplycustodymovement",
            index=models.Index(
                fields=["company", "item", "created_at"],
                name="supply_custody_move_item_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="supplycustodymovement",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    action__in=(
                        "issue",
                        "opening",
                        "return",
                        "transfer",
                        "loss",
                        "scrap",
                        "correction",
                        "reversal",
                    )
                ),
                name="ck_supply_custody_movement_action",
            ),
        ),
        migrations.AddConstraint(
            model_name="supplycustodymovement",
            constraint=models.CheckConstraint(
                condition=models.Q(quantity__gt=0),
                name="ck_supply_custody_movement_qty",
            ),
        ),
        migrations.AddConstraint(
            model_name="supplycustodymovement",
            constraint=models.CheckConstraint(
                condition=models.Q(amount__gte=0, unit_cost__gte=0),
                name="ck_supply_custody_movement_amount",
            ),
        ),
        migrations.AddConstraint(
            model_name="supplycustodymovement",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        action__in=("issue", "opening"),
                        from_custody__isnull=True,
                        to_custody__isnull=False,
                    )
                    | models.Q(
                        action__in=("return", "loss", "scrap"),
                        from_custody__isnull=False,
                        to_custody__isnull=True,
                    )
                    | (
                        models.Q(
                            action="transfer",
                            from_custody__isnull=False,
                            to_custody__isnull=False,
                        )
                        & ~models.Q(from_custody=models.F("to_custody"))
                    )
                    | models.Q(action="correction")
                    | models.Q(
                        action="reversal",
                        from_custody__isnull=False,
                        to_custody__isnull=True,
                    )
                ),
                name="ck_supply_custody_movement_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="supplycustodymovement",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(action="reversal", reverses_movement__isnull=False)
                    | (
                        ~models.Q(action="reversal")
                        & models.Q(reverses_movement__isnull=True)
                    )
                ),
                name="ck_supply_custody_movement_reversal",
            ),
        ),
        # SQLite stores NUMERIC expressions using binary affinity and can
        # falsely reject exact two-decimal equations such as
        # 33.37 - 13.35 = 20.02.  Keep both constraints in Django state and
        # install them physically on the authoritative PostgreSQL database;
        # services validate the same Decimal equations on every backend.
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RemoveConstraint(
                    model_name="supplystockledger",
                    name="ck_supply_stock_ledger_qty_equation",
                ),
                migrations.RemoveConstraint(
                    model_name="supplystockledger",
                    name="ck_supply_stock_ledger_amount_equation",
                ),
                migrations.RunPython(
                    install_postgresql_ledger_equations,
                    uninstall_postgresql_ledger_equations,
                ),
            ],
            state_operations=[],
        ),
    ]
