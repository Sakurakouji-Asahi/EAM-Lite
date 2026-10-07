from html.parser import HTMLParser
from types import SimpleNamespace
from uuid import UUID

import pytest
from django.template.loader import render_to_string


class CopyControls(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.buttons = {}
        self.targets = {}
        self.active_target = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "button" and "data-copy-target" in attrs:
            self.buttons[attrs["data-copy-target"]] = attrs
        if tag == "span" and attrs.get("id") in {"asset-display-code", "asset-equipment-number"}:
            self.active_target = attrs["id"]
            self.targets[self.active_target] = ""

    def handle_data(self, data):
        if self.active_target:
            self.targets[self.active_target] += data

    def handle_endtag(self, tag):
        if tag == "span":
            self.active_target = None


@pytest.mark.parametrize("formal_code, equipment_number, can_p1", [
    ("FA-02-2026-000123-00", '设备 & "原值" <01>', True),
    (None, "EQ-000123", True),
    ("FA-02-2026-000123-00", "restricted-equipment-number", False),
    (None, "", True),
])
def test_copy_controls_use_displayed_identifier_and_keep_equipment_visibility(
    formal_code, equipment_number, can_p1
):
    asset = SimpleNamespace(
        pk=UUID("12345678-1234-5678-1234-567812345678"),
        asset_code=formal_code, draft_number="D-12345678", asset_name="复制编号示例",
        equipment_number=equipment_number, qr_identities=SimpleNamespace(first=None),
    )
    html = render_to_string("assets/asset_detail.html", {
        "request": SimpleNamespace(user=SimpleNamespace(is_authenticated=True, username="preview-user", display_name="预览用户")),
        "asset": asset, "can_p1": can_p1,
    })
    controls = CopyControls(html)
    assert controls.targets["asset-display-code"] == (formal_code or asset.draft_number)
    assert controls.buttons["asset-display-code"]["aria-label"] == (
        "复制正式资产编号" if formal_code else "复制临时草稿号"
    )
    if can_p1 and equipment_number:
        assert controls.targets["asset-equipment-number"] == equipment_number
        assert controls.buttons["asset-equipment-number"]["aria-label"] == "复制设备编号"
    else:
        assert "asset-equipment-number" not in controls.buttons
        assert "asset-equipment-number" not in controls.targets
        if equipment_number:
            assert equipment_number not in html
