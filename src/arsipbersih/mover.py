"""Journaled file moves: execute, crash reconcile, undo, empty-dir cleanup, space check.

This is the only module that moves media files. Its invariants:

- An existing path is never replaced: every destination is checked right before each move.
- A `moves` row with status `pending` is committed before the filesystem is touched, so
  `reconcile` can settle any crash from what is on disk.
- A cross-device move deletes the source only after the copy's SHA-256 matches it.
- A unit moves all-or-nothing: when one of its files fails, the files already moved go back.
- Besides the source of a verified cross-device move, the only paths ever deleted are
  `.partial` copies this module made, a destination copy whose SHA-256 equals its
  still-present source (`reconcile`), and empty directories (`remove_empty_dirs`).
"""

from __future__ import annotations

import errno
import os
import shutil
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from arsipbersih.db import new_id
from arsipbersih.hashing import sha256_file
from arsipbersih.layout import ARCHIVE_DIR, REVIEW_DIR, SOURCE_DIR, TRASH_DIR, long_path, to_abs
from arsipbersih.planner import FileMove, UnitMove

SPACE_RESERVE = 1 << 30  # bytes left free on a destination drive
PARTIAL_SUFFIX = ".partial"
LOCATION_BY_TOP = {
    SOURCE_DIR: "source",
    ARCHIVE_DIR: "archive",
    REVIEW_DIR: "review",
    TRASH_DIR: "trash",
}
_LOCATIONS = frozenset(LOCATION_BY_TOP.values())
_UNDOABLE_KINDS = ("rapikan", "apply")
_WIN_ERROR_NOT_SAME_DEVICE = 17
_UNSAFE_CHARS = ("\\", ":", "\x00")


class InsufficientSpace(Exception):
    """A destination drive lacks room for the cross-device copies of a batch."""


class MoveConflict(Exception):
    """A destination (or its .partial) already exists on disk or in the files table."""


class _CopyMismatch(OSError):
    """A cross-device copy does not hash the same as its source."""


@dataclass
class BatchResult:
    batch_id: str
    moved_units: int
    failed_units: int
    errors: list[str]


# --- paths -------------------------------------------------------------------------------


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _norm(p: Path) -> Path:
    # Absolute and normalized: the \\?\ prefix of long_path turns off Windows normalization.
    return Path(os.path.abspath(p))


def _abs(root: Path, rel: str) -> Path:
    """Absolute path for a DB path; refuses anything outside the four managed folders."""
    parts = rel.split("/") if rel else []
    if (
        len(parts) < 2
        or parts[0] not in LOCATION_BY_TOP
        or any(p in ("", ".", "..") or any(c in p for c in _UNSAFE_CHARS) for p in parts)
    ):
        raise ValueError(f"unsafe relative path: {rel!r}")
    return to_abs(root, rel)


def _location_of(rel: str) -> str | None:
    return LOCATION_BY_TOP.get(rel.split("/", 1)[0])


def _exists(p: Path) -> bool:
    return os.path.lexists(long_path(p))


def _partial(p: Path) -> Path:
    return p.with_name(p.name + PARTIAL_SUFFIX)


def _nearest_existing(p: Path) -> Path:
    p = Path(p)
    while not _exists(p) and p.parent != p:
        p = p.parent
    return p


def _same_device(a: Path, b: Path) -> bool:
    dev_a = os.stat(long_path(_nearest_existing(a))).st_dev
    dev_b = os.stat(long_path(_nearest_existing(b))).st_dev
    return dev_a == dev_b


def _maybe_same_file(a: Path, b: Path) -> bool:
    """True when a and b may be one file (same path ignoring case, or same inode).

    A missing path is no other file. Any other stat error counts as True, because callers
    use this to refuse a deletion.
    """
    if os.path.normcase(str(a)) == os.path.normcase(str(b)):
        return True
    try:
        sa, sb = os.stat(long_path(a)), os.stat(long_path(b))
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return sa.st_dev == sb.st_dev and sa.st_ino == sb.st_ino


def _is_cross_device(e: OSError) -> bool:
    return e.errno == errno.EXDEV or getattr(e, "winerror", None) == _WIN_ERROR_NOT_SAME_DEVICE


def _describe(e: BaseException) -> str:
    text = f"{type(e).__name__}: {e}"
    for note in getattr(e, "__notes__", ()):
        text += f" ({note})"
    return text


# --- single-file move ------------------------------------------------------------------


def _cleanup_partial(partial: Path, exc: BaseException) -> None:
    """Delete a .partial this call created, after a failure; a leftover is noted on exc."""
    try:
        os.unlink(long_path(partial))
    except FileNotFoundError:
        pass
    except OSError as err:
        exc.add_note(f"could not remove {partial}: {err}")


def _copy_verify_move(src: Path, dst: Path) -> None:
    """Cross-device move: copy to <dst>.partial, verify SHA-256, rename into place, delete src."""
    partial = _partial(dst)
    if _exists(partial):
        raise MoveConflict(f"partial file exists: {partial}")
    try:
        shutil.copy2(long_path(src), long_path(partial))
        if sha256_file(partial) != sha256_file(src):
            raise _CopyMismatch(f"copy does not match its source: {src}")
        if _exists(dst):
            raise MoveConflict(f"destination exists: {dst}")
        os.rename(long_path(partial), long_path(dst))
    except BaseException as e:
        _cleanup_partial(partial, e)
        raise
    try:
        os.unlink(long_path(src))
    except OSError as e:
        if not _exists(src):
            return  # the source is gone after all, so the move is complete
        # The source stays, so take the verified copy back out: rename it back to the
        # .partial it was (and is never a user file), then delete that.
        try:
            os.rename(long_path(dst), long_path(partial))
        except OSError as err:
            e.add_note(f"verified copy left at {dst}: {err}")
        else:
            _cleanup_partial(partial, e)
        raise


def _move_file(src: Path, dst: Path) -> None:
    """Move one regular file; never replaces an existing path."""
    st = os.lstat(long_path(src))  # FileNotFoundError when the source is missing
    if not stat.S_ISREG(st.st_mode):
        raise OSError(f"not a regular file: {src}")
    if _exists(dst):
        raise MoveConflict(f"destination exists: {dst}")
    os.makedirs(long_path(dst.parent), exist_ok=True)
    if _same_device(src, dst):
        if _exists(dst):  # re-check right before rename (POSIX rename would replace dst)
            raise MoveConflict(f"destination exists: {dst}")
        try:
            os.rename(long_path(src), long_path(dst))  # Windows: fails if dst exists
            return
        except OSError as e:
            if not _is_cross_device(e):
                raise
    _copy_verify_move(src, dst)


# --- DB helpers --------------------------------------------------------------------------


def _tracked(
    conn: sqlite3.Connection, rel: str, except_id: str | None = None, *, nocase: bool = False
) -> bool:
    """Whether a files row (other than except_id) holds rel."""
    op = "= ? collate nocase" if nocase else "= ?"
    row = conn.execute(
        f"select 1 from files where rel_path {op} and id is not ? limit 1", (rel, except_id)
    ).fetchone()
    return row is not None


def _set_rel_path(conn: sqlite3.Connection, file_id: str, rel: str) -> None:
    # Another row already holding rel (e.g. an inventory run between a crash and reconcile)
    # keeps it: rel_path is UNIQUE, and that row tracks the same physical file.
    if not _tracked(conn, rel, file_id):
        conn.execute("update files set rel_path=?, missing=0 where id=?", (rel, file_id))


def _record_done(conn, row_id: str, fm: FileMove, done_at: str, original_id: str | None) -> None:
    conn.execute("update moves set status='done', done_at=? where id=?", (done_at, row_id))
    _set_rel_path(conn, fm.file_id, fm.dst_rel)
    if original_id is not None:
        conn.execute("update moves set status='undone' where id=?", (original_id,))


def _check_unit(conn: sqlite3.Connection, root: Path, um: UnitMove) -> None:
    """Refuse a stale or unsafe plan before anything moves."""
    if um.to_location not in _LOCATIONS:
        raise ValueError(f"unknown location: {um.to_location!r}")
    for fm in um.files:
        _abs(root, fm.src_rel)
        _abs(root, fm.dst_rel)
        row = conn.execute(
            "select rel_path, purged from files where id=?", (fm.file_id,)
        ).fetchone()
        if row is None or row[1] or row[0] != fm.src_rel:
            raise ValueError(f"files record does not match {fm.src_rel}")
        if _tracked(conn, fm.dst_rel, fm.file_id):
            raise MoveConflict(f"destination already in the files table: {fm.dst_rel}")


# --- batches -----------------------------------------------------------------------------


def _rollback(root: Path, um: UnitMove, moved: list[int], errors: list[str]) -> set[int]:
    """Move the already-moved files of a failed unit back. Returns those that stayed put."""
    stuck: set[int] = set()
    for i in reversed(moved):
        fm = um.files[i]
        try:
            _move_file(_abs(root, fm.dst_rel), _abs(root, fm.src_rel))
        except Exception as e:  # noqa: BLE001 - recorded; the file stays at dst
            stuck.add(i)
            errors.append(f"rollback failed, file left at {fm.dst_rel}: {_describe(e)}")
    return stuck


def _run_unit(
    conn: sqlite3.Connection,
    root: Path,
    batch_id: str,
    um: UnitMove,
    originals: list[str] | None,
) -> list[str]:
    """Move one unit all-or-nothing. Returns its errors (empty on success)."""
    row_ids = [new_id() for _ in um.files]
    with conn:  # journal first: committed before the filesystem is touched
        conn.executemany(
            "insert into moves (id, batch_id, unit_id, file_id, src_rel, dst_rel, status,"
            " created_at) values (?, ?, ?, ?, ?, ?, 'pending', ?)",
            [
                (rid, batch_id, um.unit_id, fm.file_id, fm.src_rel, fm.dst_rel, _now())
                for rid, fm in zip(row_ids, um.files)
            ],
        )
    moved: list[int] = []
    current: FileMove | None = None
    try:
        _check_unit(conn, root, um)
        for i, fm in enumerate(um.files):
            current = fm
            _move_file(_abs(root, fm.src_rel), _abs(root, fm.dst_rel))
            moved.append(i)
    except BaseException as e:
        where = f"{current.src_rel} -> {current.dst_rel}" if current else f"unit {um.unit_id}"
        errors = [f"{where}: {_describe(e)}"]
        stuck = _rollback(root, um, moved, errors)
        done_at = _now()
        with conn:
            for i, (rid, fm) in enumerate(zip(row_ids, um.files)):
                if i in stuck:  # truthful: the file really is at dst now
                    _record_done(conn, rid, fm, done_at, originals[i] if originals else None)
                else:
                    conn.execute("update moves set status='failed' where id=?", (rid,))
        if not isinstance(e, Exception):  # Ctrl+C and the like: unit settled, now stop
            raise
        return errors
    done_at = _now()
    # A DB error here propagates; the rows stay pending and reconcile settles them.
    with conn:
        for i, (rid, fm) in enumerate(zip(row_ids, um.files)):
            _record_done(conn, rid, fm, done_at, originals[i] if originals else None)
        conn.execute("update units set location=? where id=?", (um.to_location, um.unit_id))
    return []


def _run_batch(
    conn: sqlite3.Connection,
    root: Path,
    kind: str,
    label: str,
    plan: list[tuple[UnitMove, list[str] | None]],
) -> BatchResult:
    check_space(root, [um for um, _ in plan])
    batch_id = new_id()
    with conn:
        conn.execute(
            "insert into batches (id, kind, label, created_at) values (?, ?, ?, ?)",
            (batch_id, kind, label, _now()),
        )
    result = BatchResult(batch_id, 0, 0, [])
    for um, originals in plan:
        if not um.files:
            continue
        errors = _run_unit(conn, root, batch_id, um, originals)
        if errors:
            result.failed_units += 1
            result.errors.extend(errors)
        else:
            result.moved_units += 1
    return result


def check_space(root: Path, moves: list[UnitMove]) -> None:
    """Raise InsufficientSpace when cross-device copies would leave < 1 GiB free on a drive."""
    root = _norm(root)
    needed: dict[int, list] = {}  # st_dev -> [bytes, existing dir on that drive]
    for um in moves:
        for fm in um.files:
            src, dst = to_abs(root, fm.src_rel), to_abs(root, fm.dst_rel)
            try:
                size = os.stat(long_path(src)).st_size
            except OSError:
                continue  # a missing source fails in execute and needs no space
            if _same_device(src, dst):
                continue
            probe = _nearest_existing(dst.parent)
            slot = needed.setdefault(os.stat(long_path(probe)).st_dev, [0, probe])
            slot[0] += size
    for need, probe in needed.values():
        free = shutil.disk_usage(long_path(probe)).free
        if need > free - SPACE_RESERVE:
            raise InsufficientSpace(
                f"{need} bytes to copy onto the drive of {probe}, {free} free,"
                f" {SPACE_RESERVE} kept in reserve"
            )


def execute(
    conn: sqlite3.Connection, root: Path, moves: list[UnitMove], kind: str, label: str
) -> BatchResult:
    """Move units as one journaled batch. Raises InsufficientSpace before moving anything."""
    return _run_batch(conn, _norm(root), kind, label, [(um, None) for um in moves])


# --- reconcile ---------------------------------------------------------------------------


def _remove_stray_partials(
    conn, root: Path, src_rel: str, dst_rel: str, src_ok: bool, dst_ok: bool, dst_ours: bool
) -> None:
    """Delete the .partial an interrupted row left behind.

    A .partial of this module exists only while its own path is missing: <dst>.partial is
    a copy of src (forward copy), <src>.partial a copy of dst (rollback copy). So it is
    deleted only in that state and only while the other endpoint holds the file. A path
    the files table knows is a user file and is kept.
    """
    src, dst = _abs(root, src_rel), _abs(root, dst_rel)
    candidates = []
    if src_ok and not dst_ok:
        candidates.append(dst_rel + PARTIAL_SUFFIX)
    if dst_ours and not src_ok:
        candidates.append(src_rel + PARTIAL_SUFFIX)
    for rel in candidates:
        partial = _abs(root, rel)
        if (
            _exists(partial)
            and not _tracked(conn, rel, nocase=True)
            and not _maybe_same_file(partial, src)
            and not _maybe_same_file(partial, dst)
        ):
            os.unlink(long_path(partial))


def _size_matches(conn, file_id: str, p: Path) -> bool:
    row = conn.execute("select size from files where id=?", (file_id,)).fetchone()
    return row is None or row[0] is None or os.stat(long_path(p)).st_size == row[0]


def _mark_original_undone(conn, file_id: str, undo_src: str, undo_dst: str) -> None:
    conn.execute(
        "update moves set status='undone' where id = ("
        " select m.id from moves m join batches b on b.id = m.batch_id"
        " where m.file_id=? and m.src_rel=? and m.dst_rel=? and m.status='done'"
        " and b.kind in ('rapikan','apply') order by m.rowid desc limit 1)",
        (file_id, undo_dst, undo_src),
    )


def _settle_location(conn, batch_id: str, unit_id: str | None) -> None:
    """Set the unit's location once every row of it in the batch is done."""
    rows = conn.execute(
        "select status, dst_rel from moves where batch_id=? and unit_id is ?", (batch_id, unit_id)
    ).fetchall()
    if rows and all(r[0] == "done" for r in rows):
        location = _location_of(rows[0][1])
        if location is not None:
            conn.execute("update units set location=? where id=?", (location, unit_id))


def reconcile(conn: sqlite3.Connection, root: Path) -> int:
    """Settle `pending` rows left by a crash from what is on disk. Returns rows settled.

    A row that cannot be settled now (the file is at neither path, e.g. a drive is
    disconnected, or an I/O error) stays pending and is retried on the next run.
    """
    root = _norm(root)
    rows = conn.execute(
        "select m.id, m.batch_id, m.unit_id, m.file_id, m.src_rel, m.dst_rel, b.kind"
        " from moves m join batches b on b.id = m.batch_id"
        " where m.status='pending' order by m.rowid"
    ).fetchall()
    fixed = 0
    for row_id, batch_id, unit_id, file_id, src_rel, dst_rel, kind in rows:
        try:
            src, dst = _abs(root, src_rel), _abs(root, dst_rel)
        except ValueError:
            # Such a row fails validation before any move, so nothing moved.
            with conn:
                conn.execute("update moves set status='failed' where id=?", (row_id,))
            fixed += 1
            continue
        try:
            src_ok, dst_ok = _exists(src), _exists(dst)
            dst_ours = dst_ok and _size_matches(conn, file_id, dst)
            if not (src_ok or dst_ours):
                continue  # the file is at neither path: stays pending, see docstring
            # Disk first, then the row: a crash in between leaves the row pending.
            _remove_stray_partials(conn, root, src_rel, dst_rel, src_ok, dst_ok, dst_ours)
            if dst_ours and not src_ok:
                with conn:
                    _record_done(conn, row_id, FileMove(file_id, src_rel, dst_rel), _now(), None)
                    if kind == "undo":
                        _mark_original_undone(conn, file_id, src_rel, dst_rel)
                    _settle_location(conn, batch_id, unit_id)
            else:
                if (
                    src_ok
                    and dst_ok
                    and not _maybe_same_file(src, dst)
                    and not _tracked(conn, dst_rel, nocase=True)
                    and sha256_file(dst) == sha256_file(src)
                ):
                    os.unlink(long_path(dst))  # our verified copy; the original is at src
                with conn:
                    conn.execute("update moves set status='failed' where id=?", (row_id,))
        except OSError:
            continue  # stays pending: see docstring
        fixed += 1
    return fixed


# --- undo --------------------------------------------------------------------------------


def _undo(conn: sqlite3.Connection, root: Path, batch_ids: list[str], label: str) -> BatchResult:
    plan: list[tuple[UnitMove, list[str] | None]] = []
    for batch_id in batch_ids:
        rows = conn.execute(
            "select m.id, m.file_id, m.src_rel, m.dst_rel, coalesce(f.unit_id, m.unit_id)"
            " from moves m left join files f on f.id = m.file_id"
            " where m.batch_id=? and m.status='done' order by m.rowid desc",
            (batch_id,),
        ).fetchall()
        by_unit: dict[str | None, list[tuple[str, FileMove]]] = {}
        for row_id, file_id, src_rel, dst_rel, unit_id in rows:
            by_unit.setdefault(unit_id, []).append((row_id, FileMove(file_id, dst_rel, src_rel)))
        for unit_id, items in by_unit.items():
            back_to = _location_of(items[0][1].dst_rel) or ""
            unit_move = UnitMove(unit_id, back_to, [fm for _, fm in items])
            plan.append((unit_move, [row_id for row_id, _ in items]))
    return _run_batch(conn, _norm(root), "undo", label, plan)


def undo_batch(conn: sqlite3.Connection, root: Path, batch_id: str) -> BatchResult:
    """Reverse the done rows of a rapikan/apply batch as a new `undo` batch.

    Each unit goes back all-or-nothing; a unit whose original path is occupied, or whose
    file has moved on since, is skipped and reported in `errors`.
    """
    row = conn.execute("select kind from batches where id=?", (batch_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown batch: {batch_id}")
    if row[0] not in _UNDOABLE_KINDS:
        raise ValueError(f"a {row[0]!r} batch cannot be undone")
    return _undo(conn, root, [batch_id], f"undo {batch_id}")


def undo_all(conn: sqlite3.Connection, root: Path) -> BatchResult:
    """Undo every rapikan/apply batch that still has done rows, newest first, as one batch."""
    batch_ids = [
        r[0]
        for r in conn.execute(
            "select b.id from batches b where b.kind in ('rapikan','apply')"
            " and exists (select 1 from moves m where m.batch_id = b.id and m.status='done')"
            " order by b.rowid desc"
        )
    ]
    return _undo(conn, root, batch_ids, "undo all")


# --- cleanup -----------------------------------------------------------------------------


def remove_empty_dirs(base: Path) -> int:
    """Remove empty directories below base, bottom-up. Returns how many were removed.

    base itself is never removed. os.rmdir refuses a non-empty directory, so a directory
    holding any file is kept. Links and junctions are neither followed nor removed.
    """
    base = _norm(base)
    dirs: list[Path] = []
    stack = [base]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(long_path(cur)) as it:
                for entry in it:
                    if entry.is_symlink() or entry.is_junction():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        child = cur / entry.name
                        dirs.append(child)
                        stack.append(child)
        except OSError:
            continue  # unreadable: its own dirs stay unlisted, so its parents stay non-empty
    removed = 0
    for d in reversed(dirs):  # every child was listed after its parent
        try:
            os.rmdir(long_path(d))
        except OSError:
            continue  # not empty, or not removable: kept
        removed += 1
    return removed
