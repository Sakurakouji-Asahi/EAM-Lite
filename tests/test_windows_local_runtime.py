from __future__ import annotations

import builtins
import getpass
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import override_settings

from apps.audit.models import AuditLog


ROOT = Path(__file__).parents[1]
LOCAL_COMPOSE = ROOT / "deploy" / "compose.local.yaml"
DEV_COMPOSE = ROOT / "deploy" / "compose.dev.yaml"


def test_stable_and_development_compose_resources_are_explicitly_isolated():
    stable = LOCAL_COMPOSE.read_text(encoding="utf-8")
    development = DEV_COMPOSE.read_text(encoding="utf-8")

    assert "name: eam-lite-local" in stable
    assert '"127.0.0.1:8765:8080"' in stable
    assert "EAM_DATABASE_NAME:-eam_lite_local" in stable
    assert "EAM_ENVIRONMENT: local" in stable
    assert 'DEBUG: "false"' in stable
    assert "runserver" not in stable

    assert "name: eam-lite-dev" in development
    assert '"${EAM_DEV_BIND_ADDRESS:-127.0.0.1}:8766:8000"' in development
    assert "EAM_DEV_ALLOWED_HOSTS:-127.0.0.1,localhost" in development
    assert "EAM_DEV_CSRF_TRUSTED_ORIGINS:-http://127.0.0.1:8766" in development
    assert "EAM_DEV_QR_BASE_URL:-http://127.0.0.1:8766" in development
    assert "EAM_DEV_DATABASE_NAME:-eam_lite_dev" in development
    assert "EAM_ENVIRONMENT: development" in development
    assert 'DEBUG: "true"' in development
    assert "runserver" in development

    for text in (stable, development):
        assert "container_name:" not in text
        assert "5432:5432" not in text
        assert "external: true" not in text
        assert "/var/lib/postgresql" in text


def test_one_click_cmd_files_only_dispatch_to_powershell():
    expected = {
        "启动EAM-Lite.cmd": "start.ps1",
        "停止EAM-Lite.cmd": "stop.ps1",
        "更新EAM-Lite.cmd": "update.ps1",
        "查看EAM-Lite状态.cmd": "status.ps1",
        "备份EAM-Lite数据.cmd": "backup.ps1",
        "恢复EAM-Lite数据.cmd": "restore.ps1",
        "启动开发环境.cmd": "start-dev.ps1",
        "启动开发环境-局域网扫码测试.cmd": "start-dev-lan.ps1",
        "停止开发环境.cmd": "stop-dev.ps1",
    }
    for filename, script in expected.items():
        text = (ROOT / filename).read_text(encoding="utf-8")
        assert f"scripts\\local\\{script}" in text
        assert "docker " not in text.lower()
        assert "git " not in text.lower()


def test_lan_scan_launcher_keeps_default_dev_safe_and_sets_explicit_network_values():
    common = (ROOT / "scripts" / "local" / "common.ps1").read_text(
        encoding="utf-8-sig"
    )
    launcher = (ROOT / "scripts" / "local" / "start-dev-lan.ps1").read_text(
        encoding="utf-8-sig"
    )

    assert '$developmentBindAddress = "127.0.0.1"' in common
    assert '$developmentBindAddress = "0.0.0.0"' in common
    assert "EAM_DEV_ALLOWED_HOSTS" in common
    assert "EAM_DEV_CSRF_TRUSTED_ORIGINS" in common
    assert "EAM_DEV_QR_BASE_URL" in common
    assert "Get-EamPrimaryLanAddress" in launcher
    assert "Ensure-EamLanFirewallRule" in launcher
    assert "-DevelopmentLanAddress $lanAddress.IPAddress" in launcher
    assert "手机访问" in launcher


def test_local_scripts_never_delete_volumes_or_kill_unknown_processes():
    scripts = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "scripts" / "local").glob("*.ps1"))
    ).lower()
    forbidden = (
        "down -v",
        "down --volumes",
        "volume rm",
        "system prune",
        "reset --hard",
        "stop-process -force",
        "docker rm",
    )
    for token in forbidden:
        assert token not in scripts
    assert "脚本不会自动结束该进程" in scripts
    assert "latest 镜像" in scripts


@override_settings(
    EAM_ENVIRONMENT="development",
    APP_VERSION="0.2.1-test",
    APP_COMMIT_SHA="abcdef1234567890",
)
def test_development_pages_show_environment_banner_and_build_identity(client):
    response = client.get("/login/")
    html = response.content.decode()

    assert response.status_code == 200
    assert "开发环境 · 数据与稳定使用版完全隔离" in html
    assert "EAM-Lite 0.2.1-test · commit abcdef1 · 开发环境" in html


@pytest.mark.skipif(os.name != "nt", reason="PowerShell parser acceptance is Windows-only")
def test_all_local_powershell_scripts_parse_cleanly():
    command = (
        "$failed=$false; "
        "Get-ChildItem -LiteralPath 'scripts/local' -Filter '*.ps1' | ForEach-Object { "
        "$tokens=$null; $parseErrors=$null; "
        "[System.Management.Automation.Language.Parser]::ParseFile($_.FullName,[ref]$tokens,[ref]$parseErrors)|Out-Null; "
        "if($parseErrors.Count){$failed=$true; $parseErrors|ForEach-Object{[Console]::Error.WriteLine($_.Message)}} }; "
        "if($failed){exit 1}"
    )
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows Git discovery acceptance")
def test_git_worktree_is_detected_from_explorer_style_path():
    command = (
        "$env:Path=$env:SystemRoot + '\\System32;' + $env:SystemRoot; "
        ". .\\scripts\\local\\common.ps1; "
        "$git=Resolve-EamGitExecutable; "
        "if(-not $git){throw 'Git discovery failed'}; "
        "$root=Get-EamRepositoryRoot; "
        "if(-not (Test-EamGitRepository -RepositoryRoot $root)){throw 'Worktree detection failed'}; "
        "Write-Output $git"
    )
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().lower().endswith("git.exe")


@pytest.mark.django_db(transaction=True)
def test_bootstrap_local_admin_is_atomic_single_use_and_audited(
    monkeypatch, settings
):
    settings.EAM_ENVIRONMENT = "local"
    answers = iter(("local-admin", "本机管理员"))
    passwords = iter(("Local-Admin-2026-Strong!", "Local-Admin-2026-Strong!"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": next(answers))
    monkeypatch.setattr(getpass, "getpass", lambda _prompt="": next(passwords))

    call_command("bootstrap_local_admin", verbosity=0)

    user = get_user_model().objects.get(username="local-admin")
    assert user.is_staff is False and user.is_superuser is False
    assert set(user.groups.values_list("name", flat=True)) == {"system_admin"}
    assert AuditLog.objects.filter(
        action="account.bootstrap_created", object_id=str(user.pk)
    ).exists()

    call_command("bootstrap_local_admin", verbosity=0)
    assert get_user_model().objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_bootstrap_local_admin_rolls_back_when_audit_fails(monkeypatch, settings):
    settings.EAM_ENVIRONMENT = "local"
    answers = iter(("rollback-admin", "回滚管理员"))
    passwords = iter(("Saffron-Crane-2026!", "Saffron-Crane-2026!"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": next(answers))
    monkeypatch.setattr(getpass, "getpass", lambda _prompt="": next(passwords))
    monkeypatch.setattr(
        "apps.accounts.management.commands.bootstrap_local_admin.write_system_audit_log",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )

    with pytest.raises(RuntimeError, match="audit unavailable"):
        call_command("bootstrap_local_admin", verbosity=0)

    assert not get_user_model().objects.filter(username="rollback-admin").exists()


def test_release_builder_outputs_digest_bound_windows_package(tmp_path):
    from scripts.release.build_windows_package import build_package

    args = type(
        "Args",
        (),
        {
            "repository_root": ROOT,
            "output_dir": tmp_path,
            "commit": "b" * 40,
            "app_image": "ghcr.io/example/eam-lite:0.2.1",
            "app_image_digest": "sha256:" + "c" * 64,
            "repository": "example/eam-lite",
            "created_at": "2026-09-01T00:00:00Z",
        },
    )()

    package, checksum, manifest_path = build_package(args)

    assert package.is_file() and checksum.is_file() and manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["commit"] == "b" * 40
    assert manifest["app_image_digest"] == "sha256:" + "c" * 64
    assert not manifest["app_image"].endswith(":latest")
    with zipfile.ZipFile(package) as archive:
        names = set(archive.namelist())
        assert "release-manifest.json" in names
        assert "启动EAM-Lite.cmd" in names
        assert "scripts/local/start.ps1" in names
        assert "deploy/compose.local.yaml" in names
        assert "README-本机使用版.md" in names
        assert not any(name.endswith(".py") for name in names)
