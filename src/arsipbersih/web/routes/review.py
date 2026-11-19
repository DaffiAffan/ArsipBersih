"""Review API: category counts, groups, grids, decisions, apply preview and apply job."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field, StrictInt

from ... import decisions
from ...ai.heads import GROUPS
from ...classify import GRID_CATEGORIES, GROUP_CATEGORIES
from ...config import load_settings
from ...db import open_db
from .. import queries
from ..deps import get_conn, get_root, render, start_job
from ..jobs import JobBusy, batch_result_dict
from ..labels import CATEGORY_HELP, CATEGORY_LABELS
from ..messages import APPLY_CONTENT_CHANGED_TEXT, APPLY_MISMATCH_TEXT, UserError, explain

router = APIRouter(prefix="/api/review")

MAX_SEEN_BATCH = 1000
MAX_GRID_IDS = 500
MAX_BULK_DECISIONS = 500


class DecisionBody(BaseModel):
    unit_id: str
    decision: str | None = None


class BulkDecisionBody(BaseModel):
    unit_ids: list[str]
    decision: str | None = None


class SeenBody(BaseModel):
    unit_ids: list[str]


class GridIdsBody(BaseModel):
    unit_ids: list[str]


class ApplyBody(BaseModel):
    keep: StrictInt = Field(ge=0)
    discard: StrictInt = Field(ge=0)
    # Digest of the exact unit lists the owner saw; optional so count-only callers still work.
    fingerprint: str | None = None


class ApplyMismatch(Exception):
    """The counts changed between the owner's confirmation and the apply job."""


class AcceptBody(BaseModel):
    group_ids: list[str]


class AiGroupBody(BaseModel):
    unit_id: str
    grp: str


def _group_kind(kind: str) -> str:
    if kind not in GROUP_CATEGORIES:
        raise UserError("Kategori tidak dikenal", 400)
    return kind


def _grid_category(category: str) -> str:
    if category not in GRID_CATEGORIES:
        raise UserError("Kategori tidak dikenal", 400)
    return category


def _any_category(category: str) -> str:
    if category not in GRID_CATEGORIES and category not in GROUP_CATEGORIES:
        raise UserError("Kategori tidak dikenal", 400)
    return category


@router.get("/categories")
def categories(conn: sqlite3.Connection = Depends(get_conn)):
    return queries.category_counts(conn)


@router.get("/groups/{kind}")
def groups(kind: str, page: int = 1, conn: sqlite3.Connection = Depends(get_conn)):
    _group_kind(kind)
    if page < 1:
        raise UserError("Halaman tidak valid", 400)
    return queries.list_groups(conn, kind, page)


@router.get("/group/{group_id}")
def group(group_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    detail = queries.group_detail(conn, group_id)
    if detail is None:
        raise UserError("Grup tidak ditemukan", 404)
    return detail


@router.get("/grid/{category}")
def grid(
    category: str,
    sort: str = "score",
    offset: int = 0,
    conn: sqlite3.Connection = Depends(get_conn),
):
    _grid_category(category)
    if sort not in ("score", "date"):
        raise UserError("Urutan tidak dikenal", 400)
    if offset < 0:
        raise UserError("Offset tidak valid", 400)
    return queries.grid_items(conn, category, sort, offset)


@router.get("/grid/{category}/order")
def grid_order(
    category: str,
    sort: str = "score",
    view: str = "all",
    conn: sqlite3.Connection = Depends(get_conn),
):
    _grid_category(category)
    if sort not in ("score", "date", "ai_doubt"):
        raise UserError("Urutan tidak dikenal", 400)
    if view not in ("all", "unseen", "keep"):
        raise UserError("Tampilan tidak dikenal", 400)
    ids = queries.grid_order(conn, category, sort, view)
    return {"unit_ids": ids, "total": len(ids)}


@router.post("/grid/{category}/items")
def grid_items_by_ids(
    category: str, body: GridIdsBody, conn: sqlite3.Connection = Depends(get_conn)
):
    _grid_category(category)
    if len(body.unit_ids) > MAX_GRID_IDS:
        raise UserError(f"Maksimal {MAX_GRID_IDS} item per permintaan", 400)
    return {"items": queries.grid_items_by_ids(conn, category, body.unit_ids)}


@router.get("/unit/{unit_id}")
def unit(unit_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    detail = queries.unit_detail(conn, unit_id)
    if detail is None:
        raise UserError("Unit tidak ditemukan", 404)
    return detail


@router.post("/decision")
def decision(body: DecisionBody, conn: sqlite3.Connection = Depends(get_conn)):
    if body.decision not in ("keep", "discard", None):
        raise UserError("Keputusan tidak valid", 400)
    if conn.execute("select 1 from units where id=?", (body.unit_id,)).fetchone() is None:
        raise UserError("Unit tidak ditemukan", 404)
    if body.decision is None:
        decisions.clear(conn, body.unit_id)
    else:
        decisions.record(conn, body.unit_id, body.decision)
    return {"ok": True}


@router.post("/ai-group")
def ai_group(body: AiGroupBody, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Owner correction of the AI group: stored as feedback, the unit is reclassified in the
    database only (it shows up under its new review category). No file is moved."""
    if body.grp not in GROUPS:
        raise UserError("Kelompok tidak dikenal", 400)
    if conn.execute("select 1 from units where id=?", (body.unit_id,)).fetchone() is None:
        raise UserError("Unit tidak ditemukan", 404)
    if body.grp not in queries.VIDEO_GROUPS and conn.execute(
        "select 1 from units u join files f on f.id = u.primary_file_id"
        " where u.id = ? and f.kind = 'video'",
        (body.unit_id,),
    ).fetchone():
        raise UserError("Video hanya bisa dikelompokkan sebagai Grafis, Layar, atau Video biasa", 400)
    try:
        # No scan or apply may rewrite categories or move files while this one changes.
        with request.app.state.jobs.exclusive():
            settings = load_settings(conn)
            decisions.correct_group(conn, settings, body.unit_id, body.grp)
    except JobBusy as exc:
        raise UserError(explain(exc), 409) from exc
    row = conn.execute("select primary_category from units where id=?", (body.unit_id,)).fetchone()
    return {
        "primary_category": row[0],
        "label": CATEGORY_LABELS.get(row[0]) if row[0] else None,
        "ai": queries.ai_guess(conn, body.unit_id, settings.ai_head),
    }


@router.post("/decisions")
def bulk_decisions(body: BulkDecisionBody, conn: sqlite3.Connection = Depends(get_conn)):
    if body.decision != "discard":
        raise UserError("Keputusan tidak valid", 400)
    if len(body.unit_ids) > MAX_BULK_DECISIONS:
        raise UserError(f"Maksimal {MAX_BULK_DECISIONS} item per permintaan", 400)
    return {"recorded": decisions.record_bulk_discard(conn, body.unit_ids)}


@router.post("/seen")
def seen(body: SeenBody, conn: sqlite3.Connection = Depends(get_conn)):
    if len(body.unit_ids) > MAX_SEEN_BATCH:
        raise UserError(f"Maksimal {MAX_SEEN_BATCH} item per permintaan", 400)
    decisions.mark_seen(conn, body.unit_ids)
    return {"ok": True}


@router.post("/accept")
def accept(body: AcceptBody, conn: sqlite3.Connection = Depends(get_conn)):
    return {"recorded": decisions.accept_suggestions(conn, body.group_ids)}


@router.get("/apply/{category}")
def apply_preview(category: str, conn: sqlite3.Connection = Depends(get_conn)):
    _any_category(category)
    p = decisions.preview_apply(conn, category)
    if category in GRID_CATEGORIES:
        text = (
            f"{p.discard} dibuang, {p.keep} disimpan"
            f" · {p.unseen} belum diputuskan (tetap di review)"
        )
    else:
        text = f"{p.keep} disimpan, {p.discard} dibuang"
    return {
        "keep": p.keep, "discard": p.discard, "unseen": p.unseen,
        "text": text, "fingerprint": p.fingerprint,
    }


def _apply_job(root: Path, category: str, keep: int, discard: int, fingerprint: str | None = None):
    def run(report, stop):
        conn = open_db(root)  # sqlite connections are thread-bound
        try:
            p = decisions.preview_apply(conn, category)
            if (p.keep, p.discard) != (keep, discard):
                raise ApplyMismatch(category)
            if fingerprint is not None and p.fingerprint != fingerprint:
                raise decisions.ApplyContentChanged(category)
            return batch_result_dict(decisions.apply_category(conn, root, category, fingerprint))
        finally:
            conn.close()

    return run


@router.post("/apply/{category}")
def apply(
    category: str,
    body: ApplyBody,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    root: Path = Depends(get_root),
):
    _any_category(category)
    p = decisions.preview_apply(conn, category)
    if (p.keep, p.discard) != (body.keep, body.discard):
        raise UserError(APPLY_MISMATCH_TEXT, 409)
    if body.fingerprint is not None and p.fingerprint != body.fingerprint:
        raise UserError(APPLY_CONTENT_CHANGED_TEXT, 409)
    if p.keep + p.discard == 0:
        raise UserError("Belum ada keputusan untuk di-apply", 400)
    job = _apply_job(root, category, body.keep, body.discard, body.fingerprint)
    return start_job(request, "apply", f"Apply {CATEGORY_LABELS.get(category, category)}", job)

# --- pages ---------------------------------------------------------------------------------

pages = APIRouter()

def _sidebar(conn: sqlite3.Connection) -> list[dict]:
    return queries.category_counts(conn)


@pages.get("/review")
def review_index(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    cats = _sidebar(conn)
    if cats:
        return RedirectResponse(f"/review/{cats[0]['category']}", status_code=307)
    return render(
        request, "review.html", active="review", categories=[], category=None, mode=None, label=None, help=None
    )


@pages.get("/review/{category}", response_class=HTMLResponse)
def review_category(category: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    if category in GROUP_CATEGORIES:
        mode = "group"
    elif category in GRID_CATEGORIES:
        mode = "grid"
    else:
        resp = render(
            request, "message.html", active="review", title="Kategori tidak dikenal",
            message="Kategori ini tidak ada. Pilih kategori dari daftar di Review.",
        )
        resp.status_code = 404
        return resp
    return render(
        request,
        "review.html",
        active="review",
        categories=_sidebar(conn),
        category=category,
        mode=mode,
        label=CATEGORY_LABELS.get(category, category),
        help=CATEGORY_HELP.get(category),
    )
