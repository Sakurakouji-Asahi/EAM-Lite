"""A read-only physical card with the same field visibility as asset detail."""
from django.utils import timezone
from apps.assets.form_options import location_path
from apps.assets.permissions import can_view_asset_p1, can_view_asset_summary_fields
from apps.assets.qr_permissions import can_manage_labels


def asset_card_context(actor, asset):
    can_p1 = can_view_asset_p1(actor, asset)
    can_summary = can_view_asset_summary_fields(actor, asset)
    current_qr = asset.qr_identities.filter(status="active").first()
    assignment_rows = []
    if can_p1 or can_summary:
        assignment_rows = [("部门", asset.department.name if asset.department else "未记录"),
                           ("责任人", asset.responsible_employee.name if asset.responsible_employee else "未记录"),
                           ("当前位置", location_path(asset.location) if asset.location else "未记录")]
    physical_rows = []
    if can_p1:
        physical_rows = [
            ("设备编号", asset.equipment_number or "—"),
            ("品牌 / 型号", f"{asset.brand or '—'} / {asset.model or '—'}"),
            ("厂家", asset.manufacturer or "—"), ("序列号", asset.serial_number or "—"),
            ("出厂编号", asset.factory_number or "—"), ("历史参考编号", asset.historical_code or "—"),
            ("数量 / 单位", f"1 / {asset.unit or '—'}"),
            ("购置日期", asset.acquisition_date.isoformat() if asset.acquisition_date else "—"),
            ("达到可使用状态日期", asset.commissioning_date.isoformat() if asset.commissioning_date else "—"),
            ("需要保养", "是" if asset.is_maintenance_required else "否"),
        ]
        for label, value in (("车牌号", asset.vehicle_plate), ("车架号", asset.chassis_number),
                             ("校准编号", asset.calibration_number)):
            if value:
                physical_rows.append((label, value))
    return {"asset": asset, "card_can_p1": can_p1, "assignment_rows": assignment_rows,
            "physical_rows": physical_rows, "category_path": location_path(asset.category),
            "current_qr": current_qr, "card_show_qr": bool(current_qr and can_manage_labels(actor, asset)),
            "generated_at": timezone.now(), "company_name": asset.company.short_name or asset.company.name}
