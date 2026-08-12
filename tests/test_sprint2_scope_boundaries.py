from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from django.apps import apps
from django.core.management import get_commands
from django.urls import URLPattern, URLResolver, get_resolver


pytestmark = pytest.mark.django_db

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = REPOSITORY_ROOT / "apps"

# "generate" on its own is intentionally not forbidden: Sprint 2 includes
# preview generation.  These tokens describe an unbound *official* allocation
# surface, which is reserved for Sprint 4.
FORBIDDEN_PUBLIC_TOKEN = re.compile(
    r"(?:^|[-_/.])(?:issue|issuer|issuance|allocate|allocation|formal[-_]?code)"
    r"(?:$|[-_/.])",
    re.IGNORECASE,
)
FORBIDDEN_CALLABLE_NAMES = {
    "assetcodeissuer",
    "issue",
    "issue_code",
    "issue_asset_code",
    "allocate",
    "allocate_code",
    "allocate_asset_code",
    "generate_official_code",
}


def _walk_urlpatterns(patterns, prefix=""):
    for pattern in patterns:
        route = prefix + str(pattern.pattern)
        if isinstance(pattern, URLResolver):
            yield from _walk_urlpatterns(pattern.url_patterns, route)
        elif isinstance(pattern, URLPattern):
            yield pattern, route


def _python_files_under_apps():
    yield from APP_ROOT.rglob("*.py")


def _top_level_callable_names(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name.casefold()


def test_sprint2_does_not_register_asset_master_model():
    registered_models = {
        model._meta.label_lower for model in apps.get_models(include_auto_created=True)
    }

    assert not any(label.rsplit(".", 1)[-1] == "asset" for label in registered_models)


def test_sprint2_urls_have_no_unbound_official_issuance_endpoint():
    exposed = []
    for pattern, route in _walk_urlpatterns(get_resolver().url_patterns):
        callback_name = getattr(pattern.callback, "__name__", "")
        dotted_callback = getattr(pattern.callback, "__module__", "") + "." + callback_name
        surface = " ".join((route, pattern.name or "", dotted_callback))
        if FORBIDDEN_PUBLIC_TOKEN.search(surface):
            exposed.append(surface)

    assert exposed == []


def test_sprint2_has_no_official_issuance_management_command():
    forbidden_commands = sorted(
        name for name in get_commands() if FORBIDDEN_PUBLIC_TOKEN.search(name)
    )

    assert forbidden_commands == []


def test_sprint2_services_export_no_issue_or_allocate_primitive():
    forbidden = []
    for path in _python_files_under_apps():
        # Models named IssuedCode are allowed.  Only callable entry points are
        # inspected, and migrations are excluded because historical operation
        # helper names are not an application Service API.
        if "migrations" in path.parts:
            continue
        for name in _top_level_callable_names(path):
            if name in FORBIDDEN_CALLABLE_NAMES:
                forbidden.append(f"{path.relative_to(REPOSITORY_ROOT)}::{name}")

    assert forbidden == []


def test_sprint2_source_has_no_asset_model_or_later_sprint_domain_model():
    forbidden_model_names = {
        "asset",
        "assetcodehistory",
        "assetfinance",
        "assetqridentity",
        "assetmovement",
        "depreciationpolicy",
        "inventorytask",
        "maintenanceplan",
    }
    declared = []
    for path in _python_files_under_apps():
        if "migrations" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            base_names = {
                (base.attr if isinstance(base, ast.Attribute) else base.id).casefold()
                for base in node.bases
                if isinstance(base, (ast.Attribute, ast.Name))
            }
            if "model" in base_names and node.name.casefold() in forbidden_model_names:
                declared.append(
                    f"{path.relative_to(REPOSITORY_ROOT)}::{node.name}"
                )

    assert declared == []
