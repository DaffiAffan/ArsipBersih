"""Write corrected dates to `<media file>.xmp` sidecars that Immich reads on import.

Safety rules:
- Media files are never opened; only the sidecar path next to them is written.
- A sidecar is written only where no file exists yet, or over a sidecar this module wrote
  (recorded in `xmp_writes`) whose bytes on disk still hash to the recorded SHA-256.
  Anything else is the owner's (or another app's) and is never touched; a stem-only
  `IMG.xmp` next to `IMG.jpg` also counts as the owner's sidecar for that photo.
- Writes go to `<target>.partial` first and are renamed into place; a new sidecar uses a
  rename that refuses an existing target.
- One sidecar per unit, for its primary file: the photo of a Live Photo or RAW+JPG pair
  (Immich links the partner itself), the file itself for a single.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import Settings, load_settings
from ..db import new_id
from ..layout import long_path
from ..mover import PARTIAL_SUFFIX, _abs
from .store import effective_dates

CAMERA_SOURCES = ("exif", "quicktime")
CREATOR_TOOL = "ArsipBersih"
MAX_REPORTED = 20
_CHUNK = 500

_TEMPLATE = """<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="{tool}">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:exif="http://ns.adobe.com/exif/1.0/"
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"
   exif:DateTimeOriginal="{dt}"
   xmp:CreateDate="{dt}"
   photoshop:DateCreated="{dt}"
   xmp:CreatorTool="{tool}"/>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""


@dataclass
class XmpResult:
    written: int = 0
    rewritten: int = 0
    skipped_foreign: list[str] = field(default_factory=list)  # media rel paths
    not_needed: int = 0  # camera-dated (exif/quicktime): Immich reads the file itself
    unchanged: int = 0  # our sidecar already holds this date
    errors: list[str] = field(default_factory=list)


def _xmp_datetime(taken_at: str, precision: str | None, tz: str) -> str:
    dt = datetime.fromisoformat(taken_at)
    if precision == "month":
        dt = dt.replace(day=1, hour=12, minute=0, second=0, microsecond=0)
    elif precision == "day":
        # Noon: no time zone conversion in a viewer can push it onto another date.
        dt = dt.replace(hour=12, minute=0, second=0, microsecond=0)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return dt.isoformat(timespec="seconds")


def render_xmp(taken_at: str, precision: str, tz: str) -> bytes:
    """XMP packet with the date in exif:DateTimeOriginal, xmp:CreateDate and
    photoshop:DateCreated, with the UTC offset of `tz` at that date."""
    return _TEMPLATE.format(dt=_xmp_datetime(taken_at, precision, tz), tool=CREATOR_TOOL).encode("utf-8")


# --- filesystem ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(long_path(path), "rb") as f:
        while block := f.read(1 << 16):
            h.update(block)
    return h.hexdigest()


def _exists(path: Path) -> bool:
    return os.path.lexists(long_path(path))


def _write_partial(partial: Path, data: bytes) -> None:
    # "xb": a leftover or foreign .partial is never overwritten either.
    with open(long_path(partial), "xb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _place_new(partial: Path, target: Path) -> None:
    """Rename that fails when the target exists (never overwrites)."""
    if os.name == "nt":
        os.rename(long_path(partial), long_path(target))  # FileExistsError on Windows
    else:
        os.link(long_path(partial), long_path(target))  # FileExistsError on POSIX
        os.unlink(long_path(partial))


def _discard_partial(partial: Path) -> None:
    try:
        if _exists(partial):
            os.unlink(long_path(partial))
    except OSError:
        pass


# --- DB -----------------------------------------------------------------------------------


@dataclass
class _Unit:
    unit_id: str
    rel_path: str
    source_label: str | None
    present: bool


@dataclass
class _Own:
    write_id: str
    file_id: str
    rel_path: str
    sha256: str | None


def _archive_units(conn: sqlite3.Connection, unit_ids: list[str] | None) -> list[_Unit]:
    sql = (
        "select u.id, f.rel_path, f.source_label, f.missing, f.purged from units u"
        " join files f on f.id = u.primary_file_id where u.location = 'archive'"
    )
    if unit_ids is None:
        rows = conn.execute(sql).fetchall()
    else:
        rows = []
        ids = list(dict.fromkeys(unit_ids))
        for i in range(0, len(ids), _CHUNK):
            part = ids[i : i + _CHUNK]
            rows += conn.execute(f"{sql} and u.id in ({','.join('?' * len(part))})", part).fetchall()
    units = [
        _Unit(r["id"], r["rel_path"], r["source_label"], not r["missing"] and not r["purged"])
        for r in rows
    ]
    return sorted(units, key=lambda u: u.rel_path)


def _sidecars(conn: sqlite3.Connection) -> tuple[dict[str, _Own], dict[str, list[str]]]:
    """Per unit: our recorded sidecar, and the rel paths of every other .xmp it has."""
    own: dict[str, _Own] = {}
    own_ids: set[str] = set()
    for r in conn.execute(
        "select w.id, w.file_id, w.sha256, f.rel_path, f.unit_id from xmp_writes w"
        " join files f on f.id = w.file_id where f.unit_id is not null and f.purged = 0"
        " order by w.created_at, w.rowid"
    ):
        own[r["unit_id"]] = _Own(r["id"], r["file_id"], r["rel_path"], r["sha256"])
        own_ids.add(r["file_id"])
    foreign: dict[str, list[str]] = {}
    for r in conn.execute(
        "select id, unit_id, rel_path from files"
        " where ext = 'xmp' and unit_id is not null and purged = 0"
    ):
        if r["id"] not in own_ids:
            foreign.setdefault(r["unit_id"], []).append(r["rel_path"])
    return own, foreign


def _tracked(conn: sqlite3.Connection, rel: str) -> bool:
    return conn.execute(
        "select 1 from files where rel_path = ? collate nocase limit 1", (rel,)
    ).fetchone() is not None


# --- main ---------------------------------------------------------------------------------


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _write_one(
    conn: sqlite3.Connection,
    root: Path,
    unit: _Unit,
    own: _Own | None,
    taken_at: str,
    data: bytes,
) -> None:
    rel = own.rel_path if own else unit.rel_path + ".xmp"
    target = _abs(root, rel)
    partial = target.with_name(target.name + PARTIAL_SUFFIX)
    sha = hashlib.sha256(data).hexdigest()
    replace = own is not None and _exists(target)
    _write_partial(partial, data)
    try:
        with conn:  # committed only when the file is in place
            if own is None:
                file_id = new_id()
                conn.execute(
                    "insert into files (id, unit_id, rel_path, orig_rel_path, size, kind, ext,"
                    " source_label, sha256, hash_status) values (?, ?, ?, NULL, ?, 'sidecar',"
                    " 'xmp', ?, ?, 'done')",
                    (file_id, unit.unit_id, rel, len(data), unit.source_label, sha),
                )
                conn.execute(
                    "insert into xmp_writes (id, file_id, xmp_rel_path, taken_at, sha256,"
                    " created_at) values (?, ?, ?, ?, ?, ?)",
                    (new_id(), file_id, rel, taken_at, sha, _now()),
                )
            else:
                file_id = own.file_id
                conn.execute(
                    "update files set size=?, sha256=?, hash_status='done', missing=0, error=null"
                    " where id=?",
                    (len(data), sha, file_id),
                )
                conn.execute(
                    "update xmp_writes set xmp_rel_path=?, taken_at=?, sha256=?, created_at=?"
                    " where id=?",
                    (rel, taken_at, sha, _now(), own.write_id),
                )
            if replace:
                # Re-check right before replacing: edited since the first check -> keep it.
                if _sha256(target) != own.sha256:
                    raise FileExistsError(f"{rel} changed on disk")
                os.replace(long_path(partial), long_path(target))
            else:
                _place_new(partial, target)
            conn.execute(
                "update files set mtime_ns=? where id=?",
                (os.stat(long_path(target)).st_mtime_ns, file_id),
            )
    finally:
        _discard_partial(partial)


def write_dates(
    conn: sqlite3.Connection, root: Path, settings: Settings, unit_ids: list[str] | None = None
) -> XmpResult:
    """Write or refresh date sidecars for archive units whose effective date is not a
    camera date. `unit_ids=None` means every archive unit."""
    root = Path(root)
    res = XmpResult()
    units = _archive_units(conn, unit_ids)
    dates = effective_dates(conn, [u.unit_id for u in units])
    own_by_unit, foreign_by_unit = _sidecars(conn)
    for unit in units:
        taken_at, source, precision = dates.get(unit.unit_id, (None, None, None))
        if source in CAMERA_SOURCES:
            res.not_needed += 1
            continue
        if not taken_at or not unit.present:
            continue
        own = own_by_unit.get(unit.unit_id)
        try:
            media = _abs(root, unit.rel_path)
            stem_xmp = media.with_name(posixpath.splitext(media.name)[0] + ".xmp")
            if foreign_by_unit.get(unit.unit_id) or _exists(stem_xmp):
                res.skipped_foreign.append(unit.rel_path)
                continue
            data = render_xmp(taken_at, precision or "second", settings.timezone)
            if own is None:
                target_rel = unit.rel_path + ".xmp"
                if _exists(_abs(root, target_rel)) or _tracked(conn, target_rel):
                    res.skipped_foreign.append(unit.rel_path)
                    continue
                _write_one(conn, root, unit, None, taken_at, data)
                res.written += 1
                continue
            target = _abs(root, own.rel_path)
            if _exists(target):
                if _sha256(target) != own.sha256:
                    res.skipped_foreign.append(unit.rel_path)  # edited by someone: theirs now
                    continue
                if own.sha256 == hashlib.sha256(data).hexdigest():
                    res.unchanged += 1
                    continue
            _write_one(conn, root, unit, own, taken_at, data)
            res.rewritten += 1
        except FileExistsError:
            res.skipped_foreign.append(unit.rel_path)  # appeared between check and write
        except (OSError, ValueError, sqlite3.Error) as e:
            res.errors.append(f"{unit.rel_path}: {type(e).__name__}: {e}")
    return res


def write_dates_after_move(
    conn: sqlite3.Connection, root: Path, unit_ids: list[str], errors: list[str]
) -> XmpResult | None:
    """Hook for Rapikan/Apply: write sidecars for the units just moved into Arsip.

    Never raises: the move already happened, so problems become notes in `errors`.
    """
    if not unit_ids:
        return None
    try:
        res = write_dates(conn, root, load_settings(conn), unit_ids)
    except Exception as e:  # noqa: BLE001 - the batch result must survive
        errors.append(f"xmp: dates not written: {type(e).__name__}: {e}")
        return None
    errors.extend(f"xmp: {e}" for e in res.errors)
    errors.extend(
        f"xmp: {rel}: has its own .xmp, date not written"
        for rel in res.skipped_foreign[:MAX_REPORTED]
    )
    if len(res.skipped_foreign) > MAX_REPORTED:
        errors.append(f"xmp: {len(res.skipped_foreign) - MAX_REPORTED} more with their own .xmp")
    return res
