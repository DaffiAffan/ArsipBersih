"""Scan stage `dates`: borrow camera dates from originals and handle per-event dates.

- `borrowed` (automatic, recomputed every run): a WhatsApp copy (filename date, WA name) in a
  `duplikat-persis`/`duplikat-versi` group with a camera-dated unit takes that unit's date.
- `event` (owner decision per event): WhatsApp photos of an event whose send date lies more
  than LATE_DAYS outside the event's camera range are proposed; once approved they get dates
  spread inside the approved range, keeping their order. `manual` rows are never touched.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Literal

from ..config import Settings
from ..dates import is_whatsapp_name
from ..events import current_event_key
from . import store
from .clock import device_warnings

LATE_DAYS = 2
LENDING_GROUPS = ("duplikat-persis", "duplikat-versi")
CAMERA_SOURCES = ("exif", "quicktime")
RANGE_SOURCES = ("exif", "quicktime", "borrowed")
_WA_NUMBER_RE = re.compile(r"-WA(\d+)", re.IGNORECASE)


@dataclass
class EventProposal:
    event_key: str
    event_name: str
    camera_start: str
    camera_end: str
    unit_ids: list[str]
    status: Literal["new", "changed"]


@dataclass
class DatesResult:
    borrowed: int = 0
    proposals: list[EventProposal] = field(default_factory=list)
    applied_events: int = 0  # approved events whose decision is in force this run
    clock_warnings: list[str] = field(default_factory=list)


@dataclass
class _WaUnit:
    unit_id: str
    raw: datetime
    wa_no: int
    name: str

    @property
    def order(self) -> tuple:
        return (self.raw, self.wa_no, self.name, self.unit_id)


@dataclass
class _Event:
    key: str
    name: str
    unit_ids: list[str] = field(default_factory=list)
    camera: list[str] = field(default_factory=list)  # effective camera/borrowed dates
    wa: list[_WaUnit] = field(default_factory=list)  # WA units an event date may apply to

    @property
    def camera_range(self) -> tuple[str, str] | None:
        return (min(self.camera), max(self.camera)) if self.camera else None


def _basename(path: str | None) -> str:
    return (path or "").rsplit("/", 1)[-1]


def _day(s: str) -> date:
    return datetime.fromisoformat(s).date()


def _is_late(d: date, start: str, end: str) -> bool:
    return (_day(start) - d).days > LATE_DAYS or (d - _day(end)).days > LATE_DAYS


def _within(inner: tuple[str, str], outer: tuple[str | None, str | None]) -> bool:
    if outer[0] is None or outer[1] is None:
        return False
    return _day(outer[0]) <= _day(inner[0]) and _day(inner[1]) <= _day(outer[1])


def _units(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "select u.id, u.taken_at, u.date_source, u.event_key, u.event_name, u.location,"
        " coalesce(f.orig_rel_path, f.rel_path) as path"
        " from units u left join files f on f.id = u.primary_file_id"
    ).fetchall()


def _is_wa_filename_unit(r: sqlite3.Row) -> bool:
    return r["date_source"] == "filename" and bool(r["taken_at"]) and is_whatsapp_name(
        _basename(r["path"])
    )


# --- borrowed ---------------------------------------------------------------------------


def _borrow(conn: sqlite3.Connection, units: dict[str, sqlite3.Row]) -> int:
    store.clear_auto_overrides(conn, "borrowed", commit=False)
    marks = ",".join("?" * len(LENDING_GROUPS))
    members: dict[str, list[str]] = defaultdict(list)
    keep: dict[str, str | None] = {}
    for r in conn.execute(
        "select g.id, g.suggested_keep_unit_id, m.unit_id from groups g"
        f" join group_members m on m.group_id = g.id where g.kind in ({marks})"
        " order by g.id, m.unit_id",
        LENDING_GROUPS,
    ):
        members[r["id"]].append(r["unit_id"])
        keep[r["id"]] = r["suggested_keep_unit_id"]
    done: set[str] = set()
    for gid, ids in members.items():
        rows = [units[u] for u in ids if u in units]
        donors = [r for r in rows if r["date_source"] in CAMERA_SOURCES and r["taken_at"]]
        if not donors:
            continue
        preferred = [r for r in donors if r["id"] == keep[gid]]
        donor = preferred[0] if preferred else min(donors, key=lambda r: (r["taken_at"], r["id"]))
        for r in rows:
            if r["id"] in done or not _is_wa_filename_unit(r):
                continue
            store.set_override(conn, r["id"], donor["taken_at"], "second", "borrowed",
                               donor["id"], "auto", commit=False)
            done.add(r["id"])
    return len(done)


# --- events -----------------------------------------------------------------------------


def _manual_units(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("select distinct unit_id from date_overrides where source='manual'")}


def _events(conn: sqlite3.Connection, units: list[sqlite3.Row] | None = None) -> dict[str, _Event]:
    rows = [r for r in (units if units is not None else _units(conn))
            if r["event_key"] and r["location"] != "trash"]
    eff = store.effective_dates(conn, [r["id"] for r in rows])
    manual = _manual_units(conn)
    out: dict[str, _Event] = {}
    for r in sorted(rows, key=lambda r: r["id"]):
        ev = out.setdefault(r["event_key"], _Event(r["event_key"], r["event_name"] or r["event_key"]))
        if r["event_name"] and r["event_name"] < ev.name:
            ev.name = r["event_name"]
        ev.unit_ids.append(r["id"])
        taken, source, _ = eff.get(r["id"], (None, None, None))
        if source in RANGE_SOURCES and taken:
            ev.camera.append(taken)
        if (
            _is_wa_filename_unit(r)
            and r["id"] not in manual
            and source in ("filename", "event")
        ):
            name = _basename(r["path"])
            m = _WA_NUMBER_RE.search(name)
            ev.wa.append(_WaUnit(r["id"], datetime.fromisoformat(r["taken_at"]),
                                 int(m.group(1)) if m else 0, name))
    for ev in out.values():
        ev.wa.sort(key=lambda u: u.order)
    return out


def _decisions(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """Current event key -> newest decision, following renames/merges of the event."""
    out: dict[str, sqlite3.Row] = {}
    mapped: dict[str, str] = {}
    for r in conn.execute("select * from event_date_decisions order by created_at, rowid"):
        key = r["event_key"]
        if key not in mapped:
            mapped[key] = current_event_key(conn, key)
        out[mapped[key]] = r
    return out


def _late(ev: _Event, start: str, end: str) -> list[_WaUnit]:
    return [u for u in ev.wa if _is_late(u.raw.date(), start, end)]


def _spread(n: int, start: str, end: str) -> list[str]:
    """n timestamps strictly inside the whole days start..end, evenly spaced, in order."""
    lo = datetime.combine(_day(start), time.min)
    hi = datetime.combine(_day(end), time(23, 59, 59))
    span = (hi - lo).total_seconds()
    return [
        (lo + timedelta(seconds=round(span * (k + 1) / (n + 1)))).isoformat(timespec="seconds")
        for k in range(n)
    ]


def _apply(conn: sqlite3.Connection, ev: _Event, start: str, end: str) -> int:
    """Give the event's late WA units dates inside start..end; returns rows written."""
    late = _late(ev, start, end)
    current = store.effective_dates(conn, [u.unit_id for u in late])
    written = 0
    for u, stamp in zip(late, _spread(len(late), start, end)):
        taken, source, _ = current.get(u.unit_id, (None, None, None))
        if source == "event" and taken == stamp:
            continue
        conn.execute("delete from date_overrides where unit_id = ? and source = 'event'", (u.unit_id,))
        store.set_override(conn, u.unit_id, stamp, "second", "event", ev.key, "owner", commit=False)
        written += 1
    return written


def _proposal(ev: _Event, dec: sqlite3.Row | None) -> EventProposal | None:
    cam = ev.camera_range
    if cam is None:
        return None
    late = _late(ev, *cam)
    status: Literal["new", "changed"]
    if dec is None:
        if not late:
            return None
        status = "new"
    elif dec["decision"] == "approved":
        if _within(cam, (dec["range_start"], dec["range_end"])):
            return None
        status = "changed"
    else:  # ignored: quiet until the camera range grows past the one the owner saw
        if not late or _within(cam, (dec["camera_start"], dec["camera_end"])):
            return None
        status = "changed"
    return EventProposal(ev.key, ev.name, cam[0], cam[1], [u.unit_id for u in late], status)


def event_proposals(conn: sqlite3.Connection) -> list[EventProposal]:
    """Events waiting for an owner decision (read only)."""
    decisions = _decisions(conn)
    out = [p for ev in _events(conn).values() if (p := _proposal(ev, decisions.get(ev.key)))]
    return sorted(out, key=lambda p: (p.camera_start, p.event_key))


def _clear_event(conn: sqlite3.Connection, event_key: str) -> None:
    conn.execute(
        "delete from date_overrides where source = 'event' and decided_by = 'owner'"
        " and (ref = ? or unit_id in (select id from units where event_key = ?))",
        (event_key, event_key),
    )


def approve_event(
    conn: sqlite3.Connection,
    event_key: str,
    range_start: str | None = None,
    range_end: str | None = None,
) -> int:
    """Owner approves event dates for `event_key` (range defaults to the camera range).
    Returns the number of units given an event date."""
    try:
        _clear_event(conn, event_key)
        ev = _events(conn).get(event_key)
        cam = ev.camera_range if ev else None
        start = range_start or (cam[0] if cam else None)
        end = range_end or (cam[1] if cam else None)
        if start is None or end is None:
            raise ValueError(f"event {event_key!r} has no date range")
        if _day(start) > _day(end):
            raise ValueError("range_start is after range_end")
        store.record_event_decision(conn, event_key, "approved", start, end,
                                    cam[0] if cam else None, cam[1] if cam else None,
                                    commit=False)
        n = _apply(conn, ev, start, end) if ev else 0
        conn.commit()
        return n
    except BaseException:
        conn.rollback()
        raise


def ignore_event(conn: sqlite3.Connection, event_key: str) -> None:
    """Owner keeps the WhatsApp send dates for this event; removes its event dates."""
    try:
        _clear_event(conn, event_key)
        ev = _events(conn).get(event_key)
        cam = ev.camera_range if ev else None
        store.record_event_decision(conn, event_key, "ignored", None, None,
                                    cam[0] if cam else None, cam[1] if cam else None,
                                    commit=False)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def run_dates(conn: sqlite3.Connection, settings: Settings) -> DatesResult:
    """Scan stage: recompute borrowed dates, keep approved events applied, list proposals.
    Idempotent; manual rows and owner decisions are never removed."""
    result = DatesResult()
    try:
        rows = _units(conn)
        result.borrowed = _borrow(conn, {r["id"]: r for r in rows})
        decisions = _decisions(conn)
        for ev in _events(conn, rows).values():
            dec = decisions.get(ev.key)
            cam = ev.camera_range
            if dec is None or dec["decision"] != "approved" or cam is None:
                continue
            if _within(cam, (dec["range_start"], dec["range_end"])):
                _apply(conn, ev, dec["range_start"], dec["range_end"])
                result.applied_events += 1
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    result.proposals = event_proposals(conn)
    result.clock_warnings = device_warnings(conn)
    return result
