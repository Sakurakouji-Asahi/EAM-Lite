from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path


POSTGRES_IMAGE = (
    "postgres:18.6-alpine@sha256:"
    "d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2"
)
CADDY_IMAGE = (
    "caddy:2.11.4-alpine@sha256:"
    "5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648"
)
COMMAND_FILES = (
    "启动EAM-Lite.cmd",
    "停止EAM-Lite.cmd",
    "更新EAM-Lite.cmd",
    "查看EAM-Lite状态.cmd",
    "备份EAM-Lite数据.cmd",
    "恢复EAM-Lite数据.cmd",
)
STATIC_FILES = (
    "VERSION",
    "README-本机使用版.md",
    "THIRD-PARTY-NOTICES.md",
    "deploy/compose.local.yaml",
    "deploy/Caddyfile.local",
    "deploy/postgres-init.sh",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_file(root: Path, stage: Path, relative: str) -> None:
    source = root / relative
    if not source.is_file():
        raise RuntimeError(f"release source file is missing: {relative}")
    destination = stage / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def build_package(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    root = args.repository_root.resolve()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+([.-][0-9A-Za-z.-]+)?", version):
        raise RuntimeError("VERSION is not a supported semantic version")
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit.lower()):
        raise RuntimeError("commit must be a full 40-character SHA")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", args.app_image_digest.lower()):
        raise RuntimeError("app image digest must be sha256:<64 hex>")
    if args.app_image.endswith(":latest") or "@" in args.app_image:
        raise RuntimeError(
            "app image must be a versioned repository/tag without latest or digest"
        )

    created_at = args.created_at or datetime.now(timezone.utc).isoformat()
    manifest = {
        "package_format": "eam-lite-windows-local-v1",
        "package_format_version": 1,
        "version": version,
        "commit": args.commit.lower(),
        "app_image": args.app_image,
        "app_image_digest": args.app_image_digest.lower(),
        "postgres_image": POSTGRES_IMAGE,
        "postgres_image_digest": POSTGRES_IMAGE.rsplit("@", 1)[1],
        "caddy_image": CADDY_IMAGE,
        "caddy_image_digest": CADDY_IMAGE.rsplit("@", 1)[1],
        "minimum_docker_desktop_version": "4.30.0",
        "created_at": created_at,
        "repository": args.repository,
    }

    with tempfile.TemporaryDirectory(prefix="eam-lite-windows-release-") as temp:
        stage = Path(temp) / f"EAM-Lite-v{version}-Windows"
        stage.mkdir()
        for relative in (*COMMAND_FILES, *STATIC_FILES):
            _copy_file(root, stage, relative)
        for script in sorted((root / "scripts" / "local").glob("*.ps1")):
            _copy_file(root, stage, script.relative_to(root).as_posix())
        (stage / "release-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        zip_path = output / f"EAM-Lite-v{version}-Windows.zip"
        if zip_path.exists():
            raise RuntimeError(f"refusing to overwrite existing package: {zip_path}")
        with zipfile.ZipFile(
            zip_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(stage).as_posix())

    package_hash = _sha256(zip_path)
    hash_path = zip_path.with_suffix(zip_path.suffix + ".sha256")
    hash_path.write_text(f"{package_hash}  {zip_path.name}\n", encoding="ascii")
    manifest_path = output / "release-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return zip_path, hash_path, manifest_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--app-image", required=True)
    parser.add_argument("--app-image-digest", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--created-at")
    return parser.parse_args()


if __name__ == "__main__":
    package, checksum, manifest = build_package(parse_args())
    print(package)
    print(checksum)
    print(manifest)
