"""Paper-only representations of fields already offered by the result export."""
from apps.assets.models import Asset


STATUS_LABELS = dict(Asset.AssetStatus.choices)


def paper_result_items(rows):
    items = []
    for row in rows.iterator(chunk_size=200):
        scan = next((scan for scan in row.scans.all() if scan.is_effective), None)
        resolution = next((resolution for resolution in row.resolutions.all() if resolution.status == "active"), None)
        items.append({"row": row, "scan": scan, "resolution": resolution,
            "expected_status_label": STATUS_LABELS.get(row.expected_asset_status, row.expected_asset_status),
            "actual_status_label": STATUS_LABELS.get(scan.actual_status, scan.actual_status) if scan else ""})
    return items


def paper_filter_summary(form):
    values = form.cleaned_data
    return {"keyword": values["q"], "row_view": dict(form.fields["row_view"].choices)[values["row_view"]]}
