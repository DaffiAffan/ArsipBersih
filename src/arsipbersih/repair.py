"""Owner-confirmed repairs for a stuck move journal.

This module itself never touches files; all disk changes go through mover.reconcile. A row is
released (or a file forgotten) only after confirming that the file is absent from disk and
that both endpoints are reachable (an unplugged drive also makes a file look absent).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from arsipbersih.mover import _abs, _exists, _norm, _partial, _reachable, reconcile


class StillOnDisk(Exception):
    """A file is still present on disk, so its row must not be released or forgotten."""


class EndpointOffline(Exception):
    """A folder of the file is unreachable (drive disconnected?), so absence proves nothing."""


class HasPendingMove(Exception):
    """The file still has a pending journal row; settle it with release_pending first."""


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


def _found(root: Path, rel: str) -> Path | None:
    """The path (or its .partial) that exists on disk for rel, if any."""
    p = _abs(root, rel)
    for c in (p, _partial(p)):
        if _exists(c):
            return c
    return None


def _require_reachable(root: Path, *rels: str) -> None:
    for rel in rels:
        if not _reachable(_abs(root, rel)):
            raise EndpointOffline(f"folder not reachable (drive disconnected?): {rel}")


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
    for rel in (src_rel, dst_rel):
        found = _found(root, rel)
        if found is not None:
            raise StillOnDisk(f"file still on disk: {found}")
    _require_reachable(root, src_rel, dst_rel)
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
    if conn.execute("select 1 from files where id=?", (file_id,)).fetchone() is None:
        raise KeyError(file_id)
    reconcile(conn, root)
    # Read after reconcile: it may have moved rel_path from src to dst.
    row = conn.execute("select rel_path from files where id=?", (file_id,)).fetchone()
    if conn.execute(
        "select 1 from moves where file_id=? and status='pending'", (file_id,)
    ).fetchone():
        raise HasPendingMove(f"file has a pending move; use release_pending first: {row[0]}")
    found = _found(root, row[0])
    if found is not None:
        raise StillOnDisk(f"file still on disk: {found}")
    _require_reachable(root, row[0])
    with conn:
        conn.execute("update files set purged=1 where id=?", (file_id,))
