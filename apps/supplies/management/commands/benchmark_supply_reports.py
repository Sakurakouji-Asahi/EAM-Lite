"""Generate repeatable disposable data and measure Sprint 18 report queries."""

from __future__ import annotations

import json
import math
import tempfile
import time
import tracemalloc
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.masterdata.models import (
    Company,
    Department,
    Employee,
    UserDepartmentScope,
)
from apps.reports.excel import write_report_workbook
from apps.reports.queries import build_report_dataset
from apps.reports.supply_queries import build_supply_dashboard
from apps.supplies.models import (
    EmployeeSupplyClearanceItem,
    SupplyCategory,
    SupplyCountLine,
    SupplyCountTask,
    SupplyCustody,
    SupplyCustodyMovement,
    SupplyDocument,
    SupplyDocumentLine,
    SupplyItem,
    SupplyStockBalance,
    SupplyStockLedger,
    SupplyWarehouse,
)


SAFE_DATABASE_MARKERS = ("s18", "perf", "performance", "uat", "test")
FORBIDDEN_DATABASES = {"eam_lite_sprint1_browser", "eam_lite", "postgres"}


class Command(BaseCommand):
    help = "仅在空的非生产 PostgreSQL 数据库生成可重复数据并测量低值物品报表。"

    def add_arguments(self, parser):
        parser.add_argument("--confirm-non-production", action="store_true")
        parser.add_argument(
            "--measure-existing",
            action="store_true",
            help="只测量本命令此前生成的 S18PERF 数据，不再次写入",
        )
        parser.add_argument("--items", type=int, default=10_000)
        parser.add_argument("--warehouses", type=int, default=20)
        parser.add_argument("--users", type=int, default=100)
        parser.add_argument("--ledgers", type=int, default=100_000)
        parser.add_argument("--custodies", type=int, default=10_000)
        parser.add_argument("--output", help="可选 JSON 结果文件路径")
        parser.add_argument(
            "--skip-excel",
            action="store_true",
            help="分级扩容时跳过已在 100k 基线完成的完整 XLSX 测量",
        )

    def _preflight(self, options):
        database_name = str(connection.settings_dict.get("NAME") or "")
        lowered = database_name.casefold()
        if not options["confirm_non_production"]:
            raise CommandError("默认不写入；必须显式提供 --confirm-non-production。")
        if connection.vendor != "postgresql":
            raise CommandError("性能验证只允许使用 PostgreSQL。")
        if lowered in FORBIDDEN_DATABASES or not any(
            marker in lowered for marker in SAFE_DATABASE_MARKERS
        ):
            raise CommandError("数据库名称未通过非生产白名单，已拒绝生成数据。")
        if options["measure_existing"]:
            if not Company.objects.filter(normalized_code="s18perf").exists():
                raise CommandError("未找到本命令生成的 S18PERF 数据。")
        elif Company.objects.exists():
            raise CommandError("性能数据库必须为空；检测到既有公司数据，已拒绝写入。")
        for key in ("items", "warehouses", "users", "ledgers", "custodies"):
            if options[key] <= 0:
                raise CommandError(f"--{key} 必须大于 0。")
        if options["ledgers"] < options["items"]:
            raise CommandError("流水数量不得少于物品数量。")
        return database_name

    def handle(self, *args, **options):
        database_name = self._preflight(options)
        started = time.perf_counter()
        if options["measure_existing"]:
            company = Company.objects.get(normalized_code="s18perf")
            actor = get_user_model().objects.filter(
                username__startswith="s18-perf-", groups__name="warehouse"
            ).first()
            if actor is None:
                raise CommandError("性能数据缺少 warehouse 测量用户。")
            context = {"company": company, "actor": actor}
            generated_seconds = 0.0
            options = {
                **options,
                "items": SupplyItem.objects.filter(company=company).count(),
                "warehouses": SupplyWarehouse.objects.filter(company=company).count(),
                "users": get_user_model().objects.filter(username__startswith="s18-perf-").count(),
                "ledgers": SupplyStockLedger.objects.filter(company=company).count(),
                "custodies": SupplyCustody.objects.filter(company=company).count(),
            }
        else:
            context = self._seed(options)
            generated_seconds = time.perf_counter() - started
        with connection.cursor() as cursor:
            cursor.execute("ANALYZE")
        actual_scale = {
            "items": SupplyItem.objects.filter(company=context["company"]).count(),
            "warehouses": SupplyWarehouse.objects.filter(company=context["company"]).count(),
            "users": get_user_model().objects.filter(username__startswith="s18-perf-").count(),
            "ledgers": SupplyStockLedger.objects.filter(company=context["company"]).count(),
            "custodies": SupplyCustody.objects.filter(company=context["company"]).count(),
        }
        metrics = self._measure(context, options)
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_database_size(current_database())")
            database_bytes = cursor.fetchone()[0]
        result = {
            "database": database_name,
            "generated_at": timezone.now().isoformat(),
            "generation_seconds": round(generated_seconds, 4),
            "database_bytes": database_bytes,
            "scale": actual_scale,
            "metrics": metrics,
        }
        serialized = json.dumps(result, ensure_ascii=False, indent=2)
        self.stdout.write(serialized)
        if options.get("output"):
            output_path = Path(options["output"]).resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(serialized + "\n", encoding="utf-8")

    def _seed(self, options):
        now = timezone.now()
        company = Company.objects.create(
            code="S18PERF",
            normalized_code="s18perf",
            name="Sprint 18 性能验证公司",
            short_name="S18PERF",
            is_active=True,
        )
        department = Department.objects.create(
            company=company,
            code="PERF-D",
            normalized_code="perf-d",
            name="性能验证部门",
        )
        category = SupplyCategory.objects.create(
            company=company,
            code="PERF-CAT",
            normalized_code="perf-cat",
            name="性能验证分类",
        )
        warehouses = [
            SupplyWarehouse(
                company=company,
                code=f"PW{index:03d}",
                normalized_code=f"pw{index:03d}",
                name=f"性能仓库 {index:03d}",
            )
            for index in range(options["warehouses"])
        ]
        SupplyWarehouse.objects.bulk_create(warehouses, batch_size=1000)

        roles = (
            "system_admin", "finance", "warehouse", "equipment",
            "management", "department_manager", "employee", "hr",
        )
        User = get_user_model()
        users = []
        for index in range(options["users"]):
            user = User(
                username=f"s18-perf-{index:03d}",
                display_name=f"性能用户 {index:03d}",
                is_active=True,
            )
            user.set_unusable_password()
            users.append(user)
        User.objects.bulk_create(users, batch_size=1000)
        groups = {name: Group.objects.get(name=name) for name in roles}
        through = User.groups.through
        through.objects.bulk_create(
            [
                through(user_id=user.pk, group_id=groups[roles[index % len(roles)]].pk)
                for index, user in enumerate(users)
            ],
            batch_size=1000,
        )
        employees = [
            Employee(
                company=company,
                department=department,
                user=user,
                employee_no=f"PE{index:04d}",
                normalized_employee_no=f"pe{index:04d}",
                name=f"性能员工 {index:04d}",
                employment_status="active",
                is_active=True,
            )
            for index, user in enumerate(users)
        ]
        Employee.objects.bulk_create(employees, batch_size=1000)
        admin = users[0]
        UserDepartmentScope.objects.bulk_create(
            [
                UserDepartmentScope(
                    company=company,
                    user=user,
                    department=department,
                    include_descendants=True,
                    is_active=True,
                    assigned_by=admin,
                )
                for index, user in enumerate(users)
                if roles[index % len(roles)] == "department_manager"
            ],
            batch_size=1000,
        )

        units = ("个", "箱", "把", "套", "公斤")
        items = []
        items_by_warehouse = [[] for _ in warehouses]
        for index in range(options["items"]):
            warehouse_index = index % len(warehouses)
            item = SupplyItem(
                company=company,
                item_code=f"PI{index:06d}",
                normalized_item_code=f"pi{index:06d}",
                name=f"性能物品 {index:06d}",
                category=category,
                item_type=("durable_quantity" if index % 2 else "consumable"),
                unit=units[index % len(units)],
                minimum_stock_quantity=Decimal("2.0000"),
                default_warehouse=warehouses[warehouse_index],
                is_active=True,
            )
            items.append(item)
            items_by_warehouse[warehouse_index].append(item)
        SupplyItem.objects.bulk_create(items, batch_size=2000)

        # Performance data is built only in a dedicated empty database. Custom
        # mutation triggers are temporarily bypassed for bulk loading; normal
        # report measurements and all application permissions remain enabled.
        with connection.cursor() as cursor:
            cursor.execute("SET session_replication_role = replica")
        try:
            with transaction.atomic():
                stock_context = self._seed_ledgers(
                    company=company,
                    actor=admin,
                    warehouses=warehouses,
                    items_by_warehouse=items_by_warehouse,
                    ledger_count=options["ledgers"],
                    now=now,
                )
                self._seed_custodies(
                    company=company,
                    actor=admin,
                    department=department,
                    employees=employees,
                    warehouses=warehouses,
                    durable_items_by_warehouse=[
                        [
                            item
                            for item in item_group
                            if item.item_type == "durable_quantity"
                        ]
                        for item_group in items_by_warehouse
                    ],
                    stock_context=stock_context,
                    custody_count=options["custodies"],
                    now=now,
                )
                self._seed_counts(
                    company=company,
                    actor=admin,
                    warehouse=warehouses[0],
                    balances=stock_context["balances_by_warehouse"][warehouses[0].pk],
                    now=now,
                )
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SET session_replication_role = origin")
        return {"company": company, "actor": users[2]}

    def _seed_ledgers(self, *, company, actor, warehouses, items_by_warehouse, ledger_count, now):
        per_warehouse = [ledger_count // len(warehouses)] * len(warehouses)
        for index in range(ledger_count % len(warehouses)):
            per_warehouse[index] += 1
        documents_by_warehouse = {}
        for warehouse_index, warehouse in enumerate(warehouses):
            document_count = math.ceil(per_warehouse[warehouse_index] / 100)
            documents = [
                SupplyDocument(
                    company=company,
                    document_no=f"PR-{warehouse_index:03d}-{index:06d}",
                    document_type="receipt",
                    business_date=date(2026, 1, 1) + timedelta(days=index % 240),
                    target_warehouse=warehouse,
                    status="posted",
                    idempotency_key=f"perf-receipt-{warehouse_index}-{index}",
                    created_by=actor,
                    posted_by=actor,
                    posted_at=now,
                )
                for index in range(document_count)
            ]
            SupplyDocument.objects.bulk_create(documents, batch_size=1000)
            documents_by_warehouse[warehouse.pk] = documents

        totals = {item.pk: 0 for group in items_by_warehouse for item in group}
        balances_by_warehouse = defaultdict(list)
        for warehouse_index, warehouse in enumerate(warehouses):
            item_group = items_by_warehouse[warehouse_index]
            documents = documents_by_warehouse[warehouse.pk]
            remaining = per_warehouse[warehouse_index]
            offset = 0
            while remaining:
                size = min(5000, remaining)
                lines = []
                ledgers = []
                for local in range(size):
                    sequence = offset + local
                    item = item_group[sequence % len(item_group)]
                    before = totals[item.pk]
                    totals[item.pk] += 1
                    document = documents[sequence // 100]
                    line = SupplyDocumentLine(
                        company=company,
                        document=document,
                        line_no=sequence % 100 + 1,
                        item=item,
                        quantity=Decimal("1.0000"),
                        entered_unit_cost=Decimal("10.000000"),
                        posted_unit_cost=Decimal("10.000000"),
                        posted_amount=Decimal("10.00"),
                    )
                    lines.append(line)
                    ledgers.append(
                        SupplyStockLedger(
                            company=company,
                            warehouse=warehouse,
                            item=item,
                            document=document,
                            document_line=line,
                            movement_type="receipt_in",
                            quantity_delta=Decimal("1.0000"),
                            amount_delta=Decimal("10.00"),
                            unit_cost=Decimal("10.000000"),
                            quantity_before=Decimal(before).quantize(Decimal("0.0001")),
                            quantity_after=Decimal(before + 1).quantize(Decimal("0.0001")),
                            amount_before=Decimal(before * 10).quantize(Decimal("0.01")),
                            amount_after=Decimal((before + 1) * 10).quantize(Decimal("0.01")),
                            average_unit_cost_before=(Decimal("0.000000") if before == 0 else Decimal("10.000000")),
                            average_unit_cost_after=Decimal("10.000000"),
                            occurred_at=now,
                            created_by=actor,
                        )
                    )
                SupplyDocumentLine.objects.bulk_create(lines, batch_size=2000)
                SupplyStockLedger.objects.bulk_create(ledgers, batch_size=2000)
                offset += size
                remaining -= size
        balances = []
        for warehouse_index, warehouse in enumerate(warehouses):
            for item in items_by_warehouse[warehouse_index]:
                quantity = totals[item.pk]
                balance = SupplyStockBalance(
                    company=company,
                    warehouse=warehouse,
                    item=item,
                    quantity_on_hand=Decimal(quantity).quantize(Decimal("0.0001")),
                    amount_on_hand=Decimal(quantity * 10).quantize(Decimal("0.01")),
                    average_unit_cost=Decimal("10.000000"),
                )
                balances.append(balance)
                balances_by_warehouse[warehouse.pk].append(balance)
        SupplyStockBalance.objects.bulk_create(balances, batch_size=2000)
        return {
            "balances_by_warehouse": balances_by_warehouse,
            "balance_by_item": {balance.item_id: balance for balance in balances},
            "totals": totals,
        }

    def _seed_custodies(
        self,
        *,
        company,
        actor,
        department,
        employees,
        warehouses,
        durable_items_by_warehouse,
        stock_context,
        custody_count,
        now,
    ):
        durable_pairs = [
            (warehouse, durable_items_by_warehouse[index])
            for index, warehouse in enumerate(warehouses)
            if durable_items_by_warehouse[index]
        ]
        if not durable_pairs:
            raise CommandError("性能数据至少需要一个数量型耐用品。")
        durable_by_warehouse_id = {
            warehouse.pk: item_group for warehouse, item_group in durable_pairs
        }
        documents = [
            SupplyDocument(
                company=company,
                document_no=f"PIssue-{index:06d}",
                document_type="issue",
                business_date=date(2026, 8, 1),
                source_warehouse=durable_pairs[index % len(durable_pairs)][0],
                department=department,
                employee=employees[index % len(employees)],
                status="posted",
                idempotency_key=f"perf-issue-{index}",
                created_by=actor,
                posted_by=actor,
                posted_at=now,
            )
            for index in range(math.ceil(custody_count / 100))
        ]
        SupplyDocument.objects.bulk_create(documents, batch_size=1000)
        changed_item_ids = set()
        for offset in range(0, custody_count, 2000):
            size = min(2000, custody_count - offset)
            lines = []
            custodies = []
            for local in range(size):
                index = offset + local
                document = documents[index // 100]
                item_group = durable_by_warehouse_id[document.source_warehouse_id]
                item = item_group[index % len(item_group)]
                line = SupplyDocumentLine(
                    company=company,
                    document=document,
                    line_no=index % 100 + 1,
                    item=item,
                    quantity=Decimal("1.0000"),
                    posted_unit_cost=Decimal("10.000000"),
                    posted_amount=Decimal("10.00"),
                )
                lines.append(line)
                closed = index % 2 == 1
                custodies.append(
                    SupplyCustody(
                        company=company,
                        item=item,
                        origin_issue_line=line,
                        department=department,
                        employee=employees[index % len(employees)],
                        current_quantity=Decimal("0.0000" if closed else "1.0000"),
                        current_amount=Decimal("0.00" if closed else "10.00"),
                        unit_cost_snapshot=Decimal("10.000000"),
                        started_on=date(2026, 8, 1),
                        status="closed" if closed else "open",
                    )
                )
            SupplyDocumentLine.objects.bulk_create(lines, batch_size=2000)
            SupplyCustody.objects.bulk_create(custodies, batch_size=2000)
            movements = []
            ledgers = []
            for index, custody in enumerate(custodies, start=offset):
                document = custody.origin_issue_line.document
                warehouse = document.source_warehouse
                before = stock_context["totals"][custody.item_id]
                if before <= 0:
                    raise CommandError("性能数据领用超过合成库存。")
                after = before - 1
                stock_context["totals"][custody.item_id] = after
                changed_item_ids.add(custody.item_id)
                ledgers.append(
                    SupplyStockLedger(
                        company=company,
                        warehouse=warehouse,
                        item=custody.item,
                        document=document,
                        document_line=custody.origin_issue_line,
                        movement_type="issue_out",
                        quantity_delta=Decimal("-1.0000"),
                        amount_delta=Decimal("-10.00"),
                        unit_cost=Decimal("10.000000"),
                        quantity_before=Decimal(before).quantize(Decimal("0.0001")),
                        quantity_after=Decimal(after).quantize(Decimal("0.0001")),
                        amount_before=Decimal(before * 10).quantize(Decimal("0.01")),
                        amount_after=Decimal(after * 10).quantize(Decimal("0.01")),
                        average_unit_cost_before=Decimal("10.000000"),
                        average_unit_cost_after=(
                            Decimal("10.000000")
                            if after
                            else Decimal("0.000000")
                        ),
                        occurred_at=now,
                        created_by=actor,
                    )
                )
                movements.append(
                    SupplyCustodyMovement(
                        company=company,
                        item=custody.item,
                        to_custody=custody,
                        action="issue",
                        quantity=Decimal("1.0000"),
                        amount=Decimal("10.00"),
                        unit_cost=Decimal("10.000000"),
                        business_date=date(2026, 8, 1),
                        source_document_line=custody.origin_issue_line,
                        created_by=actor,
                    )
                )
                if custody.status == "closed":
                    movements.append(
                        SupplyCustodyMovement(
                            company=company,
                            item=custody.item,
                            from_custody=custody,
                            action="return",
                            quantity=Decimal("1.0000"),
                            amount=Decimal("10.00"),
                            unit_cost=Decimal("10.000000"),
                            business_date=date(2026, 8, 2),
                            created_by=actor,
                        )
                    )
            SupplyCustodyMovement.objects.bulk_create(movements, batch_size=2000)
            SupplyStockLedger.objects.bulk_create(ledgers, batch_size=2000)
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                UPDATE supplies_supplystockbalance
                   SET quantity_on_hand=%s, amount_on_hand=%s,
                       average_unit_cost=%s, updated_at=%s
                 WHERE id=%s
                """,
                [
                    (
                        Decimal(stock_context["totals"][item_id]).quantize(
                            Decimal("0.0001")
                        ),
                        Decimal(stock_context["totals"][item_id] * 10).quantize(
                            Decimal("0.01")
                        ),
                        (
                            Decimal("10.000000")
                            if stock_context["totals"][item_id]
                            else Decimal("0.000000")
                        ),
                        now,
                        stock_context["balance_by_item"][item_id].pk,
                    )
                    for item_id in changed_item_ids
                ],
            )

    def _seed_counts(self, *, company, actor, warehouse, balances, now):
        sample = balances[: min(100, len(balances))]
        tasks = [
            SupplyCountTask(
                company=company,
                task_no=f"PC-{index:04d}",
                name=f"性能盘点 {index:04d}",
                count_domain="warehouse_stock",
                warehouse=warehouse,
                planned_start=date(2026, 8, 1),
                planned_end=date(2026, 8, 2),
                snapshot_at=now,
                status="closed",
                idempotency_key=f"perf-count-{index}",
                created_by=actor,
                published_by=actor,
                published_at=now,
                stopped_by=actor,
                stopped_at=now,
                closed_by=actor,
                closed_at=now,
            )
            for index in range(100)
        ]
        SupplyCountTask.objects.bulk_create(tasks, batch_size=1000)
        lines = []
        for task in tasks:
            for balance in sample:
                lines.append(
                    SupplyCountLine(
                        company=company,
                        count_task=task,
                        item=balance.item,
                        stock_balance=balance,
                        item_code_snapshot=balance.item.item_code,
                        item_name_snapshot=balance.item.name,
                        expected_quantity=balance.quantity_on_hand,
                        expected_amount=balance.amount_on_hand,
                        expected_unit_cost=balance.average_unit_cost,
                        counted_quantity=balance.quantity_on_hand,
                        difference_quantity=Decimal("0.0000"),
                        counted_by=actor,
                        counted_at=now,
                    )
                )
        SupplyCountLine.objects.bulk_create(lines, batch_size=2000)

    def _measure(self, context, options):
        actor = context["actor"]
        company = context["company"]
        period = {"date_from": date(2026, 1, 1), "date_to": date(2026, 12, 31)}
        cases = (
            ("dashboard", lambda: build_supply_dashboard(actor=actor, company=company)),
            ("item_list", lambda: list(SupplyItem.objects.filter(company=company).order_by("normalized_item_code")[:50])),
            ("stock_balance", lambda: list(build_report_dataset(actor=actor, company=company, report_key="supply_stock_balance", filters={}).rows[:50])),
            ("low_stock", lambda: list(build_report_dataset(actor=actor, company=company, report_key="supply_low_stock", filters={"low_stock_scope": "formal"}).rows[:50])),
            ("stock_ledger", lambda: list(build_report_dataset(actor=actor, company=company, report_key="supply_stock_ledger", filters=period).rows[:50])),
            ("stock_movement", lambda: list(build_report_dataset(actor=actor, company=company, report_key="supply_stock_movement", filters=period).rows[:50])),
            ("issue_summary", lambda: list(build_report_dataset(actor=actor, company=company, report_key="supply_department_issue", filters=period).rows[:50])),
            ("custody_balance", lambda: list(build_report_dataset(actor=actor, company=company, report_key="supply_custody_balance", filters={}).rows[:50])),
            ("custody_movement", lambda: list(build_report_dataset(actor=actor, company=company, report_key="supply_custody_movement", filters={"date_from": date(2026, 8, 1), "date_to": date(2026, 8, 31)}).rows[:50])),
            ("count_difference", lambda: list(build_report_dataset(actor=actor, company=company, report_key="supply_count_difference", filters={}).rows[:50])),
        )
        metrics = {}
        for name, operation in cases:
            started = time.perf_counter()
            with CaptureQueriesContext(connection) as captured:
                value = operation()
            metrics[name] = {
                "seconds": round(time.perf_counter() - started, 4),
                "queries": len(captured),
                "rows_or_keys": len(value) if hasattr(value, "__len__") else None,
            }
        if not options["skip_excel"]:
            dataset = build_report_dataset(
                actor=actor,
                company=company,
                report_key="supply_stock_ledger",
                filters=period,
            )
            tracemalloc.start()
            started = time.perf_counter()
            with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as output:
                output_path = Path(output.name)
            try:
                write_report_workbook(dataset, output_path)
                export_bytes = output_path.stat().st_size
            finally:
                output_path.unlink(missing_ok=True)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            metrics["stock_ledger_excel"] = {
                "seconds": round(time.perf_counter() - started, 4),
                "queries": None,
                "rows": dataset.row_count,
                "bytes": export_bytes,
                "peak_memory_bytes": peak,
            }
        explain = SupplyStockLedger.objects.filter(
            company=company,
            document__business_date__gte=period["date_from"],
            document__business_date__lte=period["date_to"],
        ).order_by("-occurred_at").explain(analyze=True, buffers=True)
        metrics["stock_ledger_explain"] = explain.splitlines()[:24]
        return metrics
