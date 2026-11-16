"""Review API: category counts, groups, grids, decisions, apply preview and apply job."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from ... import decisions
from ...classify import GRID_CATEGORIES, GROUP_CATEGORIES
from ...db import open_db
from .. import queries
from ..deps import get_conn, get_root, start_job
from ..jobs import batch_result_dict
from ..messages import UserError

router = APIRouter(prefix="/api/review")

MAX_SEEN_BATCH = 1000


class DecisionBody(BaseModel):
    unit_id: str
    decision: str | None = None


class SeenBody(BaseModel):
    unit_ids: list[str]


class AcceptBody(BaseModel):
    group_ids: list[str]


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
            f"{p.keep + p.discard} dilihat: {p.discard} buang, {p.keep} selamatkan"
            f" · {p.unseen} belum dilihat (tetap di review)"
        )
    else:
        text = f"{p.keep} simpan, {p.discard} buang"
    return {"keep": p.keep, "discard": p.discard, "unseen": p.unseen, "text": text}


def _apply_job(root: Path, category: str):
    def run(report, stop):
        conn = open_db(root)  # sqlite connections are thread-bound
        try:
            return batch_result_dict(decisions.apply_category(conn, root, category))
        finally:
            conn.close()

    return run


@router.post("/apply/{category}")
def apply(
    category: str,
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    root: Path = Depends(get_root),
):
    _any_category(category)
    p = decisions.preview_apply(conn, category)
    if p.keep + p.discard == 0:
        raise UserError("Belum ada keputusan untuk diterapkan", 400)
    return start_job(request, "apply", f"Terapkan {category}", _apply_job(root, category))
