"""Review page "Tanggal": per-event dates for late WhatsApp photos, manual dates for undated
units, and the job that writes date .xmp sidecars. Nothing here moves a media file; the
new dates are used by the next Rapikan."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, StrictInt

from ...config import load_settings
from ...dating import infer, store
from ...dating.xmp import MAX_REPORTED, InvalidTimezone, write_dates
from ...db import open_db
from ...mover import reconcile_or_refuse
from ..deps import get_conn, get_root, render, start_job
from ..jobs import JobBusy
from ..messages import UserError, explain

router = APIRouter()

MAX_SAMPLES = 6
UNDATED_PAGE = 200
MAX_MANUAL = 5000
WRITE_LABEL = "Tulis tanggal"
_CHUNK = 500

# Short Indonesian month names; June/July are short enough to write in full.
MONTHS = ["Jan", "Feb", "Mar", "Apr", "Mei", "Juni", "Juli", "Agu", "Sep", "Okt", "Nov", "Des"]


class RangeBody(BaseModel):
    range_start: str
    range_end: str


class ManualBody(BaseModel):
    unit_ids: list[str]
    year: StrictInt
    month: StrictInt
    day: StrictInt | None = None


def _month_range(days: list[str]) -> str:
    """'Okt 2024', 'Okt–Des 2024' or 'Des 2023–Jan 2024' for ISO dates/timestamps."""
    lo, hi = min(days)[:10], max(days)[:10]
    (y1, m1), (y2, m2) = (int(lo[:4]), int(lo[5:7])), (int(hi[:4]), int(hi[5:7]))
    if (y1, m1) == (y2, m2):
        return f"{MONTHS[m1 - 1]} {y1}"
    if y1 == y2:
        return f"{MONTHS[m1 - 1]}–{MONTHS[m2 - 1]} {y1}"
    return f"{MONTHS[m1 - 1]} {y1}–{MONTHS[m2 - 1]} {y2}"


def _rows(conn: sqlite3.Connection, sql: str, ids: list[str]) -> list[sqlite3.Row]:
    out: list[sqlite3.Row] = []
    for i in range(0, len(ids), _CHUNK):
        part = ids[i : i + _CHUNK]
        out += conn.execute(sql.format(marks=",".join("?" * len(part))), part).fetchall()
    return out


def _event_payload(conn: sqlite3.Connection, p: infer.EventProposal) -> dict:
    rows = {
        r["id"]: r
        for r in _rows(conn, "select id, primary_file_id, taken_at from units where id in ({marks})",
                       p.unit_ids)
    }
    # units.taken_at keeps the WhatsApp send date even when an event date is in force.
    sent = [rows[u]["taken_at"] for u in p.unit_ids if u in rows and rows[u]["taken_at"]]
    sent_text = _month_range(sent) if sent else None
    camera_text = _month_range([p.camera_start, p.camera_end])
    summary = (
        f"Foto kiriman WA di acara ini memakai tanggal kirim ({sent_text}), "
        f"sedangkan foto kameranya {camera_text}."
        if sent_text else None
    )
    return {
        "event_key": p.event_key,
        "event_name": p.event_name,
        "camera_start": p.camera_start,
        "camera_end": p.camera_end,
        "status": p.status,
        "count": len(p.unit_ids),
        "samples": [rows[u]["primary_file_id"] for u in p.unit_ids[:MAX_SAMPLES]
                    if u in rows and rows[u]["primary_file_id"]],
        "sent_text": sent_text,
        "camera_text": camera_text,
        "summary": summary,
    }


def _undated_ids(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "select u.id from units u join unit_dates d on d.unit_id = u.id"
            " left join files f on f.id = u.primary_file_id"
            " where d.taken_at is null and u.location != 'trash'"
            " order by coalesce(f.orig_rel_path, f.rel_path), u.id"
        )
    ]


def _undated_items(conn: sqlite3.Connection, ids: list[str]) -> list[dict]:
    rows = {
        r["id"]: r
        for r in _rows(
            conn,
            "select u.id, u.primary_file_id, u.location, f.rel_path, f.kind from units u"
            " left join files f on f.id = u.primary_file_id where u.id in ({marks})",
            ids,
        )
    }
    items = []
    for u in ids:
        r = rows.get(u)
        if r is None:
            continue
        fid = r["primary_file_id"]
        items.append({
            "unit_id": u,
            "file_id": fid,
            "thumb": f"/media/thumb/{fid}" if fid else None,
            "name": (r["rel_path"] or "").rsplit("/", 1)[-1],
            "location": r["location"],
            "kind": r["kind"],
        })
    return items


def _event_exists(conn: sqlite3.Connection, event_key: str) -> bool:
    return conn.execute(
        "select 1 from units where event_key = ? and location != 'trash' limit 1", (event_key,)
    ).fetchone() is not None


def _parse_day(value: str, what: str) -> date:
    try:
        if len(value) != 10:
            raise ValueError(value)
        return date.fromisoformat(value)
    except ValueError:
        raise UserError(f"{what} tidak valid. Pilih tanggal dari kalender.", 400) from None


def _exclusive(request: Request):
    """Same guard as /api/review/ai-group: no scan or move job may run meanwhile."""
    return request.app.state.jobs.exclusive()


# --- page ----------------------------------------------------------------------------------


@router.get("/tanggal")
def dates_page(request: Request):
    return render(request, "dates.html", active="tanggal")


# --- read ----------------------------------------------------------------------------------


@router.get("/api/dates/counts")
def counts(conn: sqlite3.Connection = Depends(get_conn)):
    return {"proposals": len(infer.event_proposals(conn)), "undated": len(_undated_ids(conn))}


@router.get("/api/dates/events")
def events(conn: sqlite3.Connection = Depends(get_conn)):
    return {"events": [_event_payload(conn, p) for p in infer.event_proposals(conn)]}


@router.get("/api/dates/undated")
def undated(offset: int = 0, limit: int = UNDATED_PAGE, conn: sqlite3.Connection = Depends(get_conn)):
    if offset < 0 or not 1 <= limit <= 1000:
        raise UserError("Halaman tidak valid", 400)
    ids = _undated_ids(conn)
    return {"unit_ids": ids, "total": len(ids), "items": _undated_items(conn, ids[offset : offset + limit])}


# --- decisions -----------------------------------------------------------------------------


@router.post("/api/dates/events/{event_key:path}/approve")
def approve(event_key: str, body: RangeBody, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    start = _parse_day(body.range_start, "Tanggal mulai")
    end = _parse_day(body.range_end, "Tanggal selesai")
    if start > end:
        raise UserError("Tanggal mulai harus sebelum atau sama dengan tanggal selesai.", 400)
    if not _event_exists(conn, event_key):
        raise UserError("Acara tidak ditemukan. Muat ulang halaman.", 404)
    try:
        with _exclusive(request):
            n = infer.approve_event(conn, event_key, start.isoformat(), end.isoformat())
    except JobBusy as exc:
        raise UserError(explain(exc), 409) from exc
    return {"ok": True, "dated": n}


@router.post("/api/dates/events/{event_key:path}/ignore")
def ignore(event_key: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    if not _event_exists(conn, event_key):
        raise UserError("Acara tidak ditemukan. Muat ulang halaman.", 404)
    try:
        with _exclusive(request):
            infer.ignore_event(conn, event_key)
    except JobBusy as exc:
        raise UserError(explain(exc), 409) from exc
    return {"ok": True}


@router.post("/api/dates/manual")
def manual(body: ManualBody, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    ids = list(dict.fromkeys(body.unit_ids))
    if not ids:
        raise UserError("Pilih dulu foto yang mau diberi tanggal.", 400)
    if len(ids) > MAX_MANUAL:
        raise UserError(f"Maksimal {MAX_MANUAL} foto sekaligus.", 400)
    if not 1900 <= body.year <= date.today().year + 1:
        raise UserError("Tahun tidak valid.", 400)
    if not 1 <= body.month <= 12:
        raise UserError("Bulan tidak valid.", 400)
    if body.day is not None and not 1 <= body.day <= 31:
        raise UserError("Hari tidak valid.", 400)
    try:
        day = date(body.year, body.month, 1 if body.day is None else body.day)
    except ValueError:
        raise UserError("Tanggal itu tidak ada di bulan tersebut.", 400) from None
    precision = "day" if body.day is not None else "month"
    taken_at = f"{day.isoformat()}T12:00:00"
    found = {r[0] for r in _rows(conn, "select id from units where id in ({marks}) and location != 'trash'", ids)}
    if len(found) != len(ids):
        raise UserError("Sebagian foto tidak ditemukan. Muat ulang halaman.", 404)
    try:
        with _exclusive(request):
            try:
                for i in range(0, len(ids), _CHUNK):
                    part = ids[i : i + _CHUNK]
                    # One manual row per unit: the newest decision replaces the earlier one.
                    conn.execute(
                        f"delete from date_overrides where source = 'manual'"
                        f" and unit_id in ({','.join('?' * len(part))})",
                        part,
                    )
                for u in ids:
                    store.set_override(conn, u, taken_at, precision, "manual", None, "owner", commit=False)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
    except JobBusy as exc:
        raise UserError(explain(exc), 409) from exc
    return {"ok": True, "dated": len(ids)}


# --- xmp job -------------------------------------------------------------------------------


def _write_text(res) -> str:
    parts = [f"{res.written} file .xmp ditulis"]
    if res.rewritten:
        parts.append(f"{res.rewritten} diperbarui")
    if res.unchanged:
        parts.append(f"{res.unchanged} sudah benar")
    if res.not_needed:
        parts.append(f"{res.not_needed} tidak perlu (tanggal kamera)")
    if res.skipped_foreign:
        parts.append(f"{len(res.skipped_foreign)} dilewati (punya XMP sendiri)")
    if res.errors:
        parts.append(f"{len(res.errors)} gagal")
    return ", ".join(parts) + "."


def _write_job(root: Path):
    def run(report, stop):
        conn = open_db(root)  # sqlite connections are thread-bound
        try:
            journal_errors: list[str] = []
            reconcile_or_refuse(conn, root, journal_errors)  # raises PendingMoves
            try:
                res = write_dates(conn, root, load_settings(conn))
            except InvalidTimezone as e:
                raise UserError(str(e), 400) from None
        finally:
            conn.close()
        errors = journal_errors + res.errors
        return {
            "written": res.written,
            "rewritten": res.rewritten,
            "unchanged": res.unchanged,
            "not_needed": res.not_needed,
            "skipped_foreign": len(res.skipped_foreign),
            "skipped_paths": res.skipped_foreign[:MAX_REPORTED],
            "stale_own": len(res.stale_own),
            "errors": errors[:MAX_REPORTED],
            "error_count": len(errors),
            "text": _write_text(res),
        }

    return run


@router.post("/api/dates/write")
def write(request: Request, conn: sqlite3.Connection = Depends(get_conn), root: Path = Depends(get_root)):
    tz = load_settings(conn).timezone
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise UserError(
            f"Zona waktu di pengaturan tidak dikenal: {tz!r}. Perbaiki di Pengaturan"
            " (misalnya Asia/Jakarta). Belum ada file .xmp yang ditulis.",
            400,
        ) from None
    return start_job(request, "write-dates", WRITE_LABEL, _write_job(root))
