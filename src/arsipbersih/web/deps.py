"""Shared request helpers for routes."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..db import open_db
from .jobs import JobBusy, JobPrecheckFailed
from .labels import CATEGORY_LABELS
from .messages import UserError, explain


ROOT_CHANGED_TEXT = "Koleksi baru saja diganti. Muat ulang halaman, lalu coba lagi."


def get_root(request: Request) -> Path:
    root = request.app.state.root
    # Remember which collection this request works on, so start_job can refuse a job
    # whose closure still points at the collection that was just switched away from.
    if getattr(request.state, "root", None) is None:
        request.state.root = root
    return root


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = open_db(request.app.state.root, check_same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


def render(request: Request, template: str, **ctx) -> HTMLResponse:
    ctx.setdefault("active", "")
    ctx["root"] = request.app.state.root
    # Embedded in a <script type="application/json"> block: "<" must never appear raw.
    ctx["category_labels_json"] = json.dumps(CATEGORY_LABELS).replace("<", "\\u003c")
    return request.app.state.templates.TemplateResponse(request, template, ctx)


def start_job(request: Request, kind: str, label: str, fn) -> JSONResponse:
    seen = getattr(getattr(request, "state", None), "root", None)

    def same_root() -> bool:  # runs under the job lock, atomic with POST /api/root
        return seen is None or request.app.state.root == seen

    try:
        state = request.app.state.jobs.start(kind, label, fn, precheck=same_root)
    except JobBusy as exc:
        raise UserError(explain(exc), status=409) from exc
    except JobPrecheckFailed as exc:
        raise UserError(ROOT_CHANGED_TEXT, status=409) from exc
    return JSONResponse(state.as_dict(), status_code=202)
