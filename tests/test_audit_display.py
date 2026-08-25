from apps.audit.display import (
    audit_action_label,
    audit_object_label,
    localize_audit_payload,
)


def test_audit_action_and_object_codes_have_chinese_primary_labels():
    assert audit_action_label("asset_draft_create") == "创建资产草稿"
    assert audit_action_label("asset_label.print_confirmed") == "记录标签打印"
    assert audit_action_label("user_create") == "创建应用用户"
    assert audit_action_label("unknown.future.action") == "其他操作"
    assert audit_object_label("AssetFinance") == "资产财务资料"
    assert audit_object_label("User") == "用户"


def test_audit_payload_localization_keeps_values_but_translates_keys_roles_and_state():
    localized = localize_audit_payload(
        {
            "username": "zhangsan",
            "roles": ["system_admin", "finance"],
            "is_active": True,
            "asset_status": "pending_label",
            "original_cost": "12345.67",
        }
    )

    assert localized == {
        "用户名": "zhangsan",
        "固定角色": ["系统管理员", "财务"],
        "启用状态": "是",
        "资产状态": "待贴标",
        "原值": "12345.67",
    }


def test_audit_payload_localizes_qr_confirmation_method():
    assert localize_audit_payload(
        {"confirmation_method": "scan_opaque_origin"}
    ) == {"确认方式": "Edge 扫码兼容确认"}
