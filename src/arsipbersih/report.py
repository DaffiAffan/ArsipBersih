"""Final report and the file-count verification invariant."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .formats import IGNORED_NAMES, SKIP_DIRS, Kind, detect_kind
from .layout import ARCHIVE_DIR, REVIEW_DIR, SOURCE_DIR, STATE_DIR, TRASH_DIR, long_path

_MEDIA_KINDS = "('photo','raw','video','gopro_aux','sidecar')"
_DUP = "('duplikat-persis','duplikat-versi')"


@dataclass
class Report:
    total_media: int
    by_location: dict[str, int]
    by_category: dict[str, int]
    bytes_in_trash: int
    bytes_saved_duplicates: int
    non_media_left: int
    undated: int
    cache_bytes: int


@dataclass
class VerifyResult:
    ok: bool
    db_count: int
    disk_count: int
    missing: list[str]
    unexpected: list[str]


def _walk_files(root: Path, top: str, media: bool):
    """Yield rel paths under top whose kind is media (or not), with the inventory's skip rules."""
    stack = [(root / top, top)]
    while stack:
        cur_abs, cur_rel = stack.pop()
        try:
            it = os.scandir(long_path(cur_abs))
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.is_symlink():
                        continue
                    name = e.name.casefold()
                    if e.is_dir(follow_symlinks=False):
                        if name not in SKIP_DIRS:
                            stack.append((cur_abs / e.name, f"{cur_rel}/{e.name}"))
                    elif e.is_file(follow_symlinks=False):
                        if name in IGNORED_NAMES or name.endswith(".partial"):
                            continue
                        if (detect_kind(cur_abs / e.name) is not Kind.OTHER) == media:
                            yield f"{cur_rel}/{e.name}"
                except OSError:
                    continue


def _dir_bytes(path: Path) -> int:
    total = 0
    for dp, _dn, fn in os.walk(long_path(path)):
        for f in fn:
            try:
                total += os.stat(os.path.join(dp, f)).st_size
            except OSError:
                pass
    return total


def build_report(conn: sqlite3.Connection, root: Path) -> Report:
    root = Path(root)
    total = conn.execute(
        f"select count(*) from files where purged=0 and missing=0 and kind in {_MEDIA_KINDS}"
    ).fetchone()[0]
    by_location = {r[0]: r[1] for r in conn.execute("select location, count(*) from units group by location")}
    by_category = {
        r[0]: r[1]
        for r in conn.execute(
            "select primary_category, count(*) from units where primary_category is not null"
            " group by primary_category"
        )
    }
    in_trash = conn.execute(
        "select coalesce(sum(f.size), 0) from files f join units u on u.id = f.unit_id"
        " where u.location = 'trash' and f.purged = 0"
    ).fetchone()[0]
    saved = conn.execute(
        "select coalesce(sum(f.size), 0) from files f join units u on u.id = f.unit_id"
        f" where u.primary_category in {_DUP} and u.location != 'archive' and f.purged = 0"
    ).fetchone()[0]
    undated = conn.execute("select count(*) from units where taken_at is null").fetchone()[0]
    cache = root / STATE_DIR / "cache"
    return Report(
        total_media=total,
        by_location=by_location,
        by_category=by_category,
        bytes_in_trash=int(in_trash),
        bytes_saved_duplicates=int(saved),
        non_media_left=sum(1 for _ in _walk_files(root, SOURCE_DIR, media=False)),
        undated=undated,
        cache_bytes=_dir_bytes(cache) if cache.is_dir() else 0,
    )


def verify(conn: sqlite3.Connection, root: Path) -> VerifyResult:
    """Media files on disk must equal the unpurged DB rows (case-insensitive on rel_path).

    Rows a re-scan marked missing still count: they are files the archive lost.
    """
    root = Path(root)
    disk: dict[str, str] = {}
    for top in (SOURCE_DIR, ARCHIVE_DIR, REVIEW_DIR, TRASH_DIR):
        for rel in _walk_files(root, top, media=True):
            disk[rel.casefold()] = rel
    db = {
        r[0].casefold(): r[0]
        for r in conn.execute("select rel_path from files where purged=0 and kind != 'other'")
    }
    missing = sorted(db[k] for k in db.keys() - disk.keys())
    unexpected = sorted(disk[k] for k in disk.keys() - db.keys())
    return VerifyResult(
        ok=not missing and not unexpected,
        db_count=len(db),
        disk_count=len(disk),
        missing=missing,
        unexpected=unexpected,
    )
