"""SQLite storage for date overrides and per-event date decisions."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from ..db import new_id

_CHUNK = 500


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def set_override(
    conn: sqlite3.Connection,
    unit_id: str,
    taken_at: str,
    precision: str,
    source: str,
    ref: str | None,
    decided_by: str,
    *,
    commit: bool = True,
) -> None:
    conn.execute(
        "insert into date_overrides (id, unit_id, taken_at, precision, source, ref, decided_by, created_at)"
        " values (?, ?, ?, ?, ?, ?, ?, ?)",
        (new_id(), unit_id, taken_at, precision, source, ref, decided_by, _now()),
    )
    if commit:
        conn.commit()


def clear_auto_overrides(conn: sqlite3.Connection, source: str, *, commit: bool = True) -> None:
    """Delete automatic rows of one source; owner decisions are never touched."""
    conn.execute("delete from date_overrides where decided_by = 'auto' and source = ?", (source,))
    if commit:
        conn.commit()


def effective_dates(
    conn: sqlite3.Connection, unit_ids: list[str] | None = None
) -> dict[str, tuple[str | None, str | None, str | None]]:
    """unit_id -> (taken_at, source, precision), read from the unit_dates view."""
    sql = "select unit_id, taken_at, source, precision from unit_dates"
    if unit_ids is None:
        rows = conn.execute(sql).fetchall()
    else:
        rows = []
        ids = list(unit_ids)
        for i in range(0, len(ids), _CHUNK):
            part = ids[i : i + _CHUNK]
            marks = ",".join("?" * len(part))
            rows += conn.execute(f"{sql} where unit_id in ({marks})", part).fetchall()
    return {r["unit_id"]: (r["taken_at"], r["source"], r["precision"]) for r in rows}


def record_event_decision(
    conn: sqlite3.Connection,
    event_key: str,
    decision: str,
    range_start: str | None,
    range_end: str | None,
    camera_start: str | None,
    camera_end: str | None,
    *,
    commit: bool = True,
) -> None:
    conn.execute(
        "insert into event_date_decisions"
        " (id, event_key, decision, range_start, range_end, camera_start, camera_end, created_at)"
        " values (?, ?, ?, ?, ?, ?, ?, ?)",
        (new_id(), event_key, decision, range_start, range_end, camera_start, camera_end, _now()),
    )
    if commit:
        conn.commit()


def event_decisions(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """event_key -> newest decision row (rowid breaks 1 s timestamp ties)."""
    out: dict[str, sqlite3.Row] = {}
    for r in conn.execute("select * from event_date_decisions order by created_at, rowid"):
        out[r["event_key"]] = r
    return out
