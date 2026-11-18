"""Detect devices whose clock was probably wrong, from the camera dates they wrote."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta

from ..dates import parse_exif_datetime, parse_filename_date

EARLIEST = datetime(2000, 1, 1)
FILENAME_GAP = timedelta(days=30)
MIN_MISMATCHES = 5
MISMATCH_RATIO = 0.20


def _device(make: str | None, model: str | None, source: str | None) -> str | None:
    name = " ".join(p.strip() for p in (make, model) if p and p.strip())
    return name or (source.strip() if source and source.strip() else None)


def device_warnings(conn: sqlite3.Connection, now: datetime | None = None) -> list[str]:
    """One warning per device with an implausible camera date or many filename mismatches."""
    now = now or datetime.now()
    upper = now + timedelta(days=1)
    rows = conn.execute(
        "select u.taken_at, u.date_source, f.rel_path, f.source_label, m.make, m.model,"
        " m.exif_datetime, m.qt_datetime"
        " from units u join files f on f.id = u.primary_file_id"
        " left join media_info m on m.file_id = f.id"
        " where u.location != 'trash' and f.missing = 0 and f.purged = 0"
    ).fetchall()
    bad: dict[str, list[datetime]] = defaultdict(list)
    compared: dict[str, int] = defaultdict(int)
    mismatched: dict[str, int] = defaultdict(int)
    for r in rows:
        dev = _device(r["make"], r["model"], r["source_label"])
        if dev is None:
            continue
        cams = [parse_exif_datetime(r["exif_datetime"]), parse_exif_datetime(r["qt_datetime"])]
        if r["date_source"] in ("exif", "quicktime") and r["taken_at"]:
            try:
                cams.append(datetime.fromisoformat(r["taken_at"]))
            except ValueError:
                pass
        cams = [c for c in cams if c is not None]
        for c in cams:
            if c < EARLIEST or c > upper:
                bad[dev].append(c)
        # Compare the file name date with the (first plausible) camera date.
        plausible = [c for c in cams if EARLIEST <= c <= upper]
        named = parse_filename_date(r["rel_path"].rsplit("/", 1)[-1])
        if plausible and named is not None:
            compared[dev] += 1
            if abs(plausible[0] - named) > FILENAME_GAP:
                mismatched[dev] += 1
    out: list[str] = []
    for dev in sorted(set(bad) | set(mismatched)):
        if bad.get(dev):
            low = [d for d in bad[dev] if d < EARLIEST]
            if low:
                reason = f"ada foto bertanggal {min(low).year}, sebelum tahun 2000"
            else:
                reason = f"ada foto bertanggal {max(bad[dev]).year}, di masa depan"
            out.append(f"Jam {dev} mungkin salah: {reason}")
        elif (
            mismatched[dev] >= MIN_MISMATCHES
            and mismatched[dev] / compared[dev] > MISMATCH_RATIO
        ):
            out.append(
                f"Jam {dev} mungkin salah: {mismatched[dev]} dari {compared[dev]} foto "
                "tanggal kameranya beda lebih dari 30 hari dengan tanggal di nama file"
            )
    return out
