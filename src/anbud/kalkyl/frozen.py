"""A finished Kalkyl run is frozen: its outputs folder and its run record become read-only, files 0o400 and folders
0o500, so nothing can add to, change or remove what it produced. A rebuild reads a frozen run and writes its own.

The harness never writes symlinks into these folders; one found there is left alone and counted as a failure.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

FILE_MODE = 0o400
DIR_MODE = 0o500


def is_frozen(path: Path) -> bool:
    """True when the folder exists and its owner can't write to it."""
    try:
        return not path.lstat().st_mode & stat.S_IWUSR
    except FileNotFoundError:
        return False


def freeze_tree(path: Path) -> list[str]:
    """Make every file under `path` 0o400 and every folder, `path` last, 0o500. Returns a reason for each entry that
    couldn't be, never a path, so the caller can say how many failed."""
    failed: list[str] = []
    if not path.is_dir() or path.is_symlink():
        return ["not a folder"]
    for parent, dirs, files in os.walk(path, topdown=False):
        for name in [*files, *dirs]:
            entry = Path(parent) / name
            if entry.is_symlink():
                failed.append("symlink")
                continue
            failed.extend(_chmod(entry, DIR_MODE if entry.is_dir() else FILE_MODE))
    failed.extend(_chmod(path, DIR_MODE))
    return failed


def _chmod(path: Path, mode: int) -> list[str]:
    try:
        os.chmod(path, mode)
    except OSError as exc:
        return [type(exc).__name__]
    return []
