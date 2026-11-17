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
    "rusak", "duplikat-persis", "duplikat-versi", "mirip-beruntun",
    "dokumen-identitas", "dokumen-keuangan", "dokumen-kertas", "screenshot",
    "download-meme", "kiriman-chat", "pendamping-gopro", "sampah-hp",
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


def download_reason(u: UnitView, segs: list[str], ext: str) -> str | None:
    """Why the unit looks downloaded (download-meme rule), or None. Shared with the AI facts."""
    if is_download(segs):
        return "berasal dari folder unduhan"
    if u.kind == "photo" and not (u.make or u.model) and (
        ext in MEME_EXTS or (u.width and u.height and max(u.width, u.height) < MEME_LONG_SIDE)
    ):
        return "tanpa data kamera, format/ukuran khas unduhan"
    return None


BROKEN_REASON = "file rusak / tidak bisa dibuka"
EMPTY_REASON = "file kosong (0 byte)"
ERROR_IN_REASON_MAX = 80


def _rows(conn: sqlite3.Connection, sql: str, col: str, unit_ids: list[str] | None) -> list:
    """Run ``sql`` (which already has a WHERE clause), limited to ``unit_ids`` when given."""
    if unit_ids is None:
        return conn.execute(sql).fetchall()
    rows: list = []
    for i in range(0, len(unit_ids), 500):
        part = unit_ids[i : i + 500]
        marks = ",".join("?" * len(part))
        rows += conn.execute(f"{sql} and {col} in ({marks})", part).fetchall()
    return rows


def _broken_units(conn: sqlite3.Connection, unit_ids: list[str] | None = None) -> dict[str, str]:
    """{unit_id: reason} for units whose PRIMARY file failed to decode, or that hold an
    empty photo/raw/video file. A bad companion file never flags the whole unit, and
    decode_ok NULL (unknown, e.g. a RAW without an embedded preview) is not broken."""
    out: dict[str, str] = {}
    rows = _rows(
        conn,
        "select u.id, f.error from units u join files f on f.id = u.primary_file_id "
        "join media_info m on m.file_id = f.id "
        "where f.missing = 0 and f.purged = 0 and m.decode_ok = 0",
        "u.id", unit_ids,
    )
    for unit_id, error in rows:
        out[unit_id] = f"{BROKEN_REASON}: {error[:ERROR_IN_REASON_MAX]}" if error else BROKEN_REASON
    rows = _rows(
        conn,
        "select f.unit_id, f.rel_path from files f "
        "where f.unit_id is not null and f.missing = 0 and f.purged = 0 "
        "and f.size = 0 and f.kind in ('photo','raw','video')",
        "f.unit_id", unit_ids,
    )
    for unit_id, rel_path in rows:
        name = rel_path.rsplit("/", 1)[-1]
        out.setdefault(unit_id, f"{EMPTY_REASON}: {name[:ERROR_IN_REASON_MAX]}")
    return out


def _extras(conn: sqlite3.Connection, unit_ids: list[str] | None = None) -> dict[str, dict]:
    rows = _rows(
        conn,
        "select u.id as unit_id, f.ext, m.luma_mean, m.luma_std, m.clipping, m.frame_lumas "
        "from units u join files f on f.id = u.primary_file_id "
        "left join media_info m on m.file_id = f.id "
        "where f.missing = 0 and f.purged = 0",
        "u.id", unit_ids,
    )
    out = {}
    for r in rows:
        lumas = json.loads(r["frame_lumas"]) if r["frame_lumas"] else None
        out[r["unit_id"]] = dict(ext=r["ext"] or "", luma=r["luma_mean"], std=r["luma_std"],
                                 clip=r["clipping"], frame_lumas=lumas)
    return out


def _group_flags(conn: sqlite3.Connection, unit_ids: list[str] | None = None) -> dict[str, list[Flag]]:
    rows = _rows(
        conn,
        "select gm.unit_id, g.kind from group_members gm join groups g on g.id = gm.group_id "
        "where g.suggested_keep_unit_id is not gm.unit_id",
        "gm.unit_id", unit_ids,
    )
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
    download = download_reason(u, segs, ex["ext"])
    if download:
        flags.append(("download-meme", None, download))
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


AI_GROUP_NAMES = {
    "identitas": "Identitas",
    "keuangan": "Keuangan",
    "kertas": "Kertas & catatan",
    "grafis": "Grafis",
    "layar": "Layar",
    "lainnya": "Foto biasa",
}


def ai_flags_for(conn: sqlite3.Connection, head_name: str) -> dict[str, tuple[str, float, str]]:
    """{unit_id: (category, prob, reason)} from stored AI labels of ``head_name``.

    An owner correction (newest) replaces the head's label entirely; a confirmation changes
    nothing. Groups without a category (lainnya) yield no flag.
    """
    from .ai.heads import GROUP_TO_CATEGORY, canonical_head_name
    from .ai.store import effective_feedback, latest_labels

    out: dict[str, tuple[str, float, str]] = {}
    for unit_id, (grp, sub, prob) in latest_labels(conn, canonical_head_name(head_name)).items():
        cat = GROUP_TO_CATEGORY.get(grp)
        if cat:
            tail = f" · {sub}" if sub else ""
            out[unit_id] = (cat, prob, f"AI: {AI_GROUP_NAMES.get(grp, grp)}{tail} ({prob:.2f})".replace(".", ","))
    for unit_id, (grp, source) in effective_feedback(conn).items():
        if source != "koreksi":
            continue
        cat = GROUP_TO_CATEGORY.get(grp)
        if cat:
            out[unit_id] = (cat, 1.0, f"Dikoreksi pemilik: {AI_GROUP_NAMES.get(grp, grp)}")
        else:
            out.pop(unit_id, None)
    return out


class _Facts:
    """Everything the per-unit rules need, loaded once (for all units or just some)."""

    def __init__(self, conn: sqlite3.Connection, settings: Settings, unit_ids: list[str] | None = None):
        self.settings = settings
        self.extras = _extras(conn, unit_ids)
        self.broken = _broken_units(conn, unit_ids)
        self.group_flags = _group_flags(conn, unit_ids)
        ai = ai_flags_for(conn, settings.ai_head)
        if unit_ids is not None:
            wanted = set(unit_ids)
            ai = {k: v for k, v in ai.items() if k in wanted}
        self.ai_flags = ai


def _unit_flags(u: UnitView, facts: _Facts) -> list[Flag]:
    flags = list(facts.group_flags.get(u.unit_id, []))
    if u.location != "archive":
        flags += _classify_unit(u, facts.extras[u.unit_id], facts.settings, facts.broken.get(u.unit_id))
    if u.location != "archive" and u.kind != "video" and u.unit_id in facts.ai_flags:
        cat, prob, reason = facts.ai_flags[u.unit_id]
        if not any(f[0] == cat for f in flags):
            flags.append((cat, prob, reason))
    return flags


def _write_unit(conn: sqlite3.Connection, unit_id: str, flags: list[Flag]) -> str | None:
    """Replace the unit's flags and primary_category; return the new primary category."""
    by_cat = {f[0]: f for f in flags}
    conn.execute("delete from flags where unit_id = ?", (unit_id,))
    for cat, score, reason in by_cat.values():
        conn.execute(
            "insert into flags(id, unit_id, category, score, reason) values(?,?,?,?,?)",
            (new_id(), unit_id, cat, score, reason),
        )
    primary = next((c for c in CATEGORY_PRIORITY if c in by_cat), None)
    conn.execute("update units set primary_category = ? where id = ?", (primary, unit_id))
    return primary


_CLASSIFIED_LOCATIONS = ("source", "review", "archive")


def reset_review_state(conn: sqlite3.Connection, unit_id: str) -> None:
    """Forget what a unit carried from its old review category (call inside a transaction).

    Apply treats a seen grid unit without a decision, and any unapplied Delete, as a discard;
    neither may follow the unit into a category where the owner never checked it (spec 7.7).
    `seen` is reset and an unapplied Delete is removed. An unapplied Keep and applied
    decisions stay: this only ever makes Apply more conservative.
    """
    conn.execute("update units set seen=0 where id=?", (unit_id,))
    conn.execute(
        "delete from decisions where unit_id=? and applied=0 and decision='discard'", (unit_id,)
    )


def _review_categories(conn: sqlite3.Connection, unit_ids: list[str] | None = None) -> dict[str, str | None]:
    """{unit_id: primary_category} of review units (all of them, or just ``unit_ids``)."""
    rows = _rows(conn, "select id, primary_category from units where location='review'",
                 "id", unit_ids)
    return {r[0]: r[1] for r in rows}


def _reset_moved(conn: sqlite3.Connection, before: dict[str, str | None],
                 unit_ids: list[str] | None = None) -> None:
    """Reset the review state of every review unit whose primary category changed."""
    after = _review_categories(conn, unit_ids)
    for unit_id, old in before.items():
        if unit_id in after and after[unit_id] != old:
            reset_review_state(conn, unit_id)


def run_classify(conn: sqlite3.Connection, settings: Settings) -> dict[str, int]:
    """Rewrite flags and primary_category for source/review/archive units."""
    views = [u for u in load_unit_views(conn) if u.location in _CLASSIFIED_LOCATIONS]
    facts = _Facts(conn, settings)
    counts: dict[str, int] = {}
    with conn:
        before = _review_categories(conn)
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
            primary = _write_unit(conn, u.unit_id, _unit_flags(u, facts))
            if primary:
                counts[primary] = counts.get(primary, 0) + 1
        _reset_moved(conn, before)
    return counts


def reclassify_units(
    conn: sqlite3.Connection, settings: Settings, unit_ids: list[str], *, commit: bool = True
) -> dict[str, str | None]:
    """Recompute flags and primary_category of just these units, exactly as run_classify would.

    Every input is per unit (group flags are read from the stored groups, not recomputed),
    so other units are never touched. Units run_classify skips (trash, primary file missing)
    are left as they are. A review unit that changes category loses its review state
    (see reset_review_state). Returns {unit_id: primary_category} for the units rewritten.
    With ``commit=False`` the writes join the caller's open transaction.
    """
    ids = list(dict.fromkeys(unit_ids))
    views = [u for u in load_unit_views(conn, ids) if u.location in _CLASSIFIED_LOCATIONS]
    facts = _Facts(conn, settings, ids)
    out: dict[str, str | None] = {}

    def write() -> None:
        before = _review_categories(conn, ids)
        for u in views:
            out[u.unit_id] = _write_unit(conn, u.unit_id, _unit_flags(u, facts))
        _reset_moved(conn, before, ids)

    if not commit:
        write()
        return out
    with conn:
        write()
    return out
