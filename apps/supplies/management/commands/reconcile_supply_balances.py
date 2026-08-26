from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Sum

from apps.masterdata.models import Company
from apps.masterdata.normalization import normalize_identifier
from apps.supplies.domain import (
    ZERO_MONEY,
    ZERO_QTY,
    calculate_average_unit_cost,
    quantize_money,
    quantize_quantity,
)
from apps.supplies.models import SupplyStockBalance, SupplyStockLedger


class Command(BaseCommand):
    help = "只读核对低值物品库存流水汇总与余额缓存；不会修复或写入数据。"

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True, help="公司编码")

    def handle(self, *args, **options):
        company_code = normalize_identifier(options["company"])
        company = Company.objects.filter(normalized_code=company_code).first()
        if company is None:
            raise CommandError("未找到指定公司。")

        ledger_totals = {
            (row["warehouse_id"], row["item_id"]): {
                "quantity": quantize_quantity(row["ledger_quantity"] or ZERO_QTY),
                "amount": quantize_money(row["ledger_amount"] or ZERO_MONEY),
                "warehouse_label": f"{row['warehouse__code']} / {row['warehouse__name']}",
                "item_label": f"{row['item__item_code']} / {row['item__name']}",
            }
            for row in SupplyStockLedger.objects.filter(company=company)
            .values(
                "warehouse_id",
                "item_id",
                "warehouse__code",
                "warehouse__name",
                "item__item_code",
                "item__name",
            )
            .annotate(
                ledger_quantity=Sum("quantity_delta"),
                ledger_amount=Sum("amount_delta"),
            )
        }
        balances = {
            (balance.warehouse_id, balance.item_id): balance
            for balance in SupplyStockBalance.objects.filter(company=company)
            .select_related("warehouse", "item")
            .order_by("warehouse__normalized_code", "item__normalized_item_code")
        }
        keys = sorted(
            set(ledger_totals) | set(balances),
            key=lambda value: (str(value[0]), str(value[1])),
        )
        differences = []
        for key in keys:
            ledger_summary = ledger_totals.get(key)
            ledger_quantity = (
                ledger_summary["quantity"] if ledger_summary else ZERO_QTY
            )
            ledger_amount = ledger_summary["amount"] if ledger_summary else ZERO_MONEY
            balance = balances.get(key)
            balance_quantity = (
                balance.quantity_on_hand if balance is not None else ZERO_QTY
            )
            balance_amount = balance.amount_on_hand if balance is not None else ZERO_MONEY
            balance_average = (
                balance.average_unit_cost
                if balance is not None
                else Decimal("0.000000")
            )
            try:
                ledger_average = calculate_average_unit_cost(
                    ledger_quantity, ledger_amount
                )
            except ValidationError:
                ledger_average = None
            if (
                balance_quantity != ledger_quantity
                or balance_amount != ledger_amount
                or ledger_average is None
                or balance_average != ledger_average
            ):
                differences.append(
                    (
                        key,
                        balance,
                        ledger_quantity,
                        ledger_amount,
                        ledger_average,
                        balance_quantity,
                        balance_amount,
                        balance_average,
                        ledger_summary,
                    )
                )

        if not differences:
            self.stdout.write(
                self.style.SUCCESS(
                    f"公司 {company.code} 的库存余额与流水汇总一致（核对 {len(keys)} 个仓库/物品组合）。"
                )
            )
            return

        self.stdout.write(
            self.style.ERROR(
                f"公司 {company.code} 发现 {len(differences)} 个库存余额差异："
            )
        )
        for (
            key,
            balance,
            ledger_quantity,
            ledger_amount,
            ledger_average,
            balance_quantity,
            balance_amount,
            balance_average,
            ledger_summary,
        ) in differences:
            warehouse = (
                balance.warehouse
                if balance
                else ledger_summary["warehouse_label"]
            )
            item = balance.item if balance else ledger_summary["item_label"]
            self.stdout.write(
                " | ".join(
                    (
                        f"仓库={warehouse}",
                        f"物品={item}",
                        f"流水数量={ledger_quantity}",
                        f"流水金额={ledger_amount}",
                        f"流水平均={ledger_average if ledger_average is not None else '无效'}",
                        f"余额数量={balance_quantity}",
                        f"余额金额={balance_amount}",
                        f"余额平均={balance_average}",
                    )
                )
            )
        raise CommandError("库存余额核对失败；本命令未修改任何数据。")
