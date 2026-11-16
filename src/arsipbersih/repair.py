"""Owner-confirmed repairs for a stuck move journal.

Only database rows change here. No media file is ever deleted or moved: a row is released
(or a file forgotten) only after confirming that the file is absent from disk.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from arsipbersih.mover import PARTIAL_SUFFIX, _abs, _exists, _norm, _partial, reconcile


class StillOnDisk(Exception):
    """A file is still present on disk, so its row must not be released or forgotten."""


@dataclass
class PendingRow:
    row_id: str
    batch_id: str
    batch_label: str
    unit_id: str | None
    file_id: str | None
    src_rel: str
    dst_rel: str
    created_at: str


@dataclass
class MissingFile:
    file_id: str
    unit_id: str | None
    rel_path: str
    size: int | None


def list_pending(conn: sqlite3.Connection) -> list[PendingRow]:
    rows = conn.execute(
        "select m.id, m.batch_id, coalesce(b.label, ''), m.unit_id, m.file_id,"
        " m.src_rel, m.dst_rel, coalesce(m.created_at, '')"
        " from moves m join batches b on b.id = m.batch_id"
        " where m.status='pending' order by m.rowid"
    ).fetchall()
    return [PendingRow(*r) for r in rows]


def _present(root: Path, rel: str) -> bool:
    p = _abs(root, rel)
    return _exists(p) or _exists(_partial(p))


def release_pending(
    conn: sqlite3.Connection, root: Path, row_id: str, deleted_on_purpose: bool
) -> None:
    """Fail a pending row whose file is gone; mark the file purged or missing."""
    root = _norm(root)
    reconcile(conn, root)
    row = conn.execute(
        "select status, file_id, src_rel, dst_rel from moves where id=?", (row_id,)
    ).fetchone()
    if row is None or row[0] != "pending":
        return
    _, file_id, src_rel, dst_rel = row
    if _present(root, src_rel) or _present(root, dst_rel):
        raise StillOnDisk(f"file still on disk ({src_rel} / {dst_rel}{PARTIAL_SUFFIX} included)")
    flag = "purged" if deleted_on_purpose else "missing"
    with conn:
        conn.execute("update moves set status='failed' where id=? and status='pending'", (row_id,))
        if file_id:
            conn.execute(f"update files set {flag}=1 where id=?", (file_id,))
    reconcile(conn, root)


def list_missing(conn: sqlite3.Connection) -> list[MissingFile]:
    rows = conn.execute(
        "select id, unit_id, rel_path, size from files where missing=1 and purged=0 order by rel_path"
    ).fetchall()
    return [MissingFile(*r) for r in rows]


def forget_file(conn: sqlite3.Connection, root: Path, file_id: str) -> None:
    """Mark a file that is absent from disk as deliberately deleted (purged)."""
    root = _norm(root)
    row = conn.execute("select rel_path from files where id=?", (file_id,)).fetchone()
    if row is None:
        raise KeyError(file_id)
    if _present(root, row[0]):
        raise StillOnDisk(f"file still on disk: {row[0]}")
    with conn:
        conn.execute("update files set purged=1 where id=?", (file_id,))
