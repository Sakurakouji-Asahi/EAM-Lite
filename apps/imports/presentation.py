"""Business-facing import rows and live, permission-scoped next steps."""
from apps.assets.models import Asset
from apps.assets.permissions import scoped_assets_p1
from apps.finance.permissions import can_view_finance, can_manage_finance
from apps.finance.readiness import pending_finance_assets


def asset_import_context(*, actor, batch, rows):
    if batch.import_type != "asset_initialization":
        return {}
    financial = can_view_finance(actor)
    ids = list(batch.rows.filter(created_object_type="Asset", validation_status="created").values_list("created_object_id", flat=True))
    assets = scoped_assets_p1(actor, batch.company, Asset.objects.filter(pk__in=ids)).select_related("category", "department", "responsible_employee", "location")
    lookup = {str(asset.pk): asset for asset in assets}
    registered = sum(bool(asset.current_issued_code_id) for asset in lookup.values())
    drafts = sum(asset.record_status == "active" and asset.asset_status in {"draft", "pending_finance"}
                 and not asset.current_issued_code_id for asset in lookup.values())
    for row in rows:
        raw = row.raw_data_json or {}
        normalized = row.normalized_data_json or {}
        data = normalized.get("asset_data") or {}
        finance = normalized.get("finance_data") or {}
        current = lookup.get(row.created_object_id)
        row.asset_preview = {
            "name": data.get("asset_name") or raw.get("资产名称") or "未填写名称",
            "category": current.category.name if current else raw.get("实物分类编码", ""),
            "department": current.department.name if current and current.department else raw.get("部门编码", ""),
            "employee": current.responsible_employee.name if current and current.responsible_employee else raw.get("责任员工编号", ""),
            "location": current.location.name if current and current.location else raw.get("位置编码", ""),
            "cost": finance.get("original_cost", raw.get("原值")),
            "asset": lookup.get(row.created_object_id),
        }
    return {
        "show_import_finance": financial,
        "import_progress": {
            "visible": len(lookup), "registered": registered, "drafts": drafts,
            "finance_pending": pending_finance_assets(assets).count() if financial else None,
            "finance_done": assets.filter(finance__finance_confirmed_at__isnull=False).count() if financial else None,
            "can_confirm_finance": can_manage_finance(actor),
        },
    }
