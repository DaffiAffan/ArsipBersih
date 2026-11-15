"""Path and filename heuristics shared by grouping and classification."""

from __future__ import annotations

import re

CHAT_WORDS = ("whatsapp", "telegram", "messenger", "line", "signal", "wechat")
CHAT_NAME_RE = r"^(IMG|VID|AUD|PTT|STK)-\d{8}-WA\d{4}"
DOWNLOAD_DIRS = ("download", "downloads", "unduhan", "bluetooth")
TRASH_MARKERS = (".trashed", ".trash", "recycle bin", "$recycle.bin")
CAMERA_NAME_RE = r"^(IMG|VID|DSC|DSCF|DSCN|_DSC|PXL|MVIMG|GX|GH|GP|GOPR|P\d{7})|^\d{8}_\d{6}"
BURST_RE = r"_(BURST|COVER)\d*"

_CHAT_NAME = re.compile(CHAT_NAME_RE, re.IGNORECASE)
_CAMERA_NAME = re.compile(CAMERA_NAME_RE, re.IGNORECASE)
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")
_SCREENSHOT_DIRS = ("screenshots", "screenshot", "screen recordings", "screen recording",
                    "screenrecorder", "tangkapan layar")
_SCREENSHOT_NAME = re.compile(
    r"^(screenshot|screen[ _-]?shot|screen[ _-]?record|tangkapan layar)", re.IGNORECASE
)
# Long chat words match as substrings ("WhatsAppImages"); short ones only as whole words.
_CHAT_SUBSTRINGS = tuple(w for w in CHAT_WORDS if len(w) > 6)
_CHAT_TOKENS = frozenset(w for w in CHAT_WORDS if len(w) <= 6)


def segments_of(orig_rel_path: str) -> list[str]:
    """Directory parts below the source label (Sumber/<label>/...); filename excluded."""
    parts = orig_rel_path.split("/")
    if parts and parts[0] == "Sumber":
        return parts[2:-1]
    return parts[1:-1]


def is_chat(segments: list[str], filename: str) -> bool:
    if _CHAT_NAME.match(filename):
        return True
    for seg in segments:
        low = seg.lower()
        if any(w in low for w in _CHAT_SUBSTRINGS):
            return True
        if _CHAT_TOKENS.intersection(_TOKEN_SPLIT.split(low)):
            return True
    return False


def is_download(segments: list[str]) -> bool:
    return any(seg.lower() in DOWNLOAD_DIRS for seg in segments)


def is_trash(segments: list[str], filename: str) -> bool:
    if filename.lower().startswith((".trashed", ".trash")):
        return True
    return any(m in seg.lower() for seg in segments for m in TRASH_MARKERS)


def is_screenshot_name(segments: list[str], filename: str) -> bool:
    if _SCREENSHOT_NAME.match(filename):
        return True
    return any(seg.lower() in _SCREENSHOT_DIRS for seg in segments)


def has_camera_name(filename: str) -> bool:
    return _CAMERA_NAME.match(filename) is not None
