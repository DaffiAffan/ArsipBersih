"""Folder-name constants and path helpers."""

from __future__ import annotations

import os
import re
from pathlib import Path

SOURCE_DIR = "Sumber"
ARCHIVE_DIR = "Arsip"
REVIEW_DIR = "_review"
TRASH_DIR = "_sampah"
STATE_DIR = ".arsipbersih"
UNDATED_DIR = "Tanpa-Tanggal"

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_LONG_PREFIX = "\\\\?\\"


def long_path(p: Path) -> str:
    """Path string for filesystem calls: \\\\?\\ prefix for absolute paths on Windows."""
    s = str(p)
    if os.name != "nt" or s.startswith(_LONG_PREFIX) or not os.path.isabs(s):
        return s
    if s.startswith("\\\\"):  # UNC path
        return _LONG_PREFIX + "UNC\\" + s[2:]
    return _LONG_PREFIX + s


def sanitize_component(name: str) -> str:
    """Make a string safe as a single Windows folder/file name component."""
    cleaned = _INVALID_CHARS.sub(" ", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.rstrip(". ")
    return cleaned or "_"


def to_rel(root: Path, p: Path) -> str:
    """Path relative to root, POSIX-style."""
    return Path(p).relative_to(root).as_posix()


def to_abs(root: Path, rel: str) -> Path:
    return root.joinpath(*rel.split("/"))
