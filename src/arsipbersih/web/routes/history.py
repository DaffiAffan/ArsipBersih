"""History API: batches and undo, journal repair, missing files, and emptying _sampah."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, StrictInt

from ... import decisions, repair
from ...db import open_db
from ...mover import reconcile_or_refuse, undo_all, undo_batch
from .. import queries
from ..deps import get_conn, get_root, render, start_job
from ..jobs import batch_result_dict
from ..messages import UserError

router = APIRouter(prefix="/api")
pages = APIRouter()


@pages.get("/riwayat")
def riwayat_page(request: Request):
    return render(request, "history.html", active="riwayat", title="Riwayat")

PURGE_CONFIRM = "HAPUS"


class UndoBody(BaseModel):
    batch_id: str | None = None
    all: bool = False


class ReleaseBody(BaseModel):
    row_id: str
    deleted_on_purpose: bool = False


class ForgetBody(BaseModel):
    file_id: str


class PurgeBody(BaseModel):
    expected_files: StrictInt = Field(ge=0)
    expected_bytes: StrictInt = Field(ge=0)
    confirm: str = ""


@router.get("/history")
def history(conn: sqlite3.Connection = Depends(get_conn)):
    files, size = decisions.trash_totals(conn)
    return {
        "batches": queries.list_batches(conn),
        "pending": [asdict(p) for p in repair.list_pending(conn)],
        "missing": [asdict(m) for m in repair.list_missing(conn)],
        "trash": {"files": files, "bytes": size},
    }


def _undo_job(root: Path, batch_id: str | None):
    def run(report, stop):
        conn = open_db(root)  # sqlite connections are thread-bound
        try:
            journal_errors: list[str] = []
            reconcile_or_refuse(conn, root, journal_errors)
            result = undo_batch(conn, root, batch_id) if batch_id else undo_all(conn, root)
            out = batch_result_dict(result)
            out["errors"] = [f"journal: {e}" for e in journal_errors] + out["errors"]
            return out
        finally:
            conn.close()

    return run


@router.post("/undo")
def undo(body: UndoBody, request: Request, root: Path = Depends(get_root)):
    if bool(body.batch_id) == bool(body.all):
        raise UserError("Pilih tepat satu: satu batch atau semua", 400)
    label = "Kembalikan semua" if body.all else "Kembalikan batch"
    return start_job(request, "undo", label, _undo_job(root, None if body.all else body.batch_id))


def _release_job(root: Path, row_id: str, deleted_on_purpose: bool):
    def run(report, stop):
        conn = open_db(root)
        try:
            repair.release_pending(conn, root, row_id, deleted_on_purpose)
            return {"ok": True}
        finally:
            conn.close()

    return run


@router.post("/journal/release")
def release(body: ReleaseBody, request: Request, root: Path = Depends(get_root)):
    return start_job(
        request, "release", "Lepaskan perpindahan terputus",
        _release_job(root, body.row_id, body.deleted_on_purpose),
    )


def _forget_job(root: Path, file_id: str):
    def run(report, stop):
        conn = open_db(root)
        try:
            repair.forget_file(conn, root, file_id)
            return {"ok": True}
        finally:
            conn.close()

    return run


@router.post("/files/forget")
def forget(
    body: ForgetBody,
    request: Request,
    root: Path = Depends(get_root),
    conn: sqlite3.Connection = Depends(get_conn),
):
    if conn.execute("select 1 from files where id=?", (body.file_id,)).fetchone() is None:
        raise UserError("File tidak ditemukan", 404)
    return start_job(request, "forget", "Tandai sengaja dihapus", _forget_job(root, body.file_id))


def _purge_job(root: Path, expected_files: int, expected_bytes: int):
    def run(report, stop):
        conn = open_db(root)
        try:
            errors: list[str] = []
            deleted = decisions.purge_trash(conn, root, expected_files, expected_bytes, errors=errors)
            return {"deleted": deleted, "errors": errors}
        finally:
            conn.close()

    return run


@router.post("/trash/purge")
def purge(body: PurgeBody, request: Request, root: Path = Depends(get_root)):
    if body.confirm != PURGE_CONFIRM:
        raise UserError(f"Ketik {PURGE_CONFIRM} untuk mengonfirmasi", 400)
    # The client's displayed totals go straight to purge_trash; never recomputed here.
    return start_job(
        request, "purge", "Kosongkan sampah",
        _purge_job(root, body.expected_files, body.expected_bytes),
    )
