from django.core.management.base import BaseCommand, CommandError

from apps.masterdata.models import Company
from apps.masterdata.normalization import normalize_identifier
from apps.supplies.reconciliation import reconcile_custodies


class Command(BaseCommand):
    help = "只读核对耐用品保管来源链、流水与余额缓存；不会修复或写入数据。"

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True, help="公司编码")

    def handle(self, *args, **options):
        company = Company.objects.filter(
            normalized_code=normalize_identifier(options["company"])
        ).first()
        if company is None:
            raise CommandError("未找到指定公司。")

        result = reconcile_custodies(company=company)
        if result.is_consistent:
            self.stdout.write(
                self.style.SUCCESS(
                    f"公司 {company.code} 的保管来源链、流水与余额一致"
                    f"（核对 {result.checked_count} 条保管记录）。"
                )
            )
            return

        self.stdout.write(
            self.style.ERROR(
                f"公司 {company.code} 发现 {len(result.differences)} 条保管余额差异、"
                f"{len(result.integrity_errors)} 条来源/流水完整性错误："
            )
        )
        for message in result.integrity_errors:
            self.stdout.write(message)
        for difference in result.differences:
            current = difference["current"]
            expected = difference["expected"]
            self.stdout.write(
                " | ".join(
                    (
                        f"保管ID={difference['custody_id']}",
                        f"物品={difference['item']}",
                        f"部门={difference['department']}",
                        f"员工={difference['employee'] or '部门保管'}",
                        f"流水数量={expected['quantity']}",
                        f"流水金额={expected['amount']}",
                        f"期望状态={expected['status']}",
                        f"缓存数量={current['quantity']}",
                        f"缓存金额={current['amount']}",
                        f"缓存状态={current['status']}",
                    )
                )
            )
        raise CommandError("保管完整性核对失败；本命令未修改任何数据。")
