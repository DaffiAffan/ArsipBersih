"""Capture-date parsing from EXIF, QuickTime and filenames."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MIN_VALID = datetime(1990, 1, 1)

_EXIF_RE = re.compile(
    r"^\s*(\d{4})[:-](\d{2})[:-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})"
    r"(?:\.\d+)?\s*(?:Z|[+-]\d{2}:?\d{2})?\s*$"
)

# Ordered: most specific families first. Each yields groups y, m, d and
# optionally h, mi, s.
_FILENAME_PATTERNS = [
    # Screenshot_2023-08-12-14-30-22-123_com.whatsapp.jpg
    re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})[-_ ](\d{2})-(\d{2})-(\d{2})(?!\d)"),
    # IMG_20230812_143022, VID_..., PXL_20230812_073015123, MVIMG_..., 20230812_143022,
    # Screenshot_20230812-143022
    re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})"),
    # IMG-20230812-WA0001, VID-20230812-WA0007 (date only)
    re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})-WA\d+", re.IGNORECASE),
]


def parse_filename_date(name: str) -> datetime | None:
    for pattern in _FILENAME_PATTERNS:
        m = pattern.search(name)
        if not m:
            continue
        try:
            return datetime(*(int(g) for g in m.groups()))
        except ValueError:
            continue
    return None


def parse_exif_datetime(s: str | None) -> datetime | None:
    if not s:
        return None
    m = _EXIF_RE.match(s)
    if not m:
        return None
    try:
        return datetime(*(int(g) for g in m.groups()))
    except ValueError:  # includes 0000:00:00 00:00:00
        return None


def _quicktime_to_local(s: str | None, tz: str) -> datetime | None:
    dt = parse_exif_datetime(s)
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz)).replace(tzinfo=None)


def pick_taken_at(
    exif_dt: str | None,
    qt_dt: str | None,
    filename: str,
    tz: str,
    now: datetime,
) -> tuple[datetime | None, str | None]:
    upper = now + timedelta(days=1)
    candidates = (
        (parse_exif_datetime(exif_dt), "exif"),
        (_quicktime_to_local(qt_dt, tz), "quicktime"),
        (parse_filename_date(filename), "filename"),
    )
    for dt, source in candidates:
        if dt is None:
            continue
        dt = dt.replace(microsecond=0)
        if MIN_VALID <= dt <= upper:
            return dt, source
    return None, None
