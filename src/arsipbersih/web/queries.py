"""Read models for the review and history pages: plain SQL, JSON-able dicts only."""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime
from typing import Literal

from arsipbersih.ai.heads import canonical_head_name, group_thresholds, threshold_for
from arsipbersih.classify import AI_GROUP_NAMES, CATEGORY_PRIORITY, GRID_CATEGORIES, GROUP_CATEGORIES
from arsipbersih.config import load_settings
from arsipbersih.decisions import preview_apply
from arsipbersih.grouping import load_unit_views
from arsipbersih.pathrules import is_chat, segments_of
from arsipbersih.web.labels import CATEGORY_HELP, CATEGORY_LABELS
from arsipbersih.web.reasons import suggestion_reason

GROUP_PAGE = 20
GRID_PAGE = 120
_CHUNK = 500

_PRIO = {c: i for i, c in enumerate(CATEGORY_PRIORITY)}

# A group is open while a member sits in _review, or an archived non-keeper has no applied decision.
_OPEN_GROUP = (
    "exists (select 1 from group_members m join units u on u.id = m.unit_id"
    " where m.group_id = g.id and (u.location = 'review' or (u.location = 'archive'"
    "  and m.unit_id is not g.suggested_keep_unit_id"
    "  and not exists (select 1 from decisions d where d.unit_id = u.id and d.applied = 1))))"
)


# A group is fully decided when every member that needs a decision has a decisions row.
# Members needing none: an archived suggested keeper, and members already in the trash.
_GROUP_ALL_DECIDED = (
    "not exists (select 1 from group_members m join units mu on mu.id = m.unit_id"
    " where m.group_id = g.id and mu.location != 'trash'"
    " and not (mu.location = 'archive' and m.unit_id is g.suggested_keep_unit_id)"
    " and not exists (select 1 from decisions d where d.unit_id = m.unit_id))"
)


def _thumb(fid: str) -> str:
    return f"/media/thumb/{fid}"


def _view(fid: str) -> str:
    return f"/media/view/{fid}"


def _video(fid: str) -> str:
    return f"/media/video/{fid}"


def _chunks(ids: list[str]):
    for i in range(0, len(ids), _CHUNK):
        yield ids[i : i + _CHUNK]


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


# --- counts ------------------------------------------------------------------------------


def category_counts(conn: sqlite3.Connection) -> list[dict]:
    grid = {
        r[0]: r[1]
        for r in conn.execute(
            "select primary_category, count(*) from units where location='review'"
            " and primary_category is not null group by primary_category"
        )
    }
    grid_done = {
        r[0]: r[1]
        for r in conn.execute(
            "select u.primary_category, count(*) from units u where u.location='review'"
            " and u.primary_category is not null and (u.seen = 1 or exists"
            " (select 1 from decisions d where d.unit_id = u.id)) group by u.primary_category"
        )
    }
    group_done = {
        r[0]: r[1]
        for r in conn.execute(
            f"select g.kind, count(*) from groups g where {_OPEN_GROUP}"
            f" and {_GROUP_ALL_DECIDED} group by g.kind"
        )
    }
    out = []
    for cat in CATEGORY_PRIORITY:
        if cat in GROUP_CATEGORIES:
            mode = "group"
            count = conn.execute(
                f"select count(*) from groups g where g.kind=? and {_OPEN_GROUP}", (cat,)
            ).fetchone()[0]
            done = group_done.get(cat, 0)
        else:
            mode = "grid"
            count = grid.get(cat, 0)
            done = grid_done.get(cat, 0)
        if count == 0:
            continue
        p = preview_apply(conn, cat)
        out.append(
            {
                "category": cat,
                "label": CATEGORY_LABELS.get(cat, cat),
                "help": CATEGORY_HELP.get(cat, ""),
                "mode": mode,
                "count": count,
                "done": done,
                "pending_apply": p.keep + p.discard,
            }
        )
    return out


# --- member briefs -----------------------------------------------------------------------

_BRIEF_SQL = (
    "select u.id as unit_id, u.primary_file_id as file_id, u.location, u.taken_at,"
    " u.seen, u.primary_category, d.decision, d.applied"
    " from units u left join decisions d on d.unit_id = u.id"
)


def _brief(row: sqlite3.Row, keeper_id: str | None = None) -> dict:
    fid = row["file_id"]
    return {
        "unit_id": row["unit_id"],
        "file_id": fid,
        "thumb": _thumb(fid) if fid else None,
        "is_keeper": row["unit_id"] == keeper_id,
        "decision": row["decision"],
        "applied": bool(row["applied"]),
        "location": row["location"],
    }


def _group_rows(conn: sqlite3.Connection, group_ids: list[str]) -> list[dict]:
    if not group_ids:
        return []
    groups = {}
    members: dict[str, list[sqlite3.Row]] = {gid: [] for gid in group_ids}
    for part in _chunks(group_ids):
        marks = ",".join("?" * len(part))
        for g in conn.execute(
            f"select id, kind, suggested_keep_unit_id from groups where id in ({marks})", part
        ):
            groups[g["id"]] = g
        for r in conn.execute(
            f"select m.group_id, b.*, f.rel_path, f.kind as fkind from group_members m"
            f" join ({_BRIEF_SQL}) b on b.unit_id = m.unit_id"
            f" left join files f on f.id = b.file_id"
            f" where m.group_id in ({marks}) order by b.taken_at, b.unit_id",
            part,
        ):
            members[r["group_id"]].append(r)
    all_ids = [r["unit_id"] for rs in members.values() for r in rs]
    views = {v.unit_id: v for v in load_unit_views(conn, all_ids)}
    chat_flagged: set[str] = set()
    for part in _chunks(all_ids):
        marks = ",".join("?" * len(part))
        chat_flagged.update(
            r[0]
            for r in conn.execute(
                f"select unit_id from flags where category='kiriman-chat' and unit_id in ({marks})",
                part,
            )
        )

    def origin(uid: str) -> str | None:
        v = views.get(uid)
        if uid in chat_flagged or (v and is_chat(segments_of(v.orig_rel_path), v.filename)):
            return "versi kiriman WA"
        return None

    out = []
    for gid in group_ids:
        g = groups.get(gid)
        if g is None:
            continue
        keeper = g["suggested_keep_unit_id"]
        rows = sorted(members[gid], key=lambda r: r["unit_id"] != keeper)  # stable: keeper first
        times = [t for t in (_parse(r["taken_at"]) for r in rows) if t]
        span = int((max(times) - min(times)).total_seconds()) if times else 0
        folder = None
        if rows and rows[0]["rel_path"]:
            folder = rows[0]["rel_path"].rsplit("/", 1)[0]
        n = len(rows)
        noun = "video" if rows and all(r["fkind"] == "video" for r in rows) else "foto"
        if g["kind"] == "mirip-beruntun":
            summary = f"{n} {noun} dalam {span} detik"
        elif g["kind"] == "duplikat-persis":
            summary = f"{n} file identik"
        else:
            summary = f"{n} versi {noun} yang sama"
        stamps = [r["taken_at"] for r in rows if r["taken_at"]]
        # Same rule as _GROUP_ALL_DECIDED: an archived keeper and trashed members need no decision.
        needing = [
            r for r in rows
            if r["location"] != "trash"
            and not (r["location"] == "archive" and r["unit_id"] == keeper)
        ]
        decided = sum(1 for r in needing if r["decision"] is not None)
        status = "none" if decided == 0 else ("all" if decided == len(needing) else "partial")
        if not needing:
            status = "all"
        gviews = [views[r["unit_id"]] for r in rows if r["unit_id"] in views]
        out.append(
            {
                "id": gid,
                "kind": g["kind"],
                "keeper_id": keeper,
                "members": [dict(_brief(r, keeper), origin=origin(r["unit_id"])) for r in rows],
                "status": status,
                "reason": suggestion_reason(g["kind"], gviews, keeper),
                "folder": folder,
                "taken_from": min(stamps, default=None),
                "taken_to": max(stamps, default=None),
                "span_s": span,
                "summary": summary,
            }
        )
    return out


def list_groups(conn: sqlite3.Connection, kind: str, page: int) -> dict:
    """Open groups of one kind, oldest first. `page` is 1-based."""
    first = (
        "(select min(u.taken_at) from group_members m join units u on u.id=m.unit_id"
        " where m.group_id=g.id)"
    )
    ids = [
        r[0]
        for r in conn.execute(
            f"select g.id from groups g where g.kind=? and {_OPEN_GROUP}"
            f" order by {first} is null, {first}, g.id",
            (kind,),
        )
    ]
    total = len(ids)
    pages = max(1, math.ceil(total / GROUP_PAGE))
    page = min(max(page, 1), pages)
    chunk = ids[(page - 1) * GROUP_PAGE : page * GROUP_PAGE]
    return {"groups": _group_rows(conn, chunk), "page": page, "pages": pages, "total": total}


def group_detail(conn: sqlite3.Connection, group_id: str) -> dict | None:
    rows = _group_rows(conn, [group_id])
    if not rows:
        return None
    row = rows[0]
    head = load_settings(conn).ai_head
    row["members"] = [
        unit_detail(conn, m["unit_id"], head) | {"is_keeper": m["is_keeper"], "origin": m["origin"]}
        for m in row["members"]
    ]
    return row


# --- grid --------------------------------------------------------------------------------


def _grid_order_sql(sort: str) -> str:
    if sort == "date":
        return "(b.taken_at is null), b.taken_at, b.unit_id"
    return "(fl.score is null), fl.score, (b.taken_at is null), b.taken_at, b.unit_id"


_GRID_FROM = (
    f"from ({_BRIEF_SQL}) b"
    " left join flags fl on fl.unit_id = b.unit_id and fl.category = b.primary_category"
    " left join files pf on pf.id = b.file_id"
)


def _ai_doubt_order(conn: sqlite3.Connection, ids: list[str], head: str) -> list[str]:
    """Closest to its group's threshold first; no label (or an owner correction) last.

    A stable sort over ``ids`` (already in the default order), so ties keep that order.
    """
    probs: dict[str, tuple[str, float]] = {}
    corrected: set[str] = set()
    for part in _chunks(ids):
        marks = ",".join("?" * len(part))
        for r in conn.execute(
            f"select unit_id, grp, prob from ai_labels where head = ? and unit_id in ({marks})",
            (canonical_head_name(head), *part),
        ):
            probs[r["unit_id"]] = (r["grp"], r["prob"])
        corrected.update(
            r[0]
            for r in conn.execute(
                f"select unit_id from ai_feedback where source = 'koreksi' and unit_id in ({marks})",
                part,
            )
        )
    thr = group_thresholds()

    def key(uid: str) -> tuple[int, float]:
        if uid in corrected or uid not in probs or probs[uid][1] is None:
            return (1, 0.0)
        grp, prob = probs[uid]
        return (0, abs(prob - threshold_for(grp, thr)))

    return sorted(ids, key=key)


def grid_order(
    conn: sqlite3.Connection,
    category: str,
    sort: Literal["score", "date", "ai_doubt"] = "score",
    view: Literal["all", "unseen", "keep"] = "all",
    head: str | None = None,
) -> list[str]:
    if category not in GRID_CATEGORIES:
        return []
    if sort == "ai_doubt":
        ids = grid_order(conn, category, "score", view)
        return _ai_doubt_order(conn, ids, head or load_settings(conn).ai_head)
    cond = ""
    if view == "unseen":
        cond = " and b.seen = 0 and b.decision is null"
    elif view == "keep":
        cond = " and b.decision = 'keep'"
    rows = conn.execute(
        f"select b.unit_id {_GRID_FROM}"
        f" where b.location='review' and b.primary_category=?{cond} order by {_grid_order_sql(sort)}",
        (category,),
    ).fetchall()
    return [r[0] for r in rows]


def _grid_item(r: sqlite3.Row, badges: dict[str, list[str]]) -> dict:
    item = _brief(r)
    item.update(
        {
            "score": r["score"],
            "reason": r["reason"],
            "seen": bool(r["seen"]),
            "taken_at": r["taken_at"],
            "badges": badges.get(r["unit_id"], []),
            "name": (r["primary_rel_path"] or "").rsplit("/", 1)[-1],
        }
    )
    return item


def grid_items_by_ids(conn: sqlite3.Connection, category: str, unit_ids: list[str]) -> list[dict]:
    if category not in GRID_CATEGORIES:
        return []
    rows: dict[str, sqlite3.Row] = {}
    for part in _chunks(unit_ids):
        marks = ",".join("?" * len(part))
        for r in conn.execute(
            f"select b.*, fl.score, fl.reason, pf.rel_path as primary_rel_path {_GRID_FROM}"
            f" where b.location='review' and b.primary_category=? and b.unit_id in ({marks})",
            (category, *part),
        ):
            rows[r["unit_id"]] = r
    badges = _badges(conn, list(rows))
    return [_grid_item(rows[u], badges) for u in unit_ids if u in rows]


def grid_items(
    conn: sqlite3.Connection,
    category: str,
    sort: Literal["score", "date"] = "score",
    offset: int = 0,
    limit: int = GRID_PAGE,
) -> dict:
    if category not in GRID_CATEGORIES:
        return {"items": [], "total": 0}
    total = conn.execute(
        "select count(*) from units where location='review' and primary_category=?", (category,)
    ).fetchone()[0]
    order = _grid_order_sql(sort)
    rows = conn.execute(
        f"select b.*, fl.score, fl.reason, pf.rel_path as primary_rel_path {_GRID_FROM}"
        f" where b.location='review' and b.primary_category=? order by {order} limit ? offset ?",
        (category, limit, offset),
    ).fetchall()
    badges = _badges(conn, [r["unit_id"] for r in rows])
    items = [_grid_item(r, badges) for r in rows]
    return {"items": items, "total": total}


def _badges(conn: sqlite3.Connection, unit_ids: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for part in _chunks(unit_ids):
        marks = ",".join("?" * len(part))
        for r in conn.execute(
            f"select f.unit_id, f.category from flags f join units u on u.id = f.unit_id"
            f" where f.unit_id in ({marks}) and f.category is not u.primary_category",
            part,
        ):
            out.setdefault(r["unit_id"], []).append(r["category"])
    for lst in out.values():
        lst.sort(key=lambda c: _PRIO.get(c, len(_PRIO)))
    return out


# --- unit card ---------------------------------------------------------------------------


def ai_guess(conn: sqlite3.Connection, unit_id: str, head: str | None = None) -> dict | None:
    """The AI group shown for a unit: the owner's newest correction wins over the head's label."""
    fb = conn.execute(
        "select grp from ai_feedback where unit_id = ? and source = 'koreksi'"
        " order by rowid desc limit 1",
        (unit_id,),
    ).fetchone()
    if fb is not None:
        grp, sub, prob, source = fb["grp"], None, None, "koreksi"
    else:
        lab = conn.execute(
            "select grp, sub, prob from ai_labels where unit_id = ? and head = ?",
            (unit_id, canonical_head_name(head or load_settings(conn).ai_head)),
        ).fetchone()
        if lab is None:
            return None
        grp, sub, prob, source = lab["grp"], lab["sub"], lab["prob"], "ai"
    return {"grp": grp, "sub": sub, "prob": prob, "source": source, "label": AI_GROUP_NAMES.get(grp, grp)}


VIDEO_GROUPS = ("grafis", "layar", "lainnya")  # the only AI groups that apply to videos


def _clip_ref(conn: sqlite3.Connection, unit_id: str, offset_s) -> dict | None:
    row = conn.execute(
        "select f.rel_path from units u join files f on f.id = u.primary_file_id where u.id = ?",
        (unit_id,),
    ).fetchone()
    if row is None:
        return None
    return {"unit_id": unit_id, "name": row["rel_path"].rsplit("/", 1)[-1], "offset_s": offset_s}


def clip_relations(conn: sqlite3.Connection, unit_id: str) -> dict:
    """{parent: the video this one was cut from | None, children: the cuts made from it}."""
    p = conn.execute(
        "select parent_unit_id, offset_s from video_clips where clip_unit_id = ?", (unit_id,)
    ).fetchone()
    parent = _clip_ref(conn, p["parent_unit_id"], p["offset_s"]) if p else None
    children = [
        c
        for r in conn.execute(
            "select clip_unit_id, offset_s from video_clips where parent_unit_id = ? order by offset_s",
            (unit_id,),
        )
        if (c := _clip_ref(conn, r["clip_unit_id"], r["offset_s"]))
    ]
    return {"parent": parent, "children": children}


def unit_detail(conn: sqlite3.Connection, unit_id: str, head: str | None = None) -> dict | None:
    u = conn.execute(
        "select u.*, d.decision, d.applied from units u"
        " left join decisions d on d.unit_id = u.id where u.id = ?",
        (unit_id,),
    ).fetchone()
    if u is None:
        return None
    primary_id = u["primary_file_id"]
    files = conn.execute(
        "select id, rel_path, size, kind, ext, purged from files where unit_id = ?"
        " order by (id = ?) desc, rel_path",
        (unit_id, primary_id),
    ).fetchall()
    live = [f for f in files if not f["purged"]]
    prow = next((f for f in files if f["id"] == primary_id), None)
    mi = None
    if primary_id:
        mi = conn.execute(
            "select f.rel_path, f.source_label, mi.* from files f"
            " left join media_info mi on mi.file_id = f.id where f.id = ?",
            (primary_id,),
        ).fetchone()

    primary = None
    if mi is not None:
        primary = {
            "file_id": primary_id,
            "name": mi["rel_path"].rsplit("/", 1)[-1],
            "width": mi["width"],
            "height": mi["height"],
            "size_total": sum(f["size"] or 0 for f in live),
            "source_label": mi["source_label"],
            "has_gps": bool(mi["has_gps"]),
            "sharpness": mi["sharpness"],
            "duration_s": mi["duration_s"],
            "video_codec": mi["video_codec"],
            "make": mi["make"],
            "model": mi["model"],
        }

    flags = [
        {"category": r["category"], "score": r["score"], "reason": r["reason"]}
        for r in conn.execute(
            "select category, score, reason from flags where unit_id = ?", (unit_id,)
        )
    ]
    flags.sort(key=lambda f: (f["category"] != u["primary_category"], _PRIO.get(f["category"], 99)))

    is_video = prow is not None and prow["kind"] == "video"
    video_id = None
    if is_video:
        video_id = primary_id
    elif u["pair_kind"] == "live_photo":
        mov = next(
            (f for f in live if f["id"] != primary_id and (f["ext"] or "").lower() == "mov"), None
        )
        video_id = mov["id"] if mov else None
    media = {
        "thumb": _thumb(primary_id) if primary_id else None,
        "view": _view(primary_id) if primary_id else None,
        "video": _video(video_id) if video_id else None,
        "kind": "video" if is_video else "photo",
    }
    ai = ai_guess(conn, unit_id, head)
    if is_video and ai is not None and ai["grp"] not in VIDEO_GROUPS:
        ai = None  # document groups do not apply to videos
    return {
        "unit_id": unit_id,
        "pair_kind": u["pair_kind"],
        "primary_category": u["primary_category"],
        "location": u["location"],
        "decision": u["decision"],
        "applied": bool(u["applied"]),
        "seen": bool(u["seen"]),
        "taken_at": u["taken_at"],
        "date_source": u["date_source"],
        "event_name": u["event_name"],
        "files": [
            {
                "file_id": f["id"],
                "name": f["rel_path"].rsplit("/", 1)[-1],
                "rel_path": f["rel_path"],
                "size": f["size"],
                "kind": f["kind"],
                "ext": f["ext"],
            }
            for f in files
        ],
        "primary": primary,
        "flags": flags,
        "media": media,
        "ai": ai,
        "clips": clip_relations(conn, unit_id),
    }


# --- history -----------------------------------------------------------------------------


def _batch_title(label: str, labels: dict[str, str]) -> str:
    if label.startswith("apply "):
        slug = label[len("apply "):]
        return f"Apply · {CATEGORY_LABELS.get(slug, slug)}"
    if label.startswith("rapikan"):
        return "Rapikan ke Arsip"
    if label == "undo all":
        return "Undo semua"
    if label.startswith("undo "):
        target = labels.get(label[len("undo "):])
        return f"Undo · {_batch_title(target, labels)}" if target else "Undo"
    if label == "purge trash":
        return "Kosongkan sampah"
    return label


def list_batches(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "select b.id, b.kind, b.label, b.created_at,"
        " coalesce(sum(m.status='done'), 0) as done,"
        " coalesce(sum(m.status='undone'), 0) as undone,"
        " coalesce(sum(m.status='failed'), 0) as failed,"
        " coalesce(sum(m.status='purged'), 0) as purged,"
        " coalesce(sum(m.status='done' and not exists (select 1 from files f"
        "   where f.id = m.file_id and f.purged = 1)), 0) as live_done"
        " from batches b left join moves m on m.batch_id = b.id"
        " group by b.id order by b.rowid desc"
    ).fetchall()
    labels = {r["id"]: r["label"] for r in rows}
    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "label": r["label"],
            "title": _batch_title(r["label"], labels),
            "created_at": r["created_at"],
            "done": r["done"],
            "undone": r["undone"],
            "failed": r["failed"],
            "purged": r["purged"],
            "undoable": r["kind"] in ("rapikan", "apply") and r["live_done"] > 0,
        }
        for r in rows
    ]
