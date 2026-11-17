"""Plan archive/review destinations with collision-safe naming. No filesystem writes."""

from __future__ import annotations

import os
import posixpath
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .classify import CATEGORY_PRIORITY, GROUP_CATEGORIES
from .formats import GOPRO_AUX_EXTS, PHOTO_EXTS, RAW_EXTS, SIDECAR_EXTS, VIDEO_EXTS
from .layout import (
    ARCHIVE_DIR,
    DOC_SUBDIRS,
    DOCS_DIR,
    REVIEW_DIR,
    SOURCE_DIR,
    TRASH_DIR,
    UNDATED_DIR,
    long_path,
    sanitize_component,
    to_abs,
)


@dataclass
class FileMove:
    file_id: str
    src_rel: str
    dst_rel: str


@dataclass
class UnitMove:
    unit_id: str
    to_location: str
    files: list[FileMove]


def _join(*parts: str) -> str:
    return "/".join(p for p in parts if p)


def _subpath(sub: str | None) -> str:
    if not sub:
        return ""
    return "/".join(sanitize_component(p) for p in sub.split("/") if p.strip())


def archive_dir_for(
    event_name: str | None,
    event_subpath: str | None,
    event_month: str | None,
    taken_at: str | None,
    source_label: str,
) -> str:
    if event_name:
        name = sanitize_component(event_name)
        sub = _subpath(event_subpath)
        if event_month:
            return _join(ARCHIVE_DIR, event_month[:4], f"{event_month[:7]} {name}", sub)
        return _join(ARCHIVE_DIR, UNDATED_DIR, name, sub)
    if taken_at and len(taken_at) >= 7:
        return _join(ARCHIVE_DIR, taken_at[:4], taken_at[:7])
    return _join(ARCHIVE_DIR, UNDATED_DIR, sanitize_component(source_label or "_"))


def document_dir_for(category: str | None, taken_at: str | None) -> str | None:
    """Keep destination for document photos: Arsip/_Dokumen/<Sub>/<YYYY|Tanpa-Tanggal>."""
    sub = DOC_SUBDIRS.get(category or "")
    if sub is None:
        return None
    year = taken_at[:4] if taken_at and re.fullmatch(r"\d{4}", taken_at[:4]) else UNDATED_DIR
    return _join(ARCHIVE_DIR, DOCS_DIR, sub, year)


def _split_orig(orig_rel_path: str) -> tuple[str, list[str]]:
    """(source label, dirs below the label) from an original rel path."""
    parts = [p for p in orig_rel_path.replace("\\", "/").split("/") if p]
    dirs = parts[:-1]
    if not dirs:
        return SOURCE_DIR, []
    if dirs[0] == SOURCE_DIR:
        if len(dirs) == 1:
            return SOURCE_DIR, []
        return dirs[1], dirs[2:]
    if dirs[0] == ARCHIVE_DIR:
        return ARCHIVE_DIR, dirs[1:]
    return dirs[0], dirs[1:]


def review_dir_for(base: str, category: str, orig_rel_path: str) -> str:
    label, dirs = _split_orig(orig_rel_path)
    return _join(
        base,
        sanitize_component(category),
        sanitize_component(label),
        *[sanitize_component(d) for d in dirs],
    )


def _label_of(orig_rel_path: str | None, source_label: str | None) -> str:
    if source_label:
        return source_label
    return _split_orig(orig_rel_path or "")[0]


_UNIT_SQL = (
    "select u.id, u.taken_at, u.event_key, u.event_name, u.event_subpath, u.location, "
    "u.primary_file_id, u.primary_category, u.planned_archive_dir, u.planned_review_dir, "
    "pf.orig_rel_path as orig, pf.rel_path as prim_rel, pf.source_label as label "
    "from units u left join files pf on pf.id = u.primary_file_id"
)


_ARCHIVE_MONTH_RE = re.compile(r"^(\d{4}-\d{2}) ")


def _archived_month(rel_path: str | None) -> str | None:
    """YYYY-MM of an `Arsip/YYYY/YYYY-MM <Nama>[/...]` folder, or None for any other shape."""
    parts = (rel_path or "").replace("\\", "/").split("/")
    if len(parts) < 4 or parts[0] != ARCHIVE_DIR:
        return None
    m = _ARCHIVE_MONTH_RE.match(parts[2])
    return m.group(1) if m and parts[1] == m.group(1)[:4] else None


def _event_months(conn: sqlite3.Connection) -> dict[str, str]:
    """Folder month per event: the earliest month of its existing Arsip folder if it has
    archived units (so later, earlier-dated photos reuse it), else its earliest photo month."""
    rows = conn.execute(
        "select event_key, min(substr(taken_at,1,7)) m from units "
        "where event_key is not null and taken_at is not null and location != 'trash' "
        "group by event_key"
    ).fetchall()
    months = {r["event_key"]: r["m"] for r in rows if r["m"]}
    existing: dict[str, str] = {}
    for r in conn.execute(
        "select u.event_key, f.rel_path from units u join files f on f.id = u.primary_file_id "
        "where u.event_key is not null and u.location = 'archive'"
    ):
        m = _archived_month(r["rel_path"])
        if m and (r["event_key"] not in existing or m < existing[r["event_key"]]):
            existing[r["event_key"]] = m
    months.update(existing)
    return months


def _archive_dir(u: sqlite3.Row, months: dict[str, str]) -> str:
    doc = document_dir_for(u["primary_category"], u["taken_at"])
    if doc:
        return doc
    month = months.get(u["event_key"]) if u["event_key"] else None
    label = _label_of(u["orig"] or u["prim_rel"], u["label"])
    return archive_dir_for(u["event_name"], u["event_subpath"], month, u["taken_at"], label)


def _review_dir(u: sqlite3.Row, base: str, category: str) -> str:
    return review_dir_for(base, category, u["orig"] or u["prim_rel"] or "")


def compute_plans(conn: sqlite3.Connection) -> int:
    months = _event_months(conn)
    rows = conn.execute(_UNIT_SQL + " where u.location != 'trash'").fetchall()
    for u in rows:
        review = _review_dir(u, REVIEW_DIR, u["primary_category"]) if u["primary_category"] else None
        conn.execute(
            "update units set planned_archive_dir=?, planned_review_dir=? where id=?",
            (_archive_dir(u, months), review, u["id"]),
        )
    conn.commit()
    return len(rows)


_MEDIA_EXTS = PHOTO_EXTS | RAW_EXTS | VIDEO_EXTS | GOPRO_AUX_EXTS
MAX_RESERVE_ATTEMPTS = 10_000


def _split_name(fn: str) -> tuple[str, str]:
    """(stem, ext) where a sidecar of a media file keeps both extensions (IMG_1.JPG.xmp)."""
    stem, ext = posixpath.splitext(fn)
    if ext.lstrip(".").lower() in SIDECAR_EXTS:
        inner_stem, inner_ext = posixpath.splitext(stem)
        if inner_ext.lstrip(".").lower() in _MEDIA_EXTS:
            return inner_stem, inner_ext + ext
    return stem, ext


class NameReserver:
    def __init__(self, root: Path, conn: sqlite3.Connection | None = None):
        self.root = root
        self._conn = conn
        self._db_paths: set[str] | None = None
        self._taken: set[tuple[str, str]] = set()
        self._listing: dict[str, set[str]] = {}

    def _in_db(self, dst_dir: str, name: str) -> bool:
        if self._conn is None:
            return False
        if self._db_paths is None:
            self._db_paths = {
                r[0].lower() for r in self._conn.execute("select rel_path from files")
            }
        return f"{dst_dir}/{name}".lower() in self._db_paths

    def _on_disk(self, dst_dir: str, name: str) -> bool:
        d = dst_dir.lower()
        if d not in self._listing:
            try:
                names = os.listdir(long_path(to_abs(self.root, dst_dir)))
            except OSError:
                names = []
            self._listing[d] = {n.lower() for n in names}
        if name.lower() in self._listing[d]:
            return True
        return os.path.lexists(long_path(to_abs(self.root, f"{dst_dir}/{name}")))

    def _free(self, dst_dir: str, names: list[str]) -> bool:
        seen: set[str] = set()
        for n in names:
            k = n.lower()
            if (k in seen or (dst_dir.lower(), k) in self._taken or self._in_db(dst_dir, n)
                    or self._on_disk(dst_dir, n)):
                return False
            seen.add(k)
        return True

    def reserve_unit(self, dst_dir: str, filenames: list[str], source_label: str) -> list[str]:
        label = sanitize_component(source_label or "_")
        lowered = [f.lower() for f in filenames]
        if len(set(lowered)) != len(lowered):
            raise ValueError(f"duplicate file names within one unit: {filenames}")
        for n in range(1, MAX_RESERVE_ATTEMPTS + 1):
            if n == 1:
                suffix = ""
            elif n == 2:
                suffix = f"__{label}"
            else:
                suffix = f"__{label}__{n - 1}"
            cand = []
            for fn in filenames:
                stem, ext = _split_name(fn)
                cand.append(f"{stem}{suffix}{ext}")
            if self._free(dst_dir, cand):
                for c in cand:
                    self._taken.add((dst_dir.lower(), c.lower()))
                return cand
        raise RuntimeError(f"could not reserve names in {dst_dir} for {filenames}")


def _build_move(
    conn: sqlite3.Connection,
    reserver: NameReserver,
    u: sqlite3.Row,
    dst_dir: str,
    to_location: str,
) -> UnitMove | None:
    rows = conn.execute(
        "select id, rel_path from files where unit_id=?", (u["id"],)
    ).fetchall()
    files = sorted(rows, key=lambda r: (r["id"] != u["primary_file_id"], r["rel_path"]))
    if not files:
        return None
    names = [sanitize_component(posixpath.basename(f["rel_path"])) for f in files]
    label = _label_of(u["orig"] or u["prim_rel"], u["label"])
    reserved = reserver.reserve_unit(dst_dir, names, label)
    return UnitMove(
        u["id"],
        to_location,
        [FileMove(f["id"], f["rel_path"], f"{dst_dir}/{n}") for f, n in zip(files, reserved)],
    )


def _try_build(conn, reserver, u, dst_dir, loc, skipped) -> UnitMove | None:
    try:
        return _build_move(conn, reserver, u, dst_dir, loc)
    except (ValueError, RuntimeError) as err:
        if skipped is not None:
            skipped.append((u["id"], str(err)))
        return None


def _keepers(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "select distinct suggested_keep_unit_id from groups "
            "where suggested_keep_unit_id is not null"
        )
    }


def missing_files_by_unit(conn: sqlite3.Connection) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in conn.execute(
        "select unit_id, rel_path from files"
        " where unit_id is not null and missing = 1 and purged = 0"
        " order by rel_path"
    ):
        out.setdefault(r[0], []).append(r[1])
    return out


def _missing_reason(rel_paths: list[str]) -> str:
    return "file missing on disk: " + ", ".join(rel_paths)


def plan_rapikan(
    conn: sqlite3.Connection, root: Path, skipped: list[tuple[str, str]] | None = None
) -> list[UnitMove]:
    """Plan every source unit, plus review units that lost their category on a re-scan
    (no review category shows them any more, so they go back to their archive folder)."""
    months = _event_months(conn)
    keepers = _keepers(conn)
    incomplete = missing_files_by_unit(conn)
    rows = conn.execute(
        _UNIT_SQL + " where u.location = 'source'"
        " or (u.location = 'review' and u.primary_category is null)"
        " order by pf.orig_rel_path, u.id"
    ).fetchall()
    reserver = NameReserver(root, conn)
    moves: list[UnitMove] = []
    for u in rows:
        if u["id"] in incomplete:
            # Moving the rest would split the unit; nothing moves until the file is back.
            if skipped is not None:
                skipped.append((u["id"], _missing_reason(incomplete[u["id"]])))
            continue
        cat = u["primary_category"]
        if cat is None or (u["id"] in keepers and cat in GROUP_CATEGORIES):
            dst_dir, loc = (
                document_dir_for(cat, u["taken_at"])
                or u["planned_archive_dir"]
                or _archive_dir(u, months)
            ), "archive"
        else:
            dst_dir = u["planned_review_dir"] or _review_dir(u, REVIEW_DIR, cat)
            loc = "review"
        m = _try_build(conn, reserver, u, dst_dir, loc, skipped)
        if m:
            moves.append(m)
    return moves


def _discard_category(conn: sqlite3.Connection, u: sqlite3.Row) -> str:
    if u["primary_category"]:
        return u["primary_category"]
    kinds = {
        r[0]
        for r in conn.execute(
            "select g.kind from groups g left join group_members m on m.group_id=g.id "
            "where m.unit_id=? or g.suggested_keep_unit_id=?",
            (u["id"], u["id"]),
        )
    }
    for c in CATEGORY_PRIORITY:
        if c in kinds:
            return c
    return sorted(kinds)[0] if kinds else "lainnya"


def plan_apply(
    conn: sqlite3.Connection,
    root: Path,
    keep_ids: list[str],
    discard_ids: list[str],
    skipped: list[tuple[str, str]] | None = None,
) -> list[UnitMove]:
    months = _event_months(conn)
    reserver = NameReserver(root, conn)
    moves: list[UnitMove] = []

    def fetch(uid: str):
        return conn.execute(_UNIT_SQL + " where u.id=?", (uid,)).fetchone()

    def gone(uid: str) -> bool:
        rels = [
            r[0]
            for r in conn.execute(
                "select rel_path from files where unit_id=? and purged=0"
                " order by rel_path",
                (uid,),
            )
        ]
        flagged = {
            r[0]
            for r in conn.execute(
                "select rel_path from files where unit_id=? and purged=0 and missing=1", (uid,)
            )
        }
        bad = [r for r in rels if r in flagged or not os.path.exists(long_path(to_abs(root, r)))]
        if bad and skipped is not None:
            skipped.append((uid, _missing_reason(bad)))
        return bool(bad)

    for uid in keep_ids:
        u = fetch(uid)
        if u is None or u["location"] == "archive":
            continue
        if gone(uid):
            continue
        dst = document_dir_for(u["primary_category"], u["taken_at"]) or (
            u["planned_archive_dir"] or _archive_dir(u, months)
        )
        m = _try_build(conn, reserver, u, dst, "archive", skipped)
        if m:
            moves.append(m)
    for uid in discard_ids:
        u = fetch(uid)
        if u is None or u["location"] == "trash":
            continue
        if gone(uid):
            continue
        dst = _review_dir(u, TRASH_DIR, _discard_category(conn, u))
        m = _try_build(conn, reserver, u, dst, "trash", skipped)
        if m:
            moves.append(m)
    return moves
