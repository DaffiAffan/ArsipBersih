"""Beranda: root switch, tool status, scan job, summary."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from ...cli import WINGET_HINTS
from ...db import open_db
from ...layout import SOURCE_DIR
from ...lock import RootLocked, acquire_root_lock
from ...pipeline import SCAN_DONE_KEY, run_scan
from ...tools import check_tools
from ..deps import get_conn, get_root, render, start_job
from ..jobs import RECONCILE_LABEL, JobBusy, reconcile_job
from ..messages import UserError
from ..queries import category_counts

router = APIRouter()

ROOT_LOCKED_TEXT = "Koleksi ini sedang dipakai proses ArsipBersih lain."

TOOL_HINTS = {
    "exiftool": WINGET_HINTS["ExifTool"],
    "ffmpeg": WINGET_HINTS["ffmpeg"],
    "ffprobe": WINGET_HINTS["ffprobe"],
}


class RootBody(BaseModel):
    path: str


@router.get("/")
def home_page(request: Request):
    return render(request, "home.html", active="beranda", title="Beranda")


@router.get("/api/home")
def home_data(request: Request, conn=Depends(get_conn)):
    root = get_root(request)
    t = check_tools()
    row = conn.execute("select value from settings where key = ?", (SCAN_DONE_KEY,)).fetchone()
    last = json.loads(row[0]) if row else None
    by_location = {r[0]: r[1] for r in conn.execute("select location, count(*) from units group by location")}
    has_files = conn.execute("select 1 from files limit 1").fetchone() is not None
    st = request.app.state.jobs.status()
    warnings = list(st.result.get("warnings", [])) if st.kind == "scan" and st.result else []
    return {
        "root": str(root),
        "has_source": (root / SOURCE_DIR).is_dir(),
        "tools": {"exiftool": t.exiftool, "ffmpeg": t.ffmpeg, "ffprobe": t.ffprobe},
        "hints": TOOL_HINTS,
        "last_scan_at": last,
        "scan_incomplete": last is None and has_files,
        "by_location": by_location,
        "categories": category_counts(conn),
        "warnings": warnings,
    }


def _busy_error(label: str) -> UserError:
    return UserError(f"Masih ada proses yang berjalan: {label}. Tunggu sampai selesai.", status=409)


@router.post("/api/root")
def switch_root(request: Request, body: RootBody):
    jobs = request.app.state.jobs
    if jobs.busy():
        raise _busy_error(jobs.status().label or "")
    raw = body.path.strip()
    new = Path(raw).expanduser() if raw else None
    if new is None or not new.is_absolute():
        raise UserError("Masukkan path folder lengkap, misalnya D:\\Foto.", status=400)
    new = new.resolve()
    if not (new.is_dir() and (new / SOURCE_DIR).is_dir()):
        raise UserError("Folder ini tidak berisi Sumber/", status=400)
    state = request.app.state
    try:
        # Under the job lock: no job can start between the busy check and the switch.
        with jobs.exclusive():
            old_lock = state.root_lock
            same = old_lock is not None and old_lock.held and Path(state.root).resolve() == new
            new_lock = None
            if not same:
                try:
                    new_lock = acquire_root_lock(new)
                except RootLocked as exc:
                    raise UserError(ROOT_LOCKED_TEXT, status=400) from exc
                except OSError as exc:
                    raise UserError(f"Folder ini tidak bisa dipakai: {exc}", status=400) from exc
            try:
                open_db(new).close()
            except (OSError, sqlite3.Error) as exc:
                if new_lock is not None:
                    new_lock.release()
                raise UserError(f"Folder ini tidak bisa dipakai: {exc}", status=400) from exc
            state.root = new
            if new_lock is not None:
                state.root_lock = new_lock
                if old_lock is not None:
                    old_lock.release()
            if state.start_jobs:
                # spec §10: settle any interrupted move journal of the new collection
                jobs.start("reconcile", RECONCILE_LABEL, reconcile_job(new))
    except JobBusy as exc:
        raise _busy_error(exc.label) from exc
    return {"root": str(new)}


@router.post("/api/scan")
def start_scan(request: Request):
    root = get_root(request)

    def fn(report, stop):
        s = run_scan(root, progress=report, stop=stop)
        return {
            "stopped": s.stopped,
            "categories": s.categories,
            "events": len(s.events),
            "warnings": list(s.warnings),
        }

    return start_job(request, "scan", "Pindai", fn)
