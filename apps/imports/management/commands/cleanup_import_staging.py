from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.imports.cleanup import (
    abandon_validated_batch,
    cleanup_import_batches,
    cleanup_legacy_temp_files,
    cleanup_orphan_attachments,
    cleanup_unreferenced_private_files,
)


class Command(BaseCommand):
    help = "Dry-run by default: clean expired import staging and private orphan files."

    def add_arguments(self, parser):
        parser.add_argument("--actor", required=True)
        parser.add_argument("--task-id", default="manual")
        parser.add_argument("--execute", action="store_true")
        parser.add_argument("--batch-retention-days", type=int, default=30)
        parser.add_argument("--orphan-retention-days", type=int)
        parser.add_argument("--temp-older-than-hours", type=int)
        parser.add_argument("--unreferenced-private-days", type=int)
        parser.add_argument("--abandon-validated", type=int, metavar="BATCH_ID")
        parser.add_argument("--reason")

    def handle(self, *args, **options):
        actor = get_user_model().objects.filter(username=options["actor"]).first()
        if actor is None:
            raise CommandError("未找到 --actor 指定的用户。")
        dry_run = not options["execute"]
        try:
            if options["abandon_validated"] is not None:
                if not options["reason"]:
                    raise CommandError("--abandon-validated 必须同时提供 --reason。")
                changed = abandon_validated_batch(
                    actor=actor,
                    batch_id=options["abandon_validated"],
                    reason=options["reason"],
                    dry_run=dry_run,
                    task_id=options["task_id"],
                )
                self.stdout.write(
                    f"mode={'dry-run' if dry_run else 'execute'} abandon={changed}"
                )
                return

            reports = [
                cleanup_import_batches(
                    actor=actor,
                    retention_days=options["batch_retention_days"],
                    dry_run=dry_run,
                    task_id=options["task_id"],
                )
            ]
            if options["orphan_retention_days"] is not None:
                reports.append(
                    cleanup_orphan_attachments(
                        actor=actor,
                        orphan_retention_days=options["orphan_retention_days"],
                        dry_run=dry_run,
                        task_id=options["task_id"],
                    )
                )
            if options["temp_older_than_hours"] is not None:
                reports.append(
                    cleanup_legacy_temp_files(
                        actor=actor,
                        older_than_hours=options["temp_older_than_hours"],
                        dry_run=dry_run,
                        task_id=options["task_id"],
                    )
                )
            if options["unreferenced_private_days"] is not None:
                reports.append(
                    cleanup_unreferenced_private_files(
                        actor=actor,
                        older_than_days=options["unreferenced_private_days"],
                        dry_run=dry_run,
                        task_id=options["task_id"],
                        private_prefixes=(
                            "private/imports",
                            "private/assets",
                            "private/inventory",
                        ),
                    )
                )
        except (PermissionDenied, ValidationError) as exc:
            messages = getattr(exc, "messages", None) or [str(exc)]
            raise CommandError("；".join(messages)) from exc
        for report in reports:
            self.stdout.write(
                "mode={} batch_candidates={} batches={} attachments={} temp_files={} skipped={}".format(
                    "dry-run" if report.dry_run else "execute",
                    report.batch_candidates,
                    len(report.batches_deleted),
                    len(report.attachments_deleted),
                    len(report.legacy_files_deleted),
                    len(report.batches_skipped)
                    + len(report.attachments_skipped)
                    + len(report.legacy_files_skipped),
                )
            )
