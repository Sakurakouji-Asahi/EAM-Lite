from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import CommandError

from apps.masterdata.models import Company, Employee
from apps.masterdata.normalization import normalize_identifier
from apps.masterdata.permissions import role_names_for


def add_rebuild_arguments(parser):
    parser.add_argument("--company", required=True, help="公司编码")
    parser.add_argument("--actor", required=True, help="同公司启用操作人用户名")
    parser.add_argument("--reason", required=True, help="核对并重建原因")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="确认在事务内写入；省略时只执行 dry-run",
    )


def resolve_rebuild_context(options):
    company = Company.objects.filter(
        normalized_code=normalize_identifier(options["company"])
    ).first()
    if company is None:
        raise CommandError("未找到指定公司。")
    actor = get_user_model().objects.filter(
        username=options["actor"], is_active=True
    ).first()
    if actor is None:
        raise CommandError("未找到指定的启用操作人。")
    if not role_names_for(actor).intersection({"system_admin", "finance"}):
        raise CommandError("余额重建操作人必须具有 system_admin 或 finance 角色。")
    employee_links = Employee.objects.filter(user=actor)
    if employee_links.exists() and not employee_links.filter(company=company).exists():
        raise CommandError("余额重建操作人不属于指定公司。")
    reason = str(options.get("reason") or "").strip()
    if not reason:
        raise CommandError("必须填写重建原因。")
    return company, actor, reason


def write_result(command, result, *, confirm):
    mode = "confirm" if confirm else "dry-run"
    command.stdout.write(
        f"模式={mode} | 核对数={result.checked_count} | "
        f"差异数={len(result.differences)} | 关系错误={len(result.integrity_errors)}"
    )
    for message in result.integrity_errors:
        command.stdout.write(command.style.ERROR(message))
    for difference in result.differences:
        current = difference["current"]
        expected = difference["expected"]
        object_label = (
            f"仓库={difference['warehouse']} | 物品={difference['item']}"
            if result.kind == "stock"
            else f"保管ID={difference['custody_id']} | 物品={difference['item']}"
        )
        command.stdout.write(
            f"{object_label} | 当前={current} | 流水重建={expected}"
        )
    if result.integrity_errors:
        raise CommandError("流水存在完整性错误，不能重建缓存。")
    if confirm:
        command.stdout.write(command.style.SUCCESS("受控重建完成，重建后核对一致。"))
    elif result.differences:
        command.stdout.write("dry-run 已完成；未修改任何数据。")
    else:
        command.stdout.write(command.style.SUCCESS("dry-run 核对一致；未修改任何数据。"))


def validation_as_command_error(exc):
    if isinstance(exc, ValidationError):
        return CommandError("；".join(exc.messages))
    return exc
