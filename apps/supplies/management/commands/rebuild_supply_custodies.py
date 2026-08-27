from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand

from apps.supplies.management.commands._rebuild_supply_base import (
    add_rebuild_arguments,
    resolve_rebuild_context,
    validation_as_command_error,
    write_result,
)
from apps.supplies.reconciliation import rebuild_custodies


class Command(BaseCommand):
    help = "从不可变保管流水 dry-run 核对或受控重建保管余额缓存。"

    def add_arguments(self, parser):
        add_rebuild_arguments(parser)

    def handle(self, *args, **options):
        company, actor, reason = resolve_rebuild_context(options)
        try:
            result = rebuild_custodies(
                company=company,
                actor=actor,
                reason=reason,
                confirm=options["confirm"],
            )
        except ValidationError as exc:
            raise validation_as_command_error(exc) from exc
        write_result(self, result, confirm=options["confirm"])
