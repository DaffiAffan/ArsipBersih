"""Owner review decisions: record, apply per category, and the one permanent delete.

`purge_trash` is the only function in the codebase allowed to delete a media file.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from arsipbersih.classify import GRID_CATEGORIES, GROUP_CATEGORIES
from arsipbersih.db import new_id
from arsipbersih.layout import TRASH_DIR, long_path
from arsipbersih.mover import (
    BatchResult,
    _abs,
    execute,
    reconcile_or_refuse,
    remove_empty_dirs,
)
from arsipbersih.planner import plan_apply

log = logging.getLogger(__name__)
_TRASH_PREFIX = TRASH_DIR + "/"
_TRASH_PREFIX_LEN = len(_TRASH_PREFIX)
_CHUNK = 500  # ids per IN (...) list, far below SQLite's variable limit


class PurgeMismatch(Exception):
    """The trash no longer holds what the owner confirmed."""


@dataclass
class ApplyPreview:
    keep: int
    discard: int
    unseen: int


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --- recording ---------------------------------------------------------------------------


_UPSERT_DECISION = (
    "insert into decisions (unit_id, decision, decided_at, applied) values (?, ?, ?, 0)"
    " on conflict(unit_id) do update set decision=excluded.decision,"
    " decided_at=excluded.decided_at, applied=0"
)


def record(conn: sqlite3.Connection, unit_id: str, decision: Literal["keep", "discard"]) -> None:
    with conn:
        conn.execute(_UPSERT_DECISION, (unit_id, decision, _now()))


def record_bulk_discard(conn: sqlite3.Connection, unit_ids: list[str]) -> int:
    """Discard every grid-category unit still in review that has no unapplied decision,
    in one transaction. Group categories are decided per group, never in bulk. An explicit Keep or Delete the owner has not applied yet is never
    overwritten. Returns the number of decisions recorded."""
    ids = list(dict.fromkeys(unit_ids))
    grid = sorted(GRID_CATEGORIES)
    cats = ",".join("?" * len(grid))
    now = _now()
    n = 0
    with conn:
        for i in range(0, len(ids), _CHUNK):
            part = ids[i : i + _CHUNK]
            marks = ",".join("?" * len(part))
            todo = [
                r[0]
                for r in conn.execute(
                    f"select u.id from units u where u.id in ({marks}) and u.location='review'"
                    f" and u.primary_category in ({cats})"
                    " and not exists (select 1 from decisions d where d.unit_id=u.id"
                    " and d.applied=0)",
                    [*part, *grid],
                )
            ]
            conn.executemany(_UPSERT_DECISION, [(uid, "discard", now) for uid in todo])
            n += len(todo)
    return n


def clear(conn: sqlite3.Connection, unit_id: str) -> None:
    with conn:
        conn.execute("delete from decisions where unit_id=?", (unit_id,))


def mark_seen(conn: sqlite3.Connection, unit_ids: list[str]) -> None:
    with conn:
        conn.executemany("update units set seen=1 where id=?", [(u,) for u in unit_ids])


def accept_suggestions(conn: sqlite3.Connection, group_ids: list[str]) -> int:
    """Keeper -> keep, every other member -> discard, but only for units without an
    unapplied decision: an explicit choice the owner made and has not applied yet always
    wins over the suggestion. Returns the number of decisions actually recorded."""
    n = 0
    for gid in group_ids:
        g = conn.execute(
            "select suggested_keep_unit_id from groups where id=?", (gid,)
        ).fetchone()
        if g is None or g[0] is None:
            continue
        keeper = g[0]
        members = [
            r[0]
            for r in conn.execute("select unit_id from group_members where group_id=?", (gid,))
        ]
        wanted = {keeper: "keep"}
        for uid in members:
            if uid != keeper:
                wanted[uid] = "discard"
        for uid, decision in wanted.items():
            pending = conn.execute(
                "select 1 from decisions where unit_id=? and applied=0", (uid,)
            ).fetchone()
            if pending is not None:
                continue
            record(conn, uid, decision)
            n += 1
    return n


# --- apply -------------------------------------------------------------------------------


def _group_decisions(conn: sqlite3.Connection, category: str) -> tuple[list[str], list[str]]:
    rows = conn.execute(
        "select d.unit_id, d.decision from decisions d where d.applied=0 and d.unit_id in ("
        " select m.unit_id from group_members m join groups g on g.id=m.group_id where g.kind=?"
        " union select g.suggested_keep_unit_id from groups g where g.kind=?"
        " and g.suggested_keep_unit_id is not null) order by d.rowid",
        (category, category),
    ).fetchall()
    keep = [r[0] for r in rows if r[1] == "keep"]
    discard = [r[0] for r in rows if r[1] == "discard"]
    return keep, discard


def _grid_lists(conn: sqlite3.Connection, category: str) -> tuple[list[str], list[str], int]:
    rows = conn.execute(
        "select u.id, u.seen, d.decision from units u left join decisions d on d.unit_id=u.id"
        " where u.primary_category=? and u.location='review' order by u.rowid",
        (category,),
    ).fetchall()
    keep, discard, unseen = [], [], 0
    for uid, seen, decision in rows:
        if decision == "keep":
            keep.append(uid)
        elif decision == "discard" or seen:
            discard.append(uid)
        else:
            unseen += 1
    return keep, discard, unseen


def _lists(conn: sqlite3.Connection, category: str) -> tuple[list[str], list[str], int]:
    if category in GROUP_CATEGORIES:
        keep, discard = _group_decisions(conn, category)
        return keep, discard, 0
    if category in GRID_CATEGORIES:
        return _grid_lists(conn, category)
    raise ValueError(f"unknown category: {category!r}")


def preview_apply(conn: sqlite3.Connection, category: str) -> ApplyPreview:
    keep, discard, unseen = _lists(conn, category)
    return ApplyPreview(len(keep), len(discard), unseen)


def _locations(conn: sqlite3.Connection, ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    out: dict[str, str] = {}
    for i in range(0, len(ids), _CHUNK):
        part = ids[i : i + _CHUNK]
        marks = ",".join("?" * len(part))
        out.update(conn.execute(f"select id, location from units where id in ({marks})", part))
    return out


def apply_category(conn: sqlite3.Connection, root: Path, category: str) -> BatchResult:
    """Move a category's decided units. Raises PendingMoves while the journal is unsettled."""
    journal_errors: list[str] = []
    reconcile_or_refuse(conn, root, journal_errors)
    keep, discard, _ = _lists(conn, category)
    skipped: list[tuple[str, str]] = []
    moves = plan_apply(conn, root, keep, discard, skipped)
    result = execute(conn, root, moves, "apply", f"apply {category}")
    result.errors.extend(f"journal: {e}" for e in journal_errors)
    result.errors.extend(f"skipped {uid}: {reason}" for uid, reason in skipped)
    result.skipped_units = len(skipped)
    try:
        _settle(conn, keep, discard)
    except Exception as e:  # noqa: BLE001 - the files already moved; never lose the result
        result.errors.append(f"could not record applied decisions: {type(e).__name__}: {e}")
    return result


def _settle(conn: sqlite3.Connection, keep: list[str], discard: list[str]) -> None:
    now = _now()
    with conn:
        for ids, target in ((keep, "archive"), (discard, "trash")):
            decision = "keep" if target == "archive" else "discard"
            for uid, loc in _locations(conn, ids).items():
                if loc != target:
                    continue
                # A grid discard may be implicit (seen, no row): the row is written only now.
                # A decision the owner changed mid-apply is left unapplied.
                conn.execute(
                    "insert into decisions (unit_id, decision, decided_at, applied)"
                    " values (?, ?, ?, 1) on conflict(unit_id) do update set applied=1"
                    " where decisions.decision = excluded.decision",
                    (uid, decision, now),
                )


# --- purge -------------------------------------------------------------------------------


def trash_totals(conn: sqlite3.Connection) -> tuple[int, int]:
    row = conn.execute(
        "select count(*), coalesce(sum(f.size), 0) from files f join units u on u.id=f.unit_id"
        " where u.location='trash' and f.purged=0 and f.missing=0"
        f" and substr(f.rel_path,1,{_TRASH_PREFIX_LEN})='{_TRASH_PREFIX}'"
    ).fetchone()
    return int(row[0]), int(row[1])


class _SizeChanged(Exception):
    pass


def _delete(path: Path) -> None:
    p = long_path(path)
    try:
        os.unlink(p)
    except PermissionError:
        # Allowed only here: the owner explicitly chose to delete this file.
        mode = os.stat(p).st_mode
        if mode & stat.S_IWRITE:
            raise  # not a read-only problem
        os.chmod(p, mode | stat.S_IWRITE)
        os.unlink(p)


def purge_trash(
    conn: sqlite3.Connection,
    root: Path,
    expected_files: int,
    expected_bytes: int,
    errors: list[str] | None = None,
) -> int:
    """Permanently delete every file of every trashed unit. Returns files deleted.

    The journal is reconciled first (PendingMoves while a move stays unsettled), so the
    totals are compared against the settled trash. Reconcile problems go to `errors`,
    or to the log when no list is passed.
    """
    journal_errors: list[str] = []
    reconcile_or_refuse(conn, root, journal_errors)
    for e in journal_errors:
        if errors is not None:
            errors.append(f"journal: {e}")
        else:
            log.warning("journal: %s", e)
    totals = trash_totals(conn)
    if totals != (expected_files, expected_bytes):
        raise PurgeMismatch(f"trash holds {totals}, expected {(expected_files, expected_bytes)}")
    root = Path(os.path.abspath(root))
    rows = conn.execute(
        "select f.id, f.unit_id, f.rel_path, f.size from files f join units u on u.id=f.unit_id"
        " where u.location='trash' and f.purged=0 and f.missing=0"
        f" and substr(f.rel_path,1,{_TRASH_PREFIX_LEN})='{_TRASH_PREFIX}' order by f.rowid"
    ).fetchall()
    if not rows:
        return 0
    if not os.path.isdir(long_path(root / TRASH_DIR)):
        raise PurgeMismatch("trash folder not found: is the drive connected?")
    batch_id = new_id()
    with conn:
        conn.execute(
            "insert into batches (id, kind, label, created_at) values (?, 'purge', 'purge trash', ?)",
            (batch_id, _now()),
        )
    deleted = 0
    for file_id, unit_id, rel, size in rows:
        try:
            if not rel.startswith(TRASH_DIR + "/"):
                raise ValueError(f"not in {TRASH_DIR}: {rel!r}")
            target = _abs(root, rel)
            try:
                if size is not None:
                    on_disk = os.stat(long_path(target)).st_size
                    if on_disk != size:
                        raise _SizeChanged(
                            f"size on disk {on_disk} differs from recorded {size}, not deleted"
                        )
                _delete(target)
            except FileNotFoundError:
                # Already gone (crash after unlink, or emptied by hand): settle the row, but
                # only while its folder exists; otherwise a drive may be unplugged.
                if not os.path.isdir(long_path(target.parent)):
                    raise
        except Exception as e:  # noqa: BLE001 - reported; the file stays unpurged
            log.warning("could not purge %s: %s: %s", rel, type(e).__name__, e)
            if errors is not None:
                if isinstance(e, _SizeChanged):
                    errors.append(f"{rel}: {e}")
                else:
                    errors.append(f"{rel}: {type(e).__name__}: {e}")
            continue
        with conn:
            conn.execute(
                "insert into moves (id, batch_id, unit_id, file_id, src_rel, dst_rel, status,"
                " created_at, done_at) values (?, ?, ?, ?, ?, ?, 'purged', ?, ?)",
                (new_id(), batch_id, unit_id, file_id, rel, rel, _now(), _now()),
            )
            conn.execute("update files set purged=1 where id=?", (file_id,))
        deleted += 1
    remove_empty_dirs(root / TRASH_DIR)
    return deleted
