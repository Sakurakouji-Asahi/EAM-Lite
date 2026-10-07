"""Download the non-financial snapshot and current evidence of an inventory task."""

from __future__ import annotations

import csv

from django.utils import timezone

from apps.assets.models import Asset


HEADERS = (
    "任务编号", "任务名称", "任务状态", "快照基准时间", "资产编号（快照）", "资产名称（快照）",
    "实物分类（快照）", "部门（快照）", "责任人（快照）", "位置（快照）", "资产状态（快照）",
    "设备编号（当前）", "盘点处理状态", "有效现场结果", "现场位置", "现场责任人", "现场资产状态",
    "有效扫码时间", "扫码方式", "现场说明", "处理结论类型", "当前处理结论", "处理时间",
)
STATUS_LABELS = dict(Asset.AssetStatus.choices)


class _CsvBuffer:
    def write(self, value):
        return value


def _safe_cell(value):
    """Keep untrusted names/notes from becoming formulas when opened in Excel."""
    text = "" if value is None else str(value)
    candidate = text.lstrip(" \t\r\n\ufeff")
    if candidate.startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def _date_time(value):
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M:%S") if value else ""


def inventory_result_csv(task, rows):
    """Stream all matching rows, regardless of the current screen page."""
    writer = csv.writer(_CsvBuffer())
    yield "\ufeff"
    yield writer.writerow(HEADERS)
    for row in rows.iterator(chunk_size=200):
        scan = next((item for item in row.scans.all() if item.is_effective), None)
        resolution = next((item for item in row.resolutions.all() if item.status == "active"), None)
        values = (
            task.task_code, task.name, task.get_status_display(), _date_time(task.snapshot_at),
            row.expected_code_snapshot, row.expected_name_snapshot, row.expected_category_snapshot,
            row.expected_department_snapshot, row.expected_employee_snapshot, row.expected_location_path_snapshot,
            STATUS_LABELS.get(row.expected_asset_status, row.expected_asset_status), row.asset.equipment_number,
            row.get_inventory_status_display(), scan.get_result_display() if scan else "未盘",
            scan.actual_location.name if scan and scan.actual_location else "",
            scan.actual_employee.name if scan and scan.actual_employee else "",
            STATUS_LABELS.get(scan.actual_status, scan.actual_status) if scan else "",
            _date_time(scan.scanned_at) if scan else "", scan.get_scan_mode_display() if scan else "",
            scan.note if scan else "", resolution.get_resolution_type_display() if resolution else "",
            resolution.conclusion if resolution else "", _date_time(resolution.resolved_at) if resolution else "",
        )
        yield writer.writerow(_safe_cell(value) for value in values)
