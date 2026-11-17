"""Workflow position for the home page: which of the four steps the owner is on."""

from __future__ import annotations

import sqlite3

from ..decisions import trash_totals
from .queries import category_counts


def home_steps(conn: sqlite3.Connection, *, scanned: bool) -> dict:
    by_loc = {r[0]: r[1] for r in conn.execute("select location, count(*) from units group by location")}
    events = conn.execute(
        "select count(distinct event_key) from units"
        " where location in ('source', 'archive') and event_key is not null"
    ).fetchone()[0]
    cats = category_counts(conn)
    # For group categories `count` is a number of groups; the home card shows it as "menunggu review".
    review = sum(c["count"] for c in cats)
    review_done = sum(c["done"] for c in cats)
    trash_files, trash_bytes = trash_totals(conn)
    if not scanned:
        current = 1
    elif by_loc.get("source", 0) > 0:
        current = 2
    elif review > 0:
        current = 3
    elif trash_files > 0:
        current = 4
    else:
        current = 0
    return {
        "current": current,
        "totals": {
            "units": sum(by_loc.values()),
            "archive": by_loc.get("archive", 0),
            "source": by_loc.get("source", 0),
            "review": review,
            "review_done": review_done,
            "trash_files": trash_files,
            "trash_bytes": trash_bytes,
            "events": events,
        },
    }
