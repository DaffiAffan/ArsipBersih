"""Shared request helpers for routes."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse

from ..db import open_db


def get_root(request: Request) -> Path:
    return request.app.state.root


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = open_db(request.app.state.root)
    try:
        yield conn
    finally:
        conn.close()


def render(request: Request, template: str, **ctx) -> HTMLResponse:
    ctx.setdefault("active", "")
    ctx["root"] = request.app.state.root
    return request.app.state.templates.TemplateResponse(request, template, ctx)
