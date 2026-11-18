"""Scan stage ``video``: sample frames for videos the visual stage analysed before frame data existed.

New videos get their frames (and AI crops) in the visual stage's single decode; this only
backfills older ones, resumably: a video is done once it has a ``video_frames`` row.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path

from . import visual
from .config import Settings
from .formats import Kind
from .tools import find_tool
from .videoframes import VIDEO_FRAMES_VERSION, save_video_frames

VIDEO_WORKERS = 2  # RAM: each worker holds one ffmpeg decode
ERROR_PREFIX = "video: "  # marks a failed backfill so the next scan does not retry it forever


@dataclass
class VideoBackfillResult:
    done: int
    errors: int


def _worker(job: tuple) -> tuple[str, visual.VisualResult]:
    file_id, path, duration_s, ffmpeg, settings, thumb_dst, want_ai = job
    return file_id, visual.analyze_video(Path(path), duration_s, settings, ffmpeg, Path(thumb_dst),
                                         want_ai)


def _rerun_isolated(jobs: list[tuple]) -> dict[str, visual.VisualResult]:
    """One job at a time in a fresh pool: only the video that crashes alone is the culprit."""
    out: dict[str, visual.VisualResult] = {}
    for job in jobs:
        pool = visual._pool(1)
        try:
            out[job[0]] = pool.submit(_worker, job).result()[1]
        except BrokenProcessPool:
            out[job[0]] = visual.VisualResult(decode_ok=False, error="worker crashed while decoding")
        except Exception as e:
            out[job[0]] = visual.VisualResult(decode_ok=False, error=visual._error_text(e))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    return out


def run_video_backfill(
    conn: sqlite3.Connection,
    root: Path,
    settings: Settings,
    progress: Callable[[int, int], None] | None = None,
    stop: threading.Event | None = None,
    ai_sink: Callable[[str, list[bytes]], None] | None = None,
) -> VideoBackfillResult:
    root = Path(root)
    result = VideoBackfillResult(0, 0)
    ffmpeg = find_tool("ffmpeg")
    if ffmpeg is None:
        return result
    rows = conn.execute(
        "select f.id, f.rel_path, m.duration_s from units u join files f on f.id = u.primary_file_id"
        " left join media_info m on m.file_id = f.id"
        " where u.location != 'trash' and f.kind = ? and f.missing = 0 and f.purged = 0"
        " and f.visual_status = 'done' and coalesce(f.error, '') not like ?"
        " and not exists (select 1 from video_frames v where v.file_id = f.id and v.version >= ?)"
        " order by f.rel_path",
        (Kind.VIDEO.value, ERROR_PREFIX + "%", VIDEO_FRAMES_VERSION),
    ).fetchall()
    total = len(rows)
    if progress:
        progress(0, total)
    if not rows:
        return result
    pool = visual._pool(VIDEO_WORKERS)
    try:
        for start in range(0, total, VIDEO_WORKERS):
            if stop is not None and stop.is_set():
                break
            chunk = rows[start : start + VIDEO_WORKERS]
            jobs = [
                (r["id"], str(root / r["rel_path"]), r["duration_s"], ffmpeg, settings,
                 str(visual.thumb_path(root, r["id"])), ai_sink is not None)
                for r in chunk
            ]
            results: dict[str, visual.VisualResult] = {}
            unfinished: list[tuple] = []
            for job in jobs:
                try:
                    results[job[0]] = pool.submit(_worker, job).result()[1]
                except BrokenProcessPool:  # may be a neighbour's crash: find the culprit below
                    unfinished.append(job)
                except Exception as e:
                    results[job[0]] = visual.VisualResult(decode_ok=False, error=visual._error_text(e))
            if unfinished:
                pool.shutdown(wait=False, cancel_futures=True)
                results.update(_rerun_isolated(unfinished))
                pool = visual._pool(VIDEO_WORKERS)
            for job in jobs:
                fid = job[0]
                r = results[fid]
                if r.video_frames is None:
                    result.errors += 1
                    if not r.retry:
                        conn.execute("update files set error = ? where id = ?",
                                     ((ERROR_PREFIX + (r.error or "decode failed"))[:300], fid))
                else:
                    save_video_frames(conn, fid, r.video_frames)
                    if r.error:
                        conn.execute("update files set error = ? where id = ?", (r.error, fid))
                    if ai_sink is not None and r.ai_rgb_list:
                        ai_sink(fid, r.ai_rgb_list)
                conn.commit()
                result.done += 1
            if progress:
                progress(result.done, total)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return result
