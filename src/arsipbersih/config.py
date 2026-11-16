"""Settings dataclass and persistence in the DB settings table."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from dataclasses import dataclass

DEFAULT_GENERIC_FOLDERS: tuple[str, ...] = (
    "DCIM", "Camera", "Camera Roll", "Pictures", "Photos", "Foto", "Gambar",
    "Video", "Videos", "Movies", "Screenshots", "Screen recordings",
    "WhatsApp", "WhatsApp Images", "WhatsApp Video", "WhatsApp Animated Gifs",
    "Sent", "Private", "Media", "Telegram", "Telegram Images", "Telegram Video",
    "Instagram", "Facebook", "Messenger", "TikTok", "CapCut", "Snapseed",
    "Download", "Downloads", "Unduhan", "Bluetooth", "Gallery", "Edited",
    "Collage", "Restored", "Backup", "MISC", "GoPro",
)


@dataclass(frozen=True)
class Settings:
    phash_version_max: int = 6
    # [Claude] Max 32-px block difference (z-scored gray, 768 px) for a photo version;
    # calibrated on D:\TESTING: true versions <= 0.095, same-layout screenshots >= 0.122.
    version_detail_max: float = 0.11
    phash_burst_max: int = 12
    video_frame_phash_max: int = 8      # [Claude] per-frame distance for video version-dups
    burst_window_s: float = 10.0
    short_video_s: float = 2.0
    live_photo_mov_max_s: float = 4.0
    timezone: str = "Asia/Jakarta"
    workers: int = 3
    thumb_px: int = 320
    preview_px: int = 1600
    analysis_px: int = 512
    blur_threshold: float | None = None  # set by calibrate; None -> blur_fallback
    blur_fallback: float = 25.0          # [Claude]
    kepencet_sharpness_max: float = 10.0 # [Claude]
    kepencet_luma_max: float = 25.0      # [Claude]
    kepencet_std_max: float = 8.0        # [Claude]
    dark_luma_max: float = 20.0          # [Claude]
    bright_luma_min: float = 240.0       # [Claude]
    clipping_max: float = 0.5            # [Claude] fraction of pixels <=5 or >=250
    dark_video_luma_max: float = 15.0    # [Claude]
    generic_folders: tuple[str, ...] = DEFAULT_GENERIC_FOLDERS


def load_settings(conn: sqlite3.Connection) -> Settings:
    stored = {r["key"]: json.loads(r["value"]) for r in conn.execute("select key, value from settings")}
    known = {f.name for f in dataclasses.fields(Settings)}
    values = {k: v for k, v in stored.items() if k in known}
    if "generic_folders" in values:
        values["generic_folders"] = tuple(values["generic_folders"])
    return Settings(**values)


def save_settings(conn: sqlite3.Connection, s: Settings) -> None:
    with conn:
        for f in dataclasses.fields(s):
            conn.execute(
                "insert into settings(key, value) values(?, ?) "
                "on conflict(key) do update set value = excluded.value",
                (f.name, json.dumps(getattr(s, f.name))),
            )
