"""Group companion files (Live Photo, RAW+JPG, sidecars, GoPro) into move units."""

from __future__ import annotations

import posixpath
import re
import sqlite3
from collections import defaultdict

from .config import Settings
from .db import new_id
from .formats import GOPRO_AUX_EXTS, PHOTO_EXTS, RAW_EXTS, SIDECAR_EXTS, VIDEO_EXTS, Kind
from .layout import ARCHIVE_DIR, SOURCE_DIR

_LIVE_PHOTO_EXTS = {"jpg", "jpeg", "heic", "heif"}
_RAW_PARTNER_EXTS = {"jpg", "jpeg"}
_MEDIA_EXTS = PHOTO_EXTS | RAW_EXTS | VIDEO_EXTS | GOPRO_AUX_EXTS
_GOPRO_RE = re.compile(r"^(?:G[XHP]\d{2}|GOPR)(\d{4})$", re.IGNORECASE)
_LOCATIONS = {SOURCE_DIR: "source", ARCHIVE_DIR: "archive"}


def gopro_clip_id(stem: str) -> str | None:
    m = _GOPRO_RE.match(stem)
    return m.group(1) if m else None


def _group_stem(rel_path: str, ext: str) -> str:
    stem = posixpath.splitext(posixpath.basename(rel_path))[0]
    if ext in SIDECAR_EXTS:
        inner_stem, inner_ext = posixpath.splitext(stem)
        if inner_ext.lstrip(".").lower() in _MEDIA_EXTS:
            stem = inner_stem
    return stem.casefold()


def _create_unit(conn, pair_kind: str, primary_id: str, location: str, member_ids) -> str:
    unit_id = new_id()
    conn.execute(
        "insert into units (id, primary_file_id, pair_kind, location) values (?, ?, ?, ?)",
        (unit_id, primary_id, pair_kind, location),
    )
    conn.executemany(
        "update files set unit_id=? where id=?", [(unit_id, fid) for fid in member_ids]
    )
    return unit_id


def build_units(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "select id, rel_path, kind, ext from files"
        " where unit_id is null and missing=0 and purged=0 order by rel_path"
    ).fetchall()
    groups: dict[tuple[str, str], list] = defaultdict(list)
    locations: dict[tuple[str, str], str] = {}
    for r in rows:
        location = _LOCATIONS.get(r["rel_path"].split("/", 1)[0])
        if location is None:
            continue
        key = (posixpath.dirname(r["rel_path"]), _group_stem(r["rel_path"], r["ext"]))
        groups[key].append(r)
        locations[key] = location

    created = 0
    for key, members in groups.items():
        location = locations[key]
        sidecar_ids = [m["id"] for m in members if m["kind"] == Kind.SIDECAR]
        media = [m for m in members if m["kind"] not in (Kind.SIDECAR, Kind.GOPRO_AUX)]

        for m in members:
            if m["kind"] == Kind.GOPRO_AUX:
                _create_unit(conn, "single", m["id"], location, [m["id"]])
                created += 1

        remaining = sorted(media, key=lambda m: m["rel_path"])
        pair_unit_ids: list[str] = []

        raws = [m for m in remaining if m["kind"] == Kind.RAW]
        jpgs = [m for m in remaining if m["kind"] == Kind.PHOTO and m["ext"] in _RAW_PARTNER_EXTS]
        raw_unit = None
        if len(raws) == 1 and jpgs:
            raw_unit = _create_unit(
                conn, "raw_jpg", jpgs[0]["id"], location, [jpgs[0]["id"], raws[0]["id"]]
            )
            created += 1
            remaining = [m for m in remaining if m["id"] not in (raws[0]["id"], jpgs[0]["id"])]

        movs = [m for m in remaining if m["kind"] == Kind.VIDEO and m["ext"] == "mov"]
        photos = sorted(
            (m for m in remaining if m["kind"] == Kind.PHOTO and m["ext"] in _LIVE_PHOTO_EXTS),
            key=lambda m: (m["ext"] not in ("heic", "heif"), m["rel_path"]),
        )
        live_unit = None
        if len(movs) == 1 and photos:
            live_unit = _create_unit(
                conn, "live_photo", photos[0]["id"], location, [photos[0]["id"], movs[0]["id"]]
            )
            created += 1
            remaining = [m for m in remaining if m["id"] not in (movs[0]["id"], photos[0]["id"])]

        first_photo_unit = None
        for m in remaining:
            uid = _create_unit(conn, "single", m["id"], location, [m["id"]])
            created += 1
            if first_photo_unit is None and m["kind"] == Kind.PHOTO:
                first_photo_unit = uid
            if first_photo_unit is None and not pair_unit_ids:
                pair_unit_ids.append(uid)
        target = raw_unit or live_unit or first_photo_unit or (pair_unit_ids[0] if pair_unit_ids else None)
        if target and sidecar_ids:
            conn.executemany(
                "update files set unit_id=? where id=?", [(target, sid) for sid in sidecar_ids]
            )
    conn.commit()
    return created


def confirm_live_photos(conn: sqlite3.Connection, settings: Settings) -> int:
    """Split a Live Photo whose MOV is known to be too long. Returns units split.

    An unknown duration (not probed yet, or ffprobe missing/failed) keeps the pair:
    build_units never re-pairs, so a split here is permanent.
    """
    rows = conn.execute(
        "select u.id as unit_id, u.location, u.taken_at, u.date_source,"
        " f.id as mov_id, mi.duration_s"
        " from units u join files f on f.unit_id=u.id and f.ext='mov' and f.kind='video'"
        " left join media_info mi on mi.file_id=f.id"
        " where u.pair_kind='live_photo'"
    ).fetchall()
    split = 0
    for r in rows:
        d = r["duration_s"]
        if d is None or d <= settings.live_photo_mov_max_s:
            continue
        new_unit = new_id()
        conn.execute(
            "insert into units (id, primary_file_id, pair_kind, taken_at, date_source, location)"
            " values (?, ?, 'single', ?, ?, ?)",
            (new_unit, r["mov_id"], r["taken_at"], r["date_source"], r["location"]),
        )
        conn.execute("update files set unit_id=? where id=?", (new_unit, r["mov_id"]))
        conn.execute("update units set pair_kind='single' where id=?", (r["unit_id"],))
        split += 1
    conn.commit()
    return split
