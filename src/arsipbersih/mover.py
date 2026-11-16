"""Journaled file moves: execute, crash reconcile, undo, empty-dir cleanup, space check.

This is the only module that moves media files. Its invariants:

- An existing path is never replaced: every destination is checked right before each move.
- A `moves` row with status `pending` is committed before the filesystem is touched, so
  `reconcile` can settle any crash from what is on disk.
- A cross-device move deletes the source only after the copy is fsynced and its SHA-256
  matches it.
- A unit moves all-or-nothing: when one of its files fails, the files already moved go
  back, and `reconcile` rejoins a unit that a hard crash left split.
- Besides the source of a verified cross-device move, the only paths ever deleted are
  `.partial` copies this module made (after a failure, or in `reconcile` when the copy is a
  byte-prefix of the surviving file), a destination copy whose SHA-256 equals its
  still-present source (`reconcile`), and empty directories (`remove_empty_dirs`).
"""

from __future__ import annotations

import errno
import hashlib
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
COPY_CHUNK = 1 << 20
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


class PendingMoves(Exception):
    """Interrupted moves are still unsettled after reconcile, so no new batch may start."""


class UnknownBatch(ValueError):
    """No batch has this id."""


class NotUndoable(ValueError):
    """The batch kind (undo, purge) cannot be undone."""


class _CopyMismatch(OSError):
    """A cross-device copy does not hash the same as its source."""


@dataclass
class BatchResult:
    batch_id: str
    moved_units: int
    failed_units: int
    errors: list[str]
    skipped_units: int = 0  # planned out before the batch (nothing moved for them)


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


def _is_prefix_of(part: Path, whole: Path) -> bool:
    """Whether the bytes of part are a prefix of (or equal to) the bytes of whole."""
    if os.stat(long_path(part)).st_size > os.stat(long_path(whole)).st_size:
        return False
    with open(long_path(part), "rb") as a, open(long_path(whole), "rb") as b:
        while block := a.read(COPY_CHUNK):
            if b.read(len(block)) != block:
                return False
    return True


def _require_deletable(src: Path) -> None:
    """Fail before copying when a cross-device move could not delete its source."""
    if os.name == "nt":  # the read-only attribute blocks DeleteFile
        blocked = not os.stat(long_path(src)).st_mode & stat.S_IWRITE
    else:  # POSIX: deleting needs write access to the directory
        blocked = not os.access(long_path(src.parent), os.W_OK | os.X_OK)
    if blocked:
        raise PermissionError(f"sumber read-only: {src}")


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
        if _exists(partial):
            # Our own file only: clear a read-only bit copystat may have carried over.
            os.chmod(long_path(partial), stat.S_IWRITE | stat.S_IREAD)
            os.unlink(long_path(partial))
    except OSError as err:
        exc.add_note(f"could not remove {partial}: {err}")


def _copy_verify_move(src: Path, dst: Path) -> None:
    """Cross-device move: copy to <dst>.partial, fsync, verify SHA-256, rename, delete src."""
    _require_deletable(src)
    partial = _partial(dst)
    try:
        out = open(long_path(partial), "xb")  # exclusive create: never truncates a file
    except FileExistsError:
        raise MoveConflict(f"partial file exists: {partial}") from None
    try:
        src_hash = hashlib.sha256()
        with out, open(long_path(src), "rb") as f:
            while block := f.read(COPY_CHUNK):
                src_hash.update(block)
                out.write(block)
            out.flush()
            os.fsync(out.fileno())  # durable before the source can be deleted
        shutil.copystat(long_path(src), long_path(partial))
        if sha256_file(partial) != src_hash.hexdigest():
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
        src, dst = _abs(root, fm.src_rel), _abs(root, fm.dst_rel)
        if _exists(src) and not _same_device(src, dst):
            _require_deletable(src)  # fail the unit up front instead of copying
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
    after_batch=None,
) -> BatchResult:
    plan = [(um, originals) for um, originals in plan if um.files]
    if not plan:
        return BatchResult("", 0, 0, [])  # nothing to move: no empty batch row
    check_space(root, [um for um, _ in plan])
    batch_id = new_id()
    with conn:
        conn.execute(
            "insert into batches (id, kind, label, created_at) values (?, ?, ?, ?)",
            (batch_id, kind, label, _now()),
        )
    result = BatchResult(batch_id, 0, 0, [])
    try:
        for um, originals in plan:
            errors = _run_unit(conn, root, batch_id, um, originals)
            if errors:
                result.failed_units += 1
                result.errors.extend(errors)
            else:
                result.moved_units += 1
    finally:
        if after_batch is not None:
            after_batch(batch_id)  # also on Ctrl+C: restored units must not keep stale state
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
    """Move units as one journaled batch. Raises InsufficientSpace before moving anything.

    With nothing to move, no batch row is written and `batch_id` is empty.
    """
    return _run_batch(conn, _norm(root), kind, label, [(um, None) for um in moves])


# --- reconcile ---------------------------------------------------------------------------


def _remove_stray_partials(
    conn,
    root: Path,
    src_rel: str,
    dst_rel: str,
    src_ok: bool,
    dst_ok: bool,
    dst_ours: bool,
    errors: list[str],
) -> None:
    """Delete the .partial an interrupted move left behind, when it holds nothing unique.

    <dst>.partial is a forward copy of src and <src>.partial a rollback copy of dst; each
    exists only while its own path is missing. A candidate is deleted only in that state,
    only when its bytes are a prefix of (or equal to) the endpoint holding the file, and
    never when the files table knows its path. Anything else is left in place. A failed
    deletion is reported; the leftover then only blocks a later move to the same path.
    """
    src, dst = _abs(root, src_rel), _abs(root, dst_rel)
    candidates = []
    if src_ok and not dst_ok:
        candidates.append((dst_rel + PARTIAL_SUFFIX, src))
    if dst_ours and not src_ok:
        candidates.append((src_rel + PARTIAL_SUFFIX, dst))
    for rel, survivor in candidates:
        partial = _abs(root, rel)
        try:
            if (
                _exists(partial)
                and not _tracked(conn, rel, nocase=True)
                and not _maybe_same_file(partial, src)
                and not _maybe_same_file(partial, dst)
                and _is_prefix_of(partial, survivor)
            ):
                os.unlink(long_path(partial))
        except OSError as e:
            errors.append(f"{rel}: left in place: {_describe(e)}")


def _reachable(p: Path) -> bool:
    """Whether p's directory is online; isdir follows junctions to their target."""
    return os.path.isdir(long_path(p.parent))


def _adopt_partial(
    conn, root: Path, file_id: str, candidates: tuple[tuple[str, str], ...], errors: list[str]
) -> str | None:
    """Rename a complete <target>.partial into place while the file is at neither endpoint.

    A copy is fsynced and verified before its source is deleted, so a .partial whose size
    (and SHA-256, when known) equals the file's record is the file itself, left behind
    when the final rename was lost. That holds only while the directory the file left is
    reachable: an offline drive or junction target also makes a path "missing", and a copy
    cut off before its fsync can be full-size with an unwritten tail. Each candidate is
    (target_rel, left_rel). Returns the rel the file now sits at, or None.
    """
    row = conn.execute("select size, sha256 from files where id=?", (file_id,)).fetchone()
    if row is None or row[0] is None:
        return None
    size, sha = row
    for target_rel, left_rel in candidates:
        target = _abs(root, target_rel)
        partial = _partial(target)
        if not (
            _exists(partial)
            and not _exists(target)
            and not _tracked(conn, target_rel + PARTIAL_SUFFIX, nocase=True)
            and os.stat(long_path(partial)).st_size == size
        ):
            continue
        if not (_reachable(_abs(root, left_rel)) and _reachable(target)):
            errors.append(
                f"{left_rel}: endpoint offline, not adopting {target_rel}{PARTIAL_SUFFIX}"
            )
            continue
        if sha is not None and sha256_file(partial) != sha:
            continue
        os.rename(long_path(partial), long_path(target))  # Windows: never replaces
        return target_rel
    return None


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


def _revert_original(conn, file_id: str, undo_src: str, undo_dst: str) -> None:
    conn.execute(
        "update moves set status='done' where id = ("
        " select m.id from moves m join batches b on b.id = m.batch_id"
        " where m.file_id=? and m.src_rel=? and m.dst_rel=? and m.status='undone'"
        " and b.kind in ('rapikan','apply') order by m.rowid desc limit 1)",
        (file_id, undo_dst, undo_src),
    )


def _settle_row(conn, root: Path, row, errors: list[str]) -> bool:
    """Settle one pending row from what is on disk. False when it must stay pending."""
    row_id, batch_id, unit_id, file_id, src_rel, dst_rel, kind = row
    src, dst = _abs(root, src_rel), _abs(root, dst_rel)
    src_ok, dst_ok = _exists(src), _exists(dst)
    dst_ours = dst_ok and _size_matches(conn, file_id, dst)
    if not (src_ok or dst_ours):
        candidates = ((dst_rel, src_rel), (src_rel, dst_rel))
        if _adopt_partial(conn, root, file_id, candidates, errors) is None:
            errors.append(f"{src_rel} -> {dst_rel}: file at neither path, left pending")
            return False
        src_ok, dst_ok = _exists(src), _exists(dst)
        dst_ours = dst_ok and _size_matches(conn, file_id, dst)
    # Disk first, then the row: a crash in between leaves the row pending.
    _remove_stray_partials(conn, root, src_rel, dst_rel, src_ok, dst_ok, dst_ours, errors)
    if dst_ours and not src_ok:
        with conn:
            _record_done(conn, row_id, FileMove(file_id, src_rel, dst_rel), _now(), None)
            if kind == "undo":
                _mark_original_undone(conn, file_id, src_rel, dst_rel)
            _settle_location(conn, batch_id, unit_id)
        return True
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
    return True


def _return_to_source(
    conn, root: Path, file_id: str, src_rel: str, dst_rel: str, errors: list[str]
) -> bool:
    """Put a done file of a split unit back at src. False when it is skipped."""
    row = conn.execute("select rel_path, purged from files where id=?", (file_id,)).fetchone()
    if row is None or row[1]:
        return False  # purged since
    if row[0] == src_rel:  # the done row's rel_path update was refused (UNIQUE clash)
        errors.append(f"{dst_rel}: unit stays split, its files record still points at {src_rel}")
        return False
    if row[0] != dst_rel:
        return False  # moved on since: a later batch owns its location now
    src, dst = _abs(root, src_rel), _abs(root, dst_rel)
    src_ok, dst_ok = _exists(src), _exists(dst)
    if src_ok and not dst_ok and _size_matches(conn, file_id, src):
        return True  # it went back before a crash; only the DB is left
    if not src_ok and not dst_ok:
        if _adopt_partial(conn, root, file_id, ((src_rel, dst_rel),), errors) is None:
            raise FileNotFoundError(f"file at neither {src_rel} nor {dst_rel}")
        return True
    if dst_ok and not _size_matches(conn, file_id, dst):
        raise MoveConflict(f"{dst_rel} holds another file")
    if src_ok and dst_ok:
        # A cross-device return cut off after its copy reached src, before dst was removed.
        if (
            not _maybe_same_file(src, dst)
            and not _tracked(conn, dst_rel, file_id, nocase=True)
            and sha256_file(src) == sha256_file(dst)
        ):
            os.unlink(long_path(dst))  # identical copy; the file is at src
            return True
        raise MoveConflict(f"{src_rel} is occupied by another file")
    _remove_stray_partials(conn, root, src_rel, dst_rel, src_ok, dst_ok, dst_ok, errors)
    _move_file(dst, src)  # never overwrites: an occupied src is a MoveConflict
    return True


def _rejoin_split_units(conn, root: Path, errors: list[str]) -> int:
    """Move the done files of a half-moved unit back to their source. Returns rows fixed.

    A unit is split when its rows in one batch are partly done and partly failed (a hard
    crash mid-unit, or a failed rollback). Its done files go back through the
    never-overwrite _move_file, their rows become failed, and rel_path returns to src.
    units.location never changed for such a unit, so it keeps its pre-batch value. For an
    undo batch the original rows go back to done. A file that cannot go back stays where
    it is and is reported.
    """
    groups = conn.execute(
        "select m.batch_id, m.unit_id, b.kind from moves m join batches b on b.id = m.batch_id"
        " group by m.batch_id, m.unit_id"
        " having sum(m.status='done') > 0 and sum(m.status='failed') > 0"
        " and sum(m.status='pending') = 0"
    ).fetchall()
    fixed = 0
    for batch_id, unit_id, kind in groups:
        rows = conn.execute(
            "select id, file_id, src_rel, dst_rel from moves"
            " where batch_id=? and unit_id is ? and status='done' order by rowid desc",
            (batch_id, unit_id),
        ).fetchall()
        for row_id, file_id, src_rel, dst_rel in rows:
            try:
                if not _return_to_source(conn, root, file_id, src_rel, dst_rel, errors):
                    continue
            except Exception as e:  # noqa: BLE001 - the file stays put and is reported
                errors.append(
                    f"split unit {unit_id}: {dst_rel} not returned to {src_rel}: {_describe(e)}"
                )
                continue
            with conn:
                conn.execute(
                    "update moves set status='failed', done_at=null where id=?", (row_id,)
                )
                _set_rel_path(conn, file_id, src_rel)
                if kind == "undo":
                    _revert_original(conn, file_id, src_rel, dst_rel)
            fixed += 1
    return fixed


def reconcile(conn: sqlite3.Connection, root: Path, errors: list[str] | None = None) -> int:
    """Settle the journal after a crash from what is on disk. Returns rows fixed.

    Pending rows become done or failed, then units left half-moved are rejoined at their
    source. A row that cannot be settled now (the file is at neither path, e.g. a drive is
    disconnected, or an I/O error) stays pending and is retried on the next run. Problems
    are appended to `errors` when a list is passed.
    """
    root = _norm(root)
    errors = [] if errors is None else errors
    rows = conn.execute(
        "select m.id, m.batch_id, m.unit_id, m.file_id, m.src_rel, m.dst_rel, b.kind"
        " from moves m join batches b on b.id = m.batch_id"
        " where m.status='pending' order by m.rowid"
    ).fetchall()
    fixed = 0
    for row in rows:
        row_id, src_rel, dst_rel = row[0], row[4], row[5]
        try:
            _abs(root, src_rel)
            _abs(root, dst_rel)
        except ValueError:
            # Such a row fails validation before any move, so nothing moved.
            with conn:
                conn.execute("update moves set status='failed' where id=?", (row_id,))
            fixed += 1
            continue
        try:
            if _settle_row(conn, root, row, errors):
                fixed += 1
        except OSError as e:
            errors.append(f"{src_rel} -> {dst_rel}: left pending: {_describe(e)}")
    return fixed + _rejoin_split_units(conn, root, errors)


def reconcile_or_refuse(conn: sqlite3.Connection, root: Path, errors: list[str]) -> int:
    """Reconcile before a new batch; raise PendingMoves while any row is still pending.

    A batch planned on top of an unsettled move would work from stale paths. Reconcile
    problems are appended to `errors`. Returns rows fixed.
    """
    seen = len(errors)
    fixed = reconcile(conn, root, errors)
    pending = conn.execute("select count(*) from moves where status='pending'").fetchone()[0]
    if pending:
        detail = "; ".join(errors[seen : seen + 5])
        raise PendingMoves(
            f"{pending} interrupted move(s) could not be settled (is a drive disconnected?)."
            f" Nothing was moved.{' ' + detail if detail else ''}"
        )
    return fixed


# --- undo --------------------------------------------------------------------------------


def _undo(conn: sqlite3.Connection, root: Path, batch_ids: list[str], label: str) -> BatchResult:
    plan: list[tuple[UnitMove, list[str] | None]] = []
    notes: list[str] = []
    for batch_id in batch_ids:
        rows = conn.execute(
            "select m.id, m.file_id, m.src_rel, m.dst_rel, coalesce(f.unit_id, m.unit_id),"
            " coalesce(f.purged, 0)"
            " from moves m left join files f on f.id = m.file_id"
            " where m.batch_id=? and m.status='done' order by m.rowid desc",
            (batch_id,),
        ).fetchall()
        by_unit: dict[str | None, list[tuple[str, FileMove, int]]] = {}
        for row_id, file_id, src_rel, dst_rel, unit_id, purged in rows:
            by_unit.setdefault(unit_id, []).append((row_id, FileMove(file_id, dst_rel, src_rel), purged))
        for unit_id, items in by_unit.items():
            purged_n = sum(p for _, _, p in items)
            if purged_n:
                if purged_n < len(items):  # a unit never comes back split
                    notes.append(f"unit {unit_id} partly purged, not restored")
                continue
            back_to = _location_of(items[0][1].dst_rel) or ""
            unit_move = UnitMove(unit_id, back_to, [fm for _, fm, _ in items])
            plan.append((unit_move, [row_id for row_id, _, _ in items]))
    result = _run_batch(
        conn, _norm(root), "undo", label, plan,
        after_batch=lambda bid: _reset_review_state(conn, bid),
    )
    # Folders the undone batch created are left empty; the base folders themselves stay.
    for top in (ARCHIVE_DIR, REVIEW_DIR, TRASH_DIR):
        if os.path.isdir(long_path(_norm(root) / top)):
            remove_empty_dirs(_norm(root) / top)
    result.errors.extend(notes)
    return result


def _reset_review_state(conn: sqlite3.Connection, undo_batch_id: str) -> None:
    """Make units that fully came back to review/source unseen and undecided again.

    Otherwise a restored grid unit keeps seen=1 plus an applied discard decision, and the
    next apply would trash it again without it ever being shown.
    """
    with conn:
        ids = [
            r[0]
            for r in conn.execute(
                "select m.unit_id from moves m join units u on u.id = m.unit_id"
                " where m.batch_id=? and u.location in ('review','source')"
                " group by m.unit_id having sum(m.status != 'done') = 0",
                (undo_batch_id,),
            )
        ]
        for uid in ids:
            conn.execute("delete from decisions where unit_id=?", (uid,))
            conn.execute("update units set seen=0 where id=?", (uid,))


def undo_batch(conn: sqlite3.Connection, root: Path, batch_id: str) -> BatchResult:
    """Reverse the done rows of a rapikan/apply batch as a new `undo` batch.

    Each unit goes back all-or-nothing; a unit whose original path is occupied, or whose
    file has moved on since, is skipped and reported in `errors`. With no done rows left,
    no batch row is written and `batch_id` is empty.
    """
    row = conn.execute("select kind from batches where id=?", (batch_id,)).fetchone()
    if row is None:
        raise UnknownBatch(f"unknown batch: {batch_id}")
    if row[0] not in _UNDOABLE_KINDS:
        raise NotUndoable(f"a {row[0]!r} batch cannot be undone")
    return _undo(conn, root, [batch_id], f"undo {batch_id}")


def undo_all(conn: sqlite3.Connection, root: Path) -> BatchResult:
    """Undo every rapikan/apply batch that still has done rows, newest first, as one batch."""
    batch_ids = [
        r[0]
        for r in conn.execute(
            "select b.id from batches b where b.kind in ('rapikan','apply')"
            " and exists (select 1 from moves m where m.batch_id = b.id and m.status='done'"
            "   and not exists (select 1 from files f where f.id = m.file_id and f.purged = 1))"
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
