"""Thumbnails, previews and playable video for the web UI; all output lives in the cache folder."""

from __future__ import annotations

import os
import subprocess
import threading
import uuid
from pathlib import Path

from ..formats import Kind
from ..layout import long_path
from ..mover import _abs
from ..tools import NO_CONSOLE_FLAGS, find_tool
from ..visual import _load_rgb, _save_webp, analyze_video, preview_path, thumb_path

BROWSER_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "bmp"}
PLAYABLE_CONTAINERS = {"mp4", "m4v", "mov", "webm"}
PLAYABLE_CODECS = {"h264", "vp8", "vp9", "av1"}
TRANSCODE_TIMEOUT_S = 900

_UNAVAILABLE = "File tidak ditemukan (harddisk terlepas?)"
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_transcode_slot = threading.Semaphore(1)
# Video thumbnails need ffmpeg frame grabs; cap them so a scrolling grid on 8 GB RAM does
# not start dozens of ffmpeg processes at once.
_video_thumb_slots = threading.Semaphore(2)


class MediaUnavailable(Exception):
    """The file (or a playable rendition of it) cannot be served."""


def needs_transcode(ext: str, codec: str | None) -> bool:
    return not (ext.lower() in PLAYABLE_CONTAINERS and (codec or "").lower() in PLAYABLE_CODECS)


def video_cache_path(root: Path, file_id: str) -> Path:
    return Path(root) / ".arsipbersih" / "cache" / "video" / file_id[:2] / f"{file_id}.mp4"


def source_path(root: Path, file_row) -> Path:
    """Absolute path of the original, validated by mover._abs; raises MediaUnavailable if absent."""
    try:
        src = _abs(Path(root), file_row["rel_path"])
    except ValueError as exc:
        raise MediaUnavailable(_UNAVAILABLE) from exc
    if not os.path.isfile(long_path(src)):
        raise MediaUnavailable(_UNAVAILABLE)
    return src


def _kind(file_row) -> Kind:
    try:
        return Kind(file_row["kind"])
    except ValueError:
        return Kind.OTHER


def _lock_for(file_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(file_id, threading.Lock())


def _make_image_cache(root: Path, file_row, dst: Path, max_px: int) -> Path | None:
    if dst.exists():
        return dst
    src = source_path(root, file_row)
    kind = _kind(file_row)
    if kind not in (Kind.PHOTO, Kind.RAW):
        return None
    # Write to a temp sibling, then replace: dst.exists() must mean "complete".
    tmp = dst.with_name(f"{dst.stem}.{uuid.uuid4().hex}.tmp.webp")
    try:
        _save_webp(_load_rgb(src, kind, max_px), tmp, max_px)
        os.replace(tmp, dst)
    except Exception:
        return None
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return dst


def ensure_thumb(root, file_row, settings) -> Path | None:
    dst = thumb_path(root, file_row["id"])
    if dst.exists():
        return dst
    if _kind(file_row) == Kind.VIDEO:
        return _make_video_thumb(root, file_row, settings, dst)
    return _make_image_cache(root, file_row, dst, settings.thumb_px)


def _make_video_thumb(root, file_row, settings, dst: Path) -> Path | None:
    src = source_path(root, file_row)
    ffmpeg = find_tool("ffmpeg")
    if ffmpeg is None:
        return None
    with _lock_for(file_row["id"]):  # one regeneration per file, others wait and reuse it
        if dst.exists():
            return dst
        tmp = dst.with_name(f"{dst.stem}.{uuid.uuid4().hex}.tmp.webp")
        try:
            with _video_thumb_slots:
                analyze_video(src, file_row["duration_s"], settings, ffmpeg, tmp)  # result ignored
            if tmp.exists():
                os.replace(tmp, dst)  # dst.exists() must mean "complete"
        except Exception:
            return None
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    return dst if dst.exists() else None


def ensure_preview(root, file_row, settings) -> Path | None:
    return _make_image_cache(
        root, file_row, preview_path(root, file_row["id"]), settings.preview_px
    )


def _transcode(src: Path, dst: Path) -> None:
    ffmpeg = find_tool("ffmpeg")
    if ffmpeg is None:
        raise MediaUnavailable("Video tidak bisa diputar: ffmpeg tidak ditemukan")
    dst.parent.mkdir(parents=True, exist_ok=True)
    partial = dst.with_name(dst.stem + ".partial.mp4")
    cmd = [
        ffmpeg, "-nostdin", "-y", "-i", long_path(src), "-map", "0:v:0", "-map", "0:a?",
        # trunc(.../2)*2: libx264 with yuv420p needs even height (odd sources failed)
        "-vf", "scale=-2:'trunc(min(720,ih)/2)*2'", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "28", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(partial),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=TRANSCODE_TIMEOUT_S, creationflags=NO_CONSOLE_FLAGS
        )
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["ffmpeg gagal"]
            raise MediaUnavailable(f"Video tidak bisa diputar: {tail[0]}")
        os.replace(partial, dst)
    except MediaUnavailable:
        raise
    except Exception as exc:
        raise MediaUnavailable(f"Video tidak bisa diputar: {type(exc).__name__}: {exc}") from exc
    finally:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass


def ensure_playable_video(root, file_row) -> Path:
    src = source_path(root, file_row)
    if not needs_transcode(file_row["ext"], file_row["video_codec"]):
        return src
    dst = video_cache_path(root, file_row["id"])
    with _lock_for(file_row["id"]):
        if dst.exists():
            return dst
        with _transcode_slot:
            _transcode(src, dst)
    return dst
