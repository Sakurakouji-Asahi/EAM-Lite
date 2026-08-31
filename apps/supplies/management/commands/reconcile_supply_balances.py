from django.core.management.base import BaseCommand, CommandError

from apps.masterdata.models import Company
from apps.masterdata.normalization import normalize_identifier
from apps.supplies.reconciliation import reconcile_stock_balances


class Command(BaseCommand):
    help = "只读核对低值物品单据、库存流水与余额缓存；不会修复或写入数据。"

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True, help="公司编码")

    def handle(self, *args, **options):
        company_code = normalize_identifier(options["company"])
        company = Company.objects.filter(normalized_code=company_code).first()
        if company is None:
            raise CommandError("未找到指定公司。")

        result = reconcile_stock_balances(company=company)
        if result.is_consistent:
            self.stdout.write(
                self.style.SUCCESS(
                    f"公司 {company.code} 的库存单据、流水与余额一致"
                    f"（核对 {result.checked_count} 个仓库/物品组合）。"
                )
            )
            return

        self.stdout.write(
            self.style.ERROR(
                f"公司 {company.code} 发现 {len(result.differences)} 个库存余额差异、"
                f"{len(result.integrity_errors)} 条流水/单据完整性错误："
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
                        f"仓库={difference['warehouse']}",
                        f"物品={difference['item']}",
                        f"流水数量={expected['quantity']}",
                        f"流水金额={expected['amount']}",
                        f"流水平均={expected['average'] if expected['average'] is not None else '无效'}",
                        f"余额数量={current['quantity']}",
                        f"余额金额={current['amount']}",
                        f"余额平均={current['average']}",
                    )
                )
            )
        raise CommandError("库存完整性核对失败；本命令未修改任何数据。")
