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

# WhatsApp Image 2024-10-30 at 3.55.48 PM.jpeg, WhatsApp Video 2024-11-20 at 10.04.48 PM(1).mp4
_WA_WEB_RE = re.compile(
    r"WhatsApp (?:Image|Video) (\d{4})-(\d{2})-(\d{2}) at (\d{1,2})\.(\d{2})\.(\d{2}) ([AP]M)",
    re.IGNORECASE,
)
_WA_CLASSIC_RE = re.compile(r"^(?:IMG|VID)-\d{8}-WA\d+", re.IGNORECASE)
_WA_WEB_NAME_RE = re.compile(r"^WhatsApp (?:Image|Video) \d{4}-\d{2}-\d{2} at ", re.IGNORECASE)


def is_whatsapp_name(name: str) -> bool:
    """True for WhatsApp phone names (IMG-YYYYMMDD-WA####) and Web/Desktop names."""
    return bool(_WA_CLASSIC_RE.match(name) or _WA_WEB_NAME_RE.match(name))


def _parse_whatsapp_web(name: str) -> datetime | None:
    m = _WA_WEB_RE.search(name)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(g) for g in m.groups()[:6])
    if not 1 <= h <= 12:
        return None
    h %= 12
    if m.group(7).upper() == "PM":
        h += 12
    try:
        return datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None


def parse_filename_date(name: str) -> datetime | None:
    wa = _parse_whatsapp_web(name)
    if wa is not None:
        return wa
    if _WA_WEB_RE.search(name):
        return None
    for pattern in _FILENAME_PATTERNS:
        m = pattern.search(name)
        if not m:
            continue
        try:
            return datetime(*(int(g) for g in m.groups()))
        except ValueError:
            continue
    return None


def filename_has_time(name: str) -> bool:
    """True when the first filename date pattern that parses also carries a time of day.

    WhatsApp names (IMG-20230812-WA0001) only give the day; their 00:00:00 is not a real time.
    """
    if _WA_WEB_RE.search(name):
        return _parse_whatsapp_web(name) is not None
    for pattern in _FILENAME_PATTERNS:
        m = pattern.search(name)
        if not m:
            continue
        try:
            datetime(*(int(g) for g in m.groups()))
        except ValueError:
            continue
        return len(m.groups()) > 3
    return False


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
