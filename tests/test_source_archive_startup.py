import json
import os
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts.release.build_windows_package import build_package

ROOT=Path(__file__).resolve().parents[1]
pytestmark=pytest.mark.skipif(os.name!='nt',reason='Windows PowerShell startup acceptance')


def package_at(tmp_path):
    package,_,_=build_package(SimpleNamespace(repository_root=ROOT,output_dir=tmp_path/'output',commit='b'*40,
        app_image='ghcr.io/example/eam-lite:0.3.0',app_image_digest='sha256:'+'c'*64,repository='example/eam-lite',created_at='2026-09-30T00:00:00Z'))
    destination=tmp_path/'新电脑 空白目录'
    with zipfile.ZipFile(package) as archive:archive.extractall(destination)
    return destination


def run_identity(root,extra=''):
    script=root.parent/'identity-check.ps1'
    quoted=str(root).replace("'","''")
    script.write_text("[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)\n"+
        f". '{quoted}\\scripts\\local\\common.ps1'\nfunction Resolve-EamGitExecutable {{ return $null }}\n"+
        f"$identity=Get-EamStableIdentity -RepositoryRoot '{quoted}'\n"+extra+
        "$identity | Select-Object Kind,Commit,AppImage,BundledSource | ConvertTo-Json -Compress\n",encoding='utf-8-sig')
    return subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(script)],capture_output=True,text=True,encoding='utf-8',errors='replace')


def test_downloaded_source_works_without_git_or_private_state_and_detects_missing_files(tmp_path):
    root=package_at(tmp_path)
    (root/'release-manifest.json').unlink()
    first=run_identity(root)
    assert first.returncode==0,first.stderr
    identity=json.loads(first.stdout)
    assert identity['Kind']=='source' and len(identity['Commit'])==40
    with (root/'manage.py').open('a',encoding='utf-8') as target:target.write('\n# Source content change\n')
    second=run_identity(root)
    assert json.loads(second.stdout)['Commit']!=identity['Commit']
    (root/'manage.py').unlink()
    assert run_identity(root).returncode!=0


def test_release_source_fallback_is_integrity_checked_and_does_not_require_registry_access(tmp_path):
    root=package_at(tmp_path)
    result=run_identity(root,"function Pull-EamImage { throw 'registry unavailable' }\nfunction Build-EamImage { param($RepositoryRoot,$Identity) }\nEnsure-EamApplicationImage -RepositoryRoot '"+str(root).replace("'","''")+"' -Identity $identity\n")
    assert result.returncode==0,result.stderr
    identity=json.loads(next(line for line in result.stdout.splitlines() if line.startswith('{')))
    assert identity['Kind']=='release' and identity['AppImage']=='eam-lite-local:release-'+'b'*40
    with (root/'manage.py').open('a',encoding='utf-8') as target:target.write('\n# Tampered release\n')
    rejected=run_identity(root)
    assert rejected.returncode!=0
