"""Plan archive/review destinations with collision-safe naming. No filesystem writes."""

from __future__ import annotations

import os
import posixpath
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .classify import CATEGORY_PRIORITY, GROUP_CATEGORIES
from .layout import (
    ARCHIVE_DIR,
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


def _event_months(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        "select event_key, min(substr(taken_at,1,7)) m from units "
        "where event_key is not null and taken_at is not null and location != 'trash' "
        "group by event_key"
    ).fetchall()
    return {r["event_key"]: r["m"] for r in rows if r["m"]}


def _archive_dir(u: sqlite3.Row, months: dict[str, str]) -> str:
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


class NameReserver:
    def __init__(self, root: Path):
        self.root = root
        self._taken: set[tuple[str, str]] = set()
        self._listing: dict[str, set[str]] = {}

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
            if k in seen or (dst_dir.lower(), k) in self._taken or self._on_disk(dst_dir, n):
                return False
            seen.add(k)
        return True

    def reserve_unit(self, dst_dir: str, filenames: list[str], source_label: str) -> list[str]:
        label = sanitize_component(source_label or "_")
        n = 1
        while True:
            if n == 1:
                suffix = ""
            elif n == 2:
                suffix = f"__{label}"
            else:
                suffix = f"__{label}__{n - 1}"
            cand = []
            for fn in filenames:
                stem, ext = posixpath.splitext(fn)
                cand.append(f"{stem}{suffix}{ext}")
            if self._free(dst_dir, cand):
                for c in cand:
                    self._taken.add((dst_dir.lower(), c.lower()))
                return cand
            n += 1


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


def _keepers(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "select distinct suggested_keep_unit_id from groups "
            "where suggested_keep_unit_id is not null"
        )
    }


def plan_rapikan(conn: sqlite3.Connection, root: Path) -> list[UnitMove]:
    months = _event_months(conn)
    keepers = _keepers(conn)
    rows = conn.execute(
        _UNIT_SQL + " where u.location = 'source' order by pf.orig_rel_path, u.id"
    ).fetchall()
    reserver = NameReserver(root)
    moves: list[UnitMove] = []
    for u in rows:
        cat = u["primary_category"]
        if cat is None or (u["id"] in keepers and cat in GROUP_CATEGORIES):
            dst_dir, loc = (u["planned_archive_dir"] or _archive_dir(u, months)), "archive"
        else:
            dst_dir = u["planned_review_dir"] or _review_dir(u, REVIEW_DIR, cat)
            loc = "review"
        m = _build_move(conn, reserver, u, dst_dir, loc)
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
    conn: sqlite3.Connection, root: Path, keep_ids: list[str], discard_ids: list[str]
) -> list[UnitMove]:
    months = _event_months(conn)
    reserver = NameReserver(root)
    moves: list[UnitMove] = []

    def fetch(uid: str):
        return conn.execute(_UNIT_SQL + " where u.id=?", (uid,)).fetchone()

    for uid in keep_ids:
        u = fetch(uid)
        if u is None or u["location"] == "archive":
            continue
        dst = u["planned_archive_dir"] or _archive_dir(u, months)
        m = _build_move(conn, reserver, u, dst, "archive")
        if m:
            moves.append(m)
    for uid in discard_ids:
        u = fetch(uid)
        if u is None or u["location"] == "trash":
            continue
        dst = _review_dir(u, TRASH_DIR, _discard_category(conn, u))
        m = _build_move(conn, reserver, u, dst, "trash")
        if m:
            moves.append(m)
    return moves
