"""Rencana Rapikan: event preview with overrides, tidy summary and the confirmed tidy job."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from ...config import load_settings
from ...events import add_override, run_events
from ...pipeline import ScanIncomplete, rapikan, summarize_rapikan
from ...planner import compute_plans
from ..deps import get_conn, get_root, render, start_job
from ..jobs import batch_result_dict
from ..messages import UserError, explain

router = APIRouter()


class OverrideBody(BaseModel):
    kind: str
    name: str
    value: str | None = None


def _payload(conn: sqlite3.Connection, root: Path, events) -> dict:
    return {"summary": asdict(summarize_rapikan(conn, root)), "events": [asdict(e) for e in events]}


@router.get("/rencana")
def plan_page(request: Request):
    return render(request, "plan.html", active="rencana", title="Rencana Rapikan")


@router.get("/api/plan")
def get_plan(conn: sqlite3.Connection = Depends(get_conn), root: Path = Depends(get_root)):
    # GET stays read-only: events are resolved in memory; overrides and scan persist them.
    events = run_events(conn, load_settings(conn), persist=False)
    return _payload(conn, root, events)


@router.post("/api/events/override")
def post_override(
    body: OverrideBody,
    conn: sqlite3.Connection = Depends(get_conn),
    root: Path = Depends(get_root),
):
    if body.kind not in ("not_event", "rename", "merge"):
        raise UserError("Jenis perubahan tidak dikenal.", status=400)
    name = body.name.strip()
    value = (body.value or "").strip() or None
    if not name:
        raise UserError("Nama acara wajib diisi.", status=400)
    if body.kind in ("rename", "merge") and value is None:
        raise UserError("Nilai tujuan wajib diisi untuk ganti nama atau gabung.", status=400)
    add_override(conn, body.kind, name, value if body.kind != "not_event" else None)
    events = run_events(conn, load_settings(conn))
    compute_plans(conn)
    conn.commit()
    return _payload(conn, root, events)


@router.post("/api/tidy")
def post_tidy(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    root: Path = Depends(get_root),
):
    if not summarize_rapikan(conn, root).scan_done:
        raise UserError(
            explain(ScanIncomplete("The last scan did not finish. Run `scan` again before `tidy`.")),
            status=409,
        )

    def run(report, stop):
        return batch_result_dict(rapikan(root))

    return start_job(request, "tidy", "Rapikan", run)
