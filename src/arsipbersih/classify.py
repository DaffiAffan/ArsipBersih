"""Category flags and primary category per unit (spec 8.1)."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from .config import Settings
from .db import new_id
from .grouping import UnitView, load_unit_views
from .pathrules import (
    is_chat,
    is_download,
    is_screenshot_name,
    is_trash,
    segments_of,
)

CATEGORY_PRIORITY: tuple[str, ...] = (
    "rusak", "duplikat-persis", "duplikat-versi", "mirip-beruntun", "screenshot",
    "kiriman-chat", "download-meme", "pendamping-gopro", "sampah-hp",
    "video-pendek-gelap", "kepencet", "burem", "gelap-terang",
)
GROUP_CATEGORIES = frozenset({"duplikat-persis", "duplikat-versi", "mirip-beruntun"})
GRID_CATEGORIES = frozenset(CATEGORY_PRIORITY) - GROUP_CATEGORIES

SCREEN_RATIOS = (16 / 9, 2.0, 18.5 / 9, 19 / 9, 19.5 / 9, 20 / 9, 21 / 9)
SCREEN_RATIO_TOLERANCE = 0.02
MEME_LONG_SIDE = 1000
MEME_EXTS = frozenset({"gif", "webp"})

Flag = tuple[str, float | None, str]  # category, score, reason

_GROUP_REASONS = {
    "duplikat-persis": "duplikat persis",
    "duplikat-versi": "versi lain dari foto yang sama",
    "mirip-beruntun": "foto beruntun mirip",
}


def _num(x: float) -> str:
    return f"{x:.0f}"


def _is_screen_aspect(w: int | None, h: int | None) -> bool:
    if not w or not h:
        return False
    ratio = max(w, h) / min(w, h)
    return any(abs(ratio - r) <= SCREEN_RATIO_TOLERANCE for r in SCREEN_RATIOS)


BROKEN_REASON = "file rusak / tidak bisa dibuka"
EMPTY_REASON = "file kosong (0 byte)"
ERROR_IN_REASON_MAX = 80


def _broken_units(conn: sqlite3.Connection) -> dict[str, str]:
    """{unit_id: reason} for units whose PRIMARY file failed to decode, or that hold an
    empty photo/raw/video file. A bad companion file never flags the whole unit, and
    decode_ok NULL (unknown, e.g. a RAW without an embedded preview) is not broken."""
    out: dict[str, str] = {}
    rows = conn.execute(
        "select u.id, f.error from units u join files f on f.id = u.primary_file_id "
        "join media_info m on m.file_id = f.id "
        "where f.missing = 0 and f.purged = 0 and m.decode_ok = 0"
    ).fetchall()
    for unit_id, error in rows:
        out[unit_id] = f"{BROKEN_REASON}: {error[:ERROR_IN_REASON_MAX]}" if error else BROKEN_REASON
    rows = conn.execute(
        "select f.unit_id, f.rel_path from files f "
        "where f.unit_id is not null and f.missing = 0 and f.purged = 0 "
        "and f.size = 0 and f.kind in ('photo','raw','video')"
    ).fetchall()
    for unit_id, rel_path in rows:
        name = rel_path.rsplit("/", 1)[-1]
        out.setdefault(unit_id, f"{EMPTY_REASON}: {name[:ERROR_IN_REASON_MAX]}")
    return out


def _extras(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        "select u.id as unit_id, f.ext, m.luma_mean, m.luma_std, m.clipping, m.frame_lumas "
        "from units u join files f on f.id = u.primary_file_id "
        "left join media_info m on m.file_id = f.id "
        "where f.missing = 0 and f.purged = 0"
    ).fetchall()
    out = {}
    for r in rows:
        lumas = json.loads(r["frame_lumas"]) if r["frame_lumas"] else None
        out[r["unit_id"]] = dict(ext=r["ext"] or "", luma=r["luma_mean"], std=r["luma_std"],
                                 clip=r["clipping"], frame_lumas=lumas)
    return out


def _group_flags(conn: sqlite3.Connection) -> dict[str, list[Flag]]:
    rows = conn.execute(
        "select gm.unit_id, g.kind from group_members gm join groups g on g.id = gm.group_id "
        "where g.suggested_keep_unit_id is not gm.unit_id"
    ).fetchall()
    out: dict[str, list[Flag]] = defaultdict(list)
    for r in rows:
        if r["kind"] in GROUP_CATEGORIES:
            out[r["unit_id"]].append((r["kind"], None, _GROUP_REASONS[r["kind"]]))
    return out


def _classify_unit(u: UnitView, ex: dict, s: Settings, broken: str | None) -> list[Flag]:
    flags: list[Flag] = []
    segs = segments_of(u.orig_rel_path)
    is_photo = u.kind == "photo"
    no_cam = not (u.make or u.model)
    if broken:
        flags.append(("rusak", None, broken))
    if is_screenshot_name(segs, u.filename):
        flags.append(("screenshot", None, "nama file screenshot"))
    elif no_cam and ex["ext"] == "png" and _is_screen_aspect(u.width, u.height):
        flags.append(("screenshot", None, "PNG tanpa data kamera, rasio layar HP"))
    if is_chat(segs, u.filename):
        flags.append(("kiriman-chat", None, "kiriman WhatsApp/Telegram"))
    if is_download(segs):
        flags.append(("download-meme", None, "berasal dari folder unduhan"))
    elif is_photo and no_cam and (
        ex["ext"] in MEME_EXTS
        or (u.width and u.height and max(u.width, u.height) < MEME_LONG_SIDE)
    ):
        flags.append(("download-meme", None, "tanpa data kamera, format/ukuran khas unduhan"))
    if u.kind == "gopro_aux":
        flags.append(("pendamping-gopro", None, "file pendamping GoPro"))
    if is_trash(segs, u.filename):
        flags.append(("sampah-hp", None, "sisa sampah HP"))
    if u.kind == "video":
        lumas = ex["frame_lumas"]
        if u.duration_s is not None and u.duration_s < s.short_video_s:
            flags.append(("video-pendek-gelap", u.duration_s,
                          f"terlalu pendek: {u.duration_s:.1f} detik".replace(".", ",")))
        elif lumas and all(v < s.dark_video_luma_max for v in lumas):
            flags.append(("video-pendek-gelap", max(lumas),
                          f"video terlalu gelap: luma {_num(max(lumas))}"))
    sharp, luma, std, clip = u.sharpness, ex["luma"], ex["std"], ex["clip"]
    if sharp is not None and sharp < s.kepencet_sharpness_max and (
        (luma is not None and luma < s.kepencet_luma_max)
        or (std is not None and std < s.kepencet_std_max)
    ):
        flags.append(("kepencet", sharp, f"skor tajam {_num(sharp)}, gambar nyaris polos"))
    threshold = s.blur_threshold if s.blur_threshold is not None else s.blur_fallback
    if is_photo and sharp is not None and sharp < threshold:
        flags.append(("burem", sharp, f"skor tajam {_num(sharp)}"))
    if luma is not None and luma < s.dark_luma_max:
        flags.append(("gelap-terang", luma, f"terlalu gelap: luma {_num(luma)}"))
    elif luma is not None and luma > s.bright_luma_min:
        flags.append(("gelap-terang", luma, f"terlalu terang: luma {_num(luma)}"))
    elif clip is not None and clip > s.clipping_max:
        flags.append(("gelap-terang", clip, f"piksel terpotong {clip * 100:.0f}%"))
    return flags


def run_classify(conn: sqlite3.Connection, settings: Settings) -> dict[str, int]:
    """Rewrite flags and primary_category for source/review/archive units."""
    views = [u for u in load_unit_views(conn) if u.location in ("source", "review", "archive")]
    extras = _extras(conn)
    broken = _broken_units(conn)
    group_flags = _group_flags(conn)
    counts: dict[str, int] = {}
    with conn:
        # A unit whose primary file is gone keeps no stale category from an earlier scan.
        conn.execute(
            "delete from flags where unit_id in (select u.id from units u"
            " join files f on f.id = u.primary_file_id"
            " where u.location != 'trash' and (f.missing = 1 or f.purged = 1))"
        )
        conn.execute(
            "update units set primary_category = null where location != 'trash' and"
            " primary_file_id in (select id from files where missing = 1 or purged = 1)"
        )
        for u in views:
            flags = list(group_flags.get(u.unit_id, []))
            if u.location != "archive":
                flags += _classify_unit(u, extras[u.unit_id], settings, broken.get(u.unit_id))
            by_cat = {f[0]: f for f in flags}
            conn.execute("delete from flags where unit_id = ?", (u.unit_id,))
            for cat, score, reason in by_cat.values():
                conn.execute(
                    "insert into flags(id, unit_id, category, score, reason) values(?,?,?,?,?)",
                    (new_id(), u.unit_id, cat, score, reason),
                )
            primary = next((c for c in CATEGORY_PRIORITY if c in by_cat), None)
            conn.execute("update units set primary_category = ? where id = ?", (primary, u.unit_id))
            if primary:
                counts[primary] = counts.get(primary, 0) + 1
    return counts
