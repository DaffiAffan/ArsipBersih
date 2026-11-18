"""Per-video frame sampling (one ffmpeg process) and the `video_frames` table."""
from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from .db import new_id
from .tools import NO_CONSOLE_FLAGS

VIDEO_FRAMES_VERSION = 1
RGB_PX = 256  # same side as the Fase 1.5 AI crop
MAX_RGB_CROPS = 3
UNKNOWN_DURATION_FRAMES = 3
SPARSE_STEP_S = 4.0  # beyond this spacing only keyframes are decoded (long videos)
EXTRACT_TIMEOUT_S = 600


@dataclass
class VideoFrameData:
    times: list[float]
    phashes: list[str]
    lumas: list[float]
    sharpness: list[float]
    motion: list[float]  # mean abs diff vs the previous frame; 0 for the first


def sample_plan(duration_s: float | None, fps: float = 1.0, max_frames: int = 120) -> list[float]:
    """Sample times in seconds: about `fps` per second, at most `max_frames`, evenly spread."""
    if not duration_s or duration_s <= 0:
        return [float(i) / fps for i in range(UNKNOWN_DURATION_FRAMES)]
    n = max(1, min(max_frames, int(duration_s * fps)))
    return [(i + 0.5) * duration_s / n for i in range(n)]


def _pick_crops(grays: list[np.ndarray], dark_luma: float) -> list[int]:
    n = len(grays)
    wanted = sorted({round(f * (n - 1)) for f in (0.1, 0.5, 0.9)})
    bright = [i for i, g in enumerate(grays) if g.mean() >= dark_luma]
    chosen: list[int] = []
    for i in wanted:
        if i in bright or not bright:
            pick = i
        else:
            pick = min((b for b in bright if b not in chosen), key=lambda b: abs(b - i), default=i)
        if pick not in chosen:
            chosen.append(pick)
    return chosen[:MAX_RGB_CROPS]


def extract(
    ffmpeg: str, path: Path, times: list[float], size: int = 64, dark_luma: float = 15.0,
    errors: list[str] | None = None,
) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Decode the frames at `times` (evenly spaced) in one ffmpeg run.

    Returns gray `size`x`size` frames and up to 3 RGB 256x256 center crops (spread, skipping
    dark frames when others exist). Fewer frames than times may come back when the video breaks
    midway: those are kept and the problem goes into `errors`; no frame at all raises.
    """
    cmd = [ffmpeg, "-nostdin", "-v", "error", "-ss", f"{times[0]:.3f}", "-i", str(path)]
    vf = f"crop='min(iw,ih)':'min(iw,ih)',scale={RGB_PX}:{RGB_PX}"
    if len(times) > 1:
        step = (times[-1] - times[0]) / (len(times) - 1)
        vf = f"fps=1/{step:.6f}," + vf
        if step > SPARSE_STEP_S:
            cmd[1:1] = ["-skip_frame", "nokey"]
    cmd += ["-vf", vf, "-frames:v", str(len(times)), "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
    err = ""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=EXTRACT_TIMEOUT_S, check=False,
            stdin=subprocess.DEVNULL, creationflags=NO_CONSOLE_FLAGS,
        )
        raw, err = proc.stdout, proc.stderr.decode("utf-8", "replace").strip()[:300]
    except subprocess.TimeoutExpired as e:
        raw, err = e.stdout or b"", "ffmpeg timed out"
    fb = RGB_PX * RGB_PX * 3
    count = len(raw) // fb
    if count == 0:
        raise RuntimeError(err or "no frames decoded")
    if (count < len(times) or err) and errors is not None:
        errors.append(f"partial video decode: {count}/{len(times)} frames: {err or 'ended early'}")
    rgb = np.frombuffer(raw, np.uint8, count * fb).reshape(count, RGB_PX, RGB_PX, 3)
    grays = [
        cv2.resize(cv2.cvtColor(f, cv2.COLOR_RGB2GRAY), (size, size), interpolation=cv2.INTER_AREA)
        for f in rgb
    ]
    return grays, [rgb[i].copy() for i in _pick_crops(grays, dark_luma)]


# --- storage -------------------------------------------------------------------------------


def save_video_frames(conn: sqlite3.Connection, file_id: str, data: VideoFrameData) -> None:
    conn.execute(
        "insert into video_frames (id, file_id, version, times, phashes, lumas, sharpness, motion,"
        " created_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " on conflict(file_id) do update set version=excluded.version, times=excluded.times,"
        " phashes=excluded.phashes, lumas=excluded.lumas, sharpness=excluded.sharpness,"
        " motion=excluded.motion, created_at=excluded.created_at",
        (
            new_id(), file_id, VIDEO_FRAMES_VERSION, json.dumps(data.times),
            json.dumps(data.phashes), json.dumps(data.lumas), json.dumps(data.sharpness),
            json.dumps(data.motion), datetime.now(timezone.utc).isoformat(),
        ),
    )


def load_video_frames(conn: sqlite3.Connection, file_ids: Iterable[str]) -> dict[str, VideoFrameData]:
    ids = list(file_ids)
    out: dict[str, VideoFrameData] = {}
    for i in range(0, len(ids), 500):  # stay under SQLite's variable limit
        chunk = ids[i : i + 500]
        rows = conn.execute(
            "select file_id, times, phashes, lumas, sharpness, motion from video_frames"
            f" where file_id in ({','.join('?' * len(chunk))})",
            chunk,
        )
        for r in rows:
            out[r["file_id"]] = VideoFrameData(
                json.loads(r["times"]), json.loads(r["phashes"]), json.loads(r["lumas"]),
                json.loads(r["sharpness"]), json.loads(r["motion"]),
            )
    return out
