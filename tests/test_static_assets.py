from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.urls import reverse


pytestmark = pytest.mark.django_db


def test_home_request_uses_only_local_bootstrap_and_htmx(client):
    user = get_user_model().objects.create_user(
        username="static-user",
        password="Static-Password-2026!",
        display_name="静态资源测试",
    )
    client.force_login(user)

    response = client.get(reverse("home"))
    html = response.content.decode()

    assert response.status_code == 200
    assert "https://" not in html
    assert "http://" not in html
    assert "/static/vendor/bootstrap/5.3.8/css/bootstrap.min.css" in html
    assert "/static/vendor/bootstrap/5.3.8/js/bootstrap.bundle.min.js" in html
    assert "/static/vendor/htmx/2.0.10/htmx.min.js" in html
    htmx_config = '<meta name="htmx-config" content=\'{"includeIndicatorStyles": false}\'>'
    assert htmx_config in html
    assert html.index(htmx_config) < html.index("/static/vendor/htmx/2.0.10/htmx.min.js")
    csp = response.headers["Content-Security-Policy"]
    assert csp.startswith("default-src 'self'")
    assert "style-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert "<style" not in html.lower()


@pytest.mark.parametrize(
    "asset_path",
    [
        "vendor/bootstrap/5.3.8/css/bootstrap.min.css",
        "vendor/bootstrap/5.3.8/js/bootstrap.bundle.min.js",
        "vendor/bootstrap/5.3.8/LICENSE",
        "vendor/htmx/2.0.10/htmx.min.js",
        "vendor/htmx/2.0.10/LICENSE",
    ],
)
def test_required_vendor_asset_is_packaged_locally(asset_path):
    resolved = finders.find(asset_path)

    assert resolved is not None
    assert Path(resolved).is_file()
    assert Path(resolved).stat().st_size > 0


def test_templates_do_not_reference_public_cdn():
    template_roots = [settings.BASE_DIR / "templates"]
    template_roots.extend(
        path for path in (settings.BASE_DIR / "apps").glob("*/templates") if path.is_dir()
    )
    for root in template_roots:
        for template in root.rglob("*.html"):
            content = template.read_text(encoding="utf-8").lower()
            assert "cdn." not in content
            assert "unpkg.com" not in content
            assert "http://" not in content
            assert "https://" not in content
            assert " onsubmit=" not in content
            assert " onclick=" not in content


def test_local_styles_include_htmx_indicator_defaults():
    app_css = Path(finders.find("css/app.css"))
    content = app_css.read_text(encoding="utf-8")

    assert ".htmx-indicator" in content
    assert ".htmx-request .htmx-indicator" in content
    assert ".htmx-request.htmx-indicator" in content
    assert "visibility: hidden" in content
    assert "visibility: visible" in content
