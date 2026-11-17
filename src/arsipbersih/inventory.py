"""Incremental inventory of Sumber/ and Arsip/ into the `files` table."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .db import new_id
from .formats import IGNORED_NAMES, SKIP_DIRS, Kind, detect_kind
from .layout import ARCHIVE_DIR, SOURCE_DIR, long_path

NO_LABEL = "_tanpa-label"
BATCH_SIZE = 1000
MAX_ERROR_PATHS = 50

_INSERT = (
    "insert into files (id, rel_path, orig_rel_path, size, mtime_ns, kind, ext, source_label,"
    " missing, purged) values (?, ?, ?, ?, ?, ?, ?, ?, 0, 0)"
)


@dataclass
class InventoryResult:
    added: int = 0
    unchanged: int = 0
    updated: int = 0
    missing: int = 0
    non_media: int = 0
    skipped_dirs: int = 0
    errors: int = 0
    error_paths: list[str] = field(default_factory=list)
    missing_paths: list[str] = field(default_factory=list)

    def record_error(self, rel: str) -> None:
        self.errors += 1
        if len(self.error_paths) < MAX_ERROR_PATHS:
            self.error_paths.append(rel)


def _is_link(entry: os.DirEntry) -> bool:
    if entry.is_symlink():
        return True
    is_junction = getattr(entry, "is_junction", None)
    return bool(is_junction and is_junction())


def _walk(abs_dir: Path, rel_dir: str, result: InventoryResult):
    """Yield (abs_path, rel_path, size, mtime_ns) for every regular file, pruning skip dirs."""
    stack = [(abs_dir, rel_dir)]
    while stack:
        cur_abs, cur_rel = stack.pop()
        try:
            it = os.scandir(long_path(cur_abs))
        except OSError:
            result.record_error(cur_rel)
            continue
        with it:
            for entry in it:
                if _is_link(entry):
                    continue
                child_rel = f"{cur_rel}/{entry.name}"
                child_abs = cur_abs / entry.name
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.casefold() in SKIP_DIRS:
                            result.skipped_dirs += 1
                        else:
                            stack.append((child_abs, child_rel))
                    elif entry.is_file(follow_symlinks=False):
                        if entry.name.casefold() in IGNORED_NAMES:
                            continue
                        if entry.name.casefold().endswith(".partial"):
                            continue  # the mover's temp files are never media
                        st = entry.stat(follow_symlinks=False)
                        yield child_abs, child_rel, st.st_size, st.st_mtime_ns
                except OSError:
                    result.record_error(child_rel)
                    continue


def _label_for(rel_path: str) -> str:
    parts = rel_path.split("/")
    if parts[0] == SOURCE_DIR:
        return parts[1] if len(parts) > 2 else NO_LABEL
    return ARCHIVE_DIR


def _path_key(rel: str) -> str:
    """NTFS ignores case: IMG.JPG renamed to img.jpg is the same file, not a new one."""
    return rel.casefold() if os.name == "nt" else rel


def _pick(cands: list, rel: str):
    """The row a walked path belongs to: exact spelling first, then a live row."""
    for c in cands:
        if c["rel_path"] == rel:
            return c
    return next((c for c in cands if not c["purged"]), cands[0])


def run_inventory(conn: sqlite3.Connection, root: Path) -> InventoryResult:
    root = Path(root)
    result = InventoryResult()
    known: dict[str, list] = {}
    for r in conn.execute("select id, rel_path, size, mtime_ns, missing, purged from files"):
        known.setdefault(_path_key(r["rel_path"]), []).append(r)
    seen_ids: set[str] = set()
    seen_keys: set[str] = set()
    inserts: list[tuple] = []

    def flush() -> None:
        if inserts:
            conn.executemany(_INSERT, inserts)
            inserts.clear()

    for top in (SOURCE_DIR, ARCHIVE_DIR):
        top_abs = root / top
        if not top_abs.is_dir():
            continue
        for abs_path, rel, size, mtime_ns in _walk(top_abs, top, result):
            key = _path_key(rel)
            cands = known.get(key)
            row = _pick(cands, rel) if cands else None
            if row is not None:
                seen_ids.add(row["id"])
                seen_keys.add(key)
                if row["rel_path"] != rel:  # renamed in case only: follow the new spelling
                    conn.execute(
                        "update files set rel_path=?, orig_rel_path=case when orig_rel_path=?"
                        " then ? else orig_rel_path end where id=?",
                        (rel, row["rel_path"], rel, row["id"]),
                    )
                if row["size"] == size and row["mtime_ns"] == mtime_ns and not row["purged"]:
                    if row["missing"]:
                        conn.execute("update files set missing=0 where id=?", (row["id"],))
                    result.unchanged += 1
                    continue
            kind = detect_kind(abs_path)
            if kind is Kind.OTHER:
                result.non_media += 1
                continue
            ext = abs_path.suffix.lower().lstrip(".")
            if row is not None:
                conn.execute(
                    "update files set size=?, mtime_ns=?, kind=?, ext=?, sha256=null,"
                    " hash_status=null, meta_status=null, visual_status=null, error=null,"
                    " missing=0, purged=0 where id=?",
                    (size, mtime_ns, kind.value, ext, row["id"]),
                )
                # The unit's image changed: its AI embedding / error describe the old bytes.
                for table in ("ai_embeddings", "ai_errors"):
                    conn.execute(
                        f"delete from {table} where unit_id in"
                        " (select id from units where primary_file_id=?)",
                        (row["id"],),
                    )
                result.updated += 1
            else:
                inserts.append(
                    (new_id(), rel, rel, size, mtime_ns, kind.value, ext, _label_for(rel))
                )
                result.added += 1
                if len(inserts) >= BATCH_SIZE:
                    flush()
    flush()

    # Rows the walk did not see: Sumber/Arsip rows are gone, _review/_sampah rows were never
    # walked. A direct existence test covers both.
    for key, cands in known.items():
        for row in cands:
            if row["id"] in seen_ids or row["purged"]:
                continue
            if key in seen_keys:
                exists = False  # a case-only twin of a walked file: that file is another row's
            else:
                exists = os.path.lexists(long_path(root.joinpath(*row["rel_path"].split("/"))))
            if exists and row["missing"]:
                conn.execute("update files set missing=0 where id=?", (row["id"],))
            elif not exists and not row["missing"]:
                conn.execute("update files set missing=1 where id=?", (row["id"],))
                if key in seen_keys:
                    continue  # a stale twin row; the file itself is still there
                result.missing += 1
                if len(result.missing_paths) < MAX_ERROR_PATHS:
                    result.missing_paths.append(row["rel_path"])
    conn.commit()
    return result
