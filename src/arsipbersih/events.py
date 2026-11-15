"""Derive event names from the owner's own folder names."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Settings
from .db import new_id
from .layout import sanitize_component
from .pathrules import segments_of

CAMERA_DIR_RE = re.compile(r"^\d{3}[A-Z_]{5}$", re.I)
DATE_DIR_RE = re.compile(r"^\d{4}[-_]?\d{2}([-_]?\d{2})?$")


def normalize_event_key(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().casefold()


def is_generic(segment: str, generic: set[str], not_event: set[str]) -> bool:
    seg = segment.strip()
    key = normalize_event_key(seg)
    if key in generic or key in not_event:
        return True
    return bool(CAMERA_DIR_RE.match(seg) or DATE_DIR_RE.match(seg))


def resolve_event(
    segments: list[str], generic: set[str], not_event: set[str]
) -> tuple[str | None, str | None]:
    """Return (event display name, subpath of remaining non-generic segments)."""
    name: str | None = None
    rest: list[str] = []
    for seg in segments:
        if is_generic(seg, generic, not_event):
            continue
        if name is None:
            name = seg.strip()
        else:
            rest.append(seg.strip())
    if name is None:
        return None, None
    return name, ("/".join(rest) if rest else None)


@dataclass
class EventSummary:
    key: str
    name: str
    file_count: int
    month: str | None
    sources: list[str]
    sample_path: str


def add_override(conn, kind: str, name: str, value: str | None = None) -> None:
    key = normalize_event_key(name)
    with conn:
        conn.execute("delete from event_overrides where kind=? and name_key=?", (kind, key))
        conn.execute(
            "insert into event_overrides(id, kind, name_key, value) values(?,?,?,?)",
            (new_id(), kind, key, value),
        )


def run_events(conn, settings: Settings) -> list[EventSummary]:
    generic = {normalize_event_key(g) for g in settings.generic_folders}
    not_event: set[str] = set()
    merges: dict[str, str] = {}
    renames: dict[str, str] = {}
    for r in conn.execute("select kind, name_key, value from event_overrides"):
        if r["kind"] == "not_event":
            not_event.add(r["name_key"])
        elif r["kind"] == "merge" and r["value"]:
            merges[r["name_key"]] = r["value"]
        elif r["kind"] == "rename" and r["value"]:
            renames[r["name_key"]] = r["value"]

    rows = conn.execute(
        "select u.id, u.location, u.taken_at, u.event_key, u.event_name, u.event_subpath, "
        "f.orig_rel_path, f.source_label "
        "from units u join files f on f.id = u.primary_file_id "
        "order by f.orig_rel_path, u.id"
    ).fetchall()

    resolved: list[tuple] = []  # (row, key, name, sub)
    canonical: dict[str, str] = {}
    for r in rows:
        if r["location"] == "source":
            name, sub = resolve_event(segments_of(r["orig_rel_path"]), generic, not_event)
            key = None
            if name is not None:
                key = normalize_event_key(name)
                if key in merges:
                    name = merges[key]
                    key = normalize_event_key(name)
                if key in renames:
                    name = renames[key]
                name = sanitize_component(name)
                key = normalize_event_key(name)
        else:
            key, name, sub = r["event_key"], r["event_name"], r["event_subpath"]
        resolved.append((r, key, name, sub))

    # Canonical display name per key: existing archive/review name wins, else first-seen source.
    for r, key, name, _ in resolved:
        if key and r["location"] != "source" and name:
            canonical.setdefault(key, name)
    for r, key, name, _ in resolved:
        if key and r["location"] == "source":
            canonical.setdefault(key, name)

    groups: dict[str, dict] = {}
    updates: list[tuple] = []
    for r, key, name, sub in resolved:
        if r["location"] == "source":
            updates.append((key, canonical.get(key) if key else None, sub, r["id"]))
        if not key:
            continue
        g = groups.setdefault(
            key, {"name": canonical[key], "count": 0, "months": [], "sources": set(), "sample": r["orig_rel_path"]}
        )
        g["count"] += 1
        if r["taken_at"]:
            g["months"].append(r["taken_at"][:7])
        if r["source_label"]:
            g["sources"].add(r["source_label"])

    with conn:
        conn.executemany(
            "update units set event_key=?, event_name=?, event_subpath=? where id=?", updates
        )

    return [
        EventSummary(
            key=k,
            name=g["name"],
            file_count=g["count"],
            month=min(g["months"]) if g["months"] else None,
            sources=sorted(g["sources"]),
            sample_path=g["sample"],
        )
        for k, g in groups.items()
    ]
