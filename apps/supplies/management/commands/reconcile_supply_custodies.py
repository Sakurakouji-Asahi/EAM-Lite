from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError

from apps.masterdata.models import Company
from apps.masterdata.normalization import normalize_identifier
from apps.supplies.domain import ZERO_MONEY, ZERO_QTY, quantize_money, quantize_quantity
from apps.supplies.models import SupplyCustody, SupplyCustodyMovement


class Command(BaseCommand):
    help = "只读核对耐用品保管流水与保管余额缓存；不会修复或写入数据。"

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True, help="公司编码")

    def handle(self, *args, **options):
        company = Company.objects.filter(
            normalized_code=normalize_identifier(options["company"])
        ).first()
        if company is None:
            raise CommandError("未找到指定公司。")

        totals = defaultdict(
            lambda: {"quantity": ZERO_QTY, "amount": ZERO_MONEY, "count": 0}
        )
        integrity_errors = []
        custodies = SupplyCustody.objects.filter(company=company).select_related(
            "item",
            "department",
            "employee",
            "origin_issue_line",
            "origin_import_row__batch",
            "parent_custody",
        ).order_by("item__normalized_item_code", "started_on", "pk")
        custody_map = {custody.pk: custody for custody in custodies}
        movements = SupplyCustodyMovement.objects.filter(company=company).select_related(
            "from_custody", "to_custody", "reverses_movement"
        ).order_by("created_at", "pk")
        for movement in movements.iterator():
            if movement.from_custody_id is not None:
                source = custody_map.get(movement.from_custody_id)
                if source is None or source.item_id != movement.item_id:
                    integrity_errors.append(f"流水 {movement.pk} 的转出保管公司或物品不一致")
            if movement.to_custody_id is not None:
                target = custody_map.get(movement.to_custody_id)
                if target is None or target.item_id != movement.item_id:
                    integrity_errors.append(f"流水 {movement.pk} 的转入保管公司或物品不一致")
            if movement.action in {"issue", "opening"}:
                shape_valid = movement.from_custody_id is None and movement.to_custody_id is not None
            elif movement.action in {"return", "loss", "scrap"}:
                shape_valid = movement.from_custody_id is not None and movement.to_custody_id is None
            elif movement.action == "transfer":
                shape_valid = (
                    movement.from_custody_id is not None
                    and movement.to_custody_id is not None
                    and movement.from_custody_id != movement.to_custody_id
                )
            elif movement.action == "reversal":
                original = movement.reverses_movement
                shape_valid = bool(
                    original
                    and movement.from_custody_id == original.to_custody_id
                    and movement.to_custody_id == original.from_custody_id
                    and movement.quantity == original.quantity
                    and movement.amount == original.amount
                    and movement.unit_cost == original.unit_cost
                )
            else:
                shape_valid = movement.action == "correction"
            if not shape_valid:
                integrity_errors.append(f"流水 {movement.pk} 的动作方向或冲销快照不正确")
            if movement.to_custody_id is not None:
                summary = totals[movement.to_custody_id]
                summary["quantity"] = quantize_quantity(
                    summary["quantity"] + movement.quantity
                )
                summary["amount"] = quantize_money(
                    summary["amount"] + movement.amount
                )
                summary["count"] += 1
            if movement.from_custody_id is not None:
                summary = totals[movement.from_custody_id]
                summary["quantity"] = quantize_quantity(
                    summary["quantity"] - movement.quantity
                )
                summary["amount"] = quantize_money(
                    summary["amount"] - movement.amount
                )
                summary["count"] += 1

        differences = []
        for custody in custodies:
            if custody.parent_custody_id:
                source_valid = bool(
                    custody.origin_issue_line_id is None
                    and custody.origin_import_row_id is None
                    and custody.parent_custody_id != custody.pk
                    and custody.parent_custody.company_id == custody.company_id
                    and custody.parent_custody.item_id == custody.item_id
                )
            else:
                source_valid = bool(custody.origin_issue_line_id) != bool(
                    custody.origin_import_row_id
                )
            if not source_valid:
                integrity_errors.append(f"保管 {custody.pk} 的根来源或父子关系不正确")
            summary = totals[custody.pk]
            expected_status = (
                "closed"
                if summary["quantity"] == ZERO_QTY
                and summary["amount"] == ZERO_MONEY
                else "open"
                if summary["quantity"] > ZERO_QTY
                and summary["amount"] >= ZERO_MONEY
                else "invalid"
            )
            if (
                custody.current_quantity != summary["quantity"]
                or custody.current_amount != summary["amount"]
                or custody.status != expected_status
            ):
                differences.append((custody, summary, expected_status))

        if not differences and not integrity_errors:
            self.stdout.write(
                self.style.SUCCESS(
                    f"公司 {company.code} 的保管余额与保管流水一致（核对 {custodies.count()} 条保管记录）。"
                )
            )
            return

        self.stdout.write(
            self.style.ERROR(
                f"公司 {company.code} 发现 {len(differences)} 条保管余额差异、{len(integrity_errors)} 条关系差异："
            )
        )
        for message in integrity_errors:
            self.stdout.write(message)
        for custody, summary, expected_status in differences:
            self.stdout.write(
                " | ".join(
                    (
                        f"保管ID={custody.pk}",
                        f"物品={custody.item.item_code} / {custody.item.name}",
                        f"部门={custody.department}",
                        f"员工={custody.employee or '部门保管'}",
                        f"流水数量={summary['quantity']}",
                        f"流水金额={summary['amount']}",
                        f"流水数={summary['count']}",
                        f"期望状态={expected_status}",
                        f"缓存数量={custody.current_quantity}",
                        f"缓存金额={custody.current_amount}",
                        f"缓存状态={custody.status}",
                    )
                )
            )
        raise CommandError("保管余额核对失败；本命令未修改任何数据。")
