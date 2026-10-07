"""Evidence comparison and navigation for a single inventory difference."""
from urllib.parse import urlencode, urlsplit

from django.urls import reverse

from apps.assets.form_options import location_path
from apps.assets.models import Asset


def difference_return_url(request, task):
    fallback = reverse("inventory:task-detail", args=[task.pk])
    value = request.POST.get("return_to", request.GET.get("return_to", ""))
    if not value or len(value) > 3000 or "\\" in value or any(ord(char) < 32 for char in value):
        return fallback
    try:
        parts = urlsplit(value)
        if (not parts.scheme and not parts.netloc and parts.path == fallback
                and parts.fragment in ("", "inventory-results")):
            return value
    except ValueError:
        pass
    return fallback


def difference_return_query(request, task):
    return urlencode({"return_to": difference_return_url(request, task)})


def difference_evidence(row):
    scan = row.scans.filter(is_effective=True).select_related(
        "actual_location", "actual_employee", "scanned_by",
    ).first()
    resolution = row.resolutions.filter(status="active").select_related("resolved_by").first()
    asset = row.asset
    status_labels = dict(Asset.AssetStatus.choices)
    fields = (
        ("部门", row.expected_department_snapshot, "未单独录入", asset.department.name,
         None, asset.department_id != row.expected_department_id),
        ("责任人", row.expected_employee_snapshot, scan.actual_employee.name if scan and scan.actual_employee else "—",
         asset.responsible_employee.name if asset.responsible_employee else "—",
         scan.actual_employee_id != row.expected_employee_id if scan else None,
         asset.responsible_employee_id != row.expected_employee_id),
        ("位置", row.expected_location_path_snapshot, location_path(scan.actual_location) if scan and scan.actual_location else "—",
         location_path(asset.location), scan.actual_location_id != row.expected_location_id if scan else None,
         asset.location_id != row.expected_location_id),
        ("状态", status_labels.get(row.expected_asset_status, row.expected_asset_status),
         status_labels.get(scan.actual_status, scan.actual_status) if scan else "—", asset.get_asset_status_display(),
         scan.actual_status != row.expected_asset_status if scan else None,
         asset.asset_status != row.expected_asset_status),
    )
    return {
        "difference_scan": scan,
        "difference_active_resolution": resolution,
        "difference_comparison": [
            {"label": label, "expected": expected, "actual": actual, "current": current,
             "actual_differs": actual_differs, "current_differs": current_differs}
            for label, expected, actual, current, actual_differs, current_differs in fields
        ],
    }
