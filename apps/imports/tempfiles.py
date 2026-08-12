"""Cross-process activity markers for private upload temporary files."""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from pathlib import Path

from django.core.exceptions import ValidationError


ACTIVE_MARKER_DIRECTORY = ".active"


def _resolved_below(root, value):
    root = Path(root).resolve()
    value = Path(value).resolve()
    if value == root or root not in value.parents:
        raise ValidationError("上传临时文件超出受控临时目录。")
    return root, value


def marker_path_for(temp_path, root):
    root, temp_path = _resolved_below(root, temp_path)
    digest = hashlib.sha256(str(temp_path).encode("utf-8")).hexdigest()
    return root / ACTIVE_MARKER_DIRECTORY / f"{digest}.lock"


def _lock_marker(handle, *, nonblocking):
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        mode = msvcrt.LK_NBLCK if nonblocking else msvcrt.LK_LOCK
        msvcrt.locking(handle.fileno(), mode, 1)
    else:
        import fcntl

        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        fcntl.flock(handle.fileno(), flags)


def _unlock_marker(handle):
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def hold_temp_file_active(temp_path, root):
    """Hold an OS lock so cleanup can prove that a temp file is inactive."""

    marker = marker_path_for(temp_path, root)
    marker.parent.mkdir(parents=True, exist_ok=True)
    with marker.open("a+b") as handle:
        if marker.stat().st_size == 0:
            handle.write(b"1")
            handle.flush()
        _lock_marker(handle, nonblocking=False)
        try:
            yield marker
        finally:
            _unlock_marker(handle)
    marker.unlink(missing_ok=True)


def marker_is_active(marker):
    """Return True only while another process owns the marker lock."""

    marker = Path(marker)
    try:
        with marker.open("r+b") as handle:
            try:
                _lock_marker(handle, nonblocking=True)
            except OSError:
                return True
            _unlock_marker(handle)
            return False
    except FileNotFoundError:
        return False
