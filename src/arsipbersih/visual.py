"""Visual analysis: pHash, sharpness, luma stats, video frames, cached thumbnails/previews."""

from __future__ import annotations

import io
import json
import os
import signal
import sqlite3
import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pillow_heif
from PIL import Image, ImageOps

from .ai.model import crop_rgb
from .config import Settings
from .formats import Kind
from .layout import long_path
from .tools import NO_CONSOLE_FLAGS, find_tool

pillow_heif.register_heif_opener()

# Pillow refuses to open anything above ~179 MP (DecompressionBombError), before a JPEG can be
# drafted down. Its check is switched off; _load_rgb applies MAX_DECODE_PIXELS itself to the
# size that would actually be decoded.
Image.MAX_IMAGE_PIXELS = None
MAX_DECODE_PIXELS = 2 * 89_478_485  # Pillow's own error limit

CHUNK_SIZE = 100
WEBP_QUALITY = 80
VIDEO_FRAME_FRACTIONS = (0.1, 0.5, 0.9)
FALLBACK_FRAME_TIMES = (0.5, 1.0, 1.5)
FFMPEG_TIMEOUT_S = 60
_PREVIEW_EXTS = {"heic", "heif", "tif", "tiff"}
_SECOND_PASS_EXTS = {"heic", "heif"}


@dataclass
class VisualResult:
    decode_ok: bool | None  # None: unknown (RAW without a preview, or a retryable failure)
    phash: str | None = None
    sharpness: float | None = None
    luma_mean: float | None = None
    luma_std: float | None = None
    clipping: float | None = None
    frame_phashes: list[str] | None = None
    frame_lumas: list[float] | None = None
    error: str | None = None
    retry: bool = False  # a timeout or I/O error, not the file: visual_status stays NULL
    ai_rgb: bytes | None = None  # 256x256x3 uint8 crop for the AI stage, only when asked for


class NoRawThumbnail(ValueError):
    """The RAW has no usable embedded preview; that says nothing about the RAW data."""


class TooLargeToDecode(ValueError):
    """Too many pixels to decode safely on this machine; that says nothing about the file."""


# --- hashing and scoring -------------------------------------------------------------------


def phash64(gray: np.ndarray) -> int:
    small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    block = cv2.dct(small)[:8, :8].flatten()
    med = np.median(block[1:])  # exclude DC term
    bits = block > med  # 64 booleans, row-major
    return sum(1 << i for i, b in enumerate(bits) if b)


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def phash_hex(v: int) -> str:
    return f"{v:016x}"


def phash_int(s: str) -> int:
    return int(s, 16)


def sharpness_score(gray: np.ndarray) -> float:
    v = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    return 100.0 * v / (v + 200.0)


def luma_stats(gray: np.ndarray) -> tuple[float, float, float]:
    clipped = float(np.count_nonzero((gray <= 5) | (gray >= 250))) / gray.size
    return float(gray.mean()), float(gray.std()), clipped


# --- decoding ------------------------------------------------------------------------------


def _open_raw_thumb(path: Path) -> Image.Image:
    import rawpy

    with open(long_path(path), "rb") as f:
        with rawpy.imread(f) as raw:
            try:
                thumb = raw.extract_thumb()
            except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError) as e:
                raise NoRawThumbnail(f"RAW has no usable embedded thumbnail: {e}") from e
    if thumb.format == rawpy.ThumbFormat.JPEG:
        img = Image.open(io.BytesIO(thumb.data))
        img.load()
        return img
    if thumb.format == rawpy.ThumbFormat.BITMAP:
        return Image.fromarray(thumb.data)
    raise NoRawThumbnail("RAW embedded thumbnail has an unsupported format")


def _load_rgb(path: Path, kind: Kind, max_px: int) -> Image.Image:
    """Decode to an upright RGB image, cheaply reduced toward `max_px` where the format allows."""
    if kind == Kind.RAW:
        img = _open_raw_thumb(path)
    else:
        img = Image.open(long_path(path))
        if img.format == "JPEG":
            img.draft("RGB", (max_px, max_px))
        w, h = img.size
        if w * h > MAX_DECODE_PIXELS:
            img.close()
            raise TooLargeToDecode(f"{w}x{h} is too large to analyse")
        img.load()
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img


def _to_gray_small(img: Image.Image, max_px: int) -> np.ndarray:
    small = img.copy()
    small.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
    return np.asarray(small.convert("L"))


def load_gray_small(path: Path, kind: Kind, max_px: int) -> np.ndarray:
    return _to_gray_small(_load_rgb(Path(path), kind, max_px), max_px)


def _save_webp(img: Image.Image, dst: Path, max_px: int) -> None:
    out = img.copy()
    out.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
    os.makedirs(long_path(Path(dst).parent), exist_ok=True)
    out.save(long_path(Path(dst)), format="WEBP", quality=WEBP_QUALITY)


# --- photos --------------------------------------------------------------------------------


def _error_text(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"[:500]


def _is_transient(e: BaseException) -> bool:
    """A timeout or an OS-level I/O error (it carries an errno): the file may be fine.

    Pillow reports undecodable data as a plain OSError without errno (UnidentifiedImageError,
    "image file is truncated"); those stay decode failures.
    """
    return isinstance(e, subprocess.TimeoutExpired) or (
        isinstance(e, OSError) and e.errno is not None
    )


def _failure(e: BaseException) -> VisualResult:
    if isinstance(e, TooLargeToDecode):
        return VisualResult(decode_ok=None, error=_error_text(e))  # unknown, not rusak
    if isinstance(e, MemoryError) or _is_transient(e):  # may pass with fewer workers busy
        return VisualResult(decode_ok=None, error=_error_text(e), retry=True)
    return VisualResult(decode_ok=False, error=_error_text(e))


def analyze_photo(
    path: Path,
    kind: Kind,
    settings: Settings,
    thumb_dst: Path,
    preview_dst: Path | None,
    want_ai: bool = False,
) -> VisualResult:
    try:
        big = max(settings.analysis_px, settings.thumb_px)
        if preview_dst is not None:
            big = max(big, settings.preview_px)
        img = _load_rgb(Path(path), kind, big)
        gray = _to_gray_small(img, settings.analysis_px)
        mean, std, clip = luma_stats(gray)
        result = VisualResult(
            decode_ok=True,
            phash=phash_hex(phash64(gray)),
            sharpness=sharpness_score(gray),
            luma_mean=mean,
            luma_std=std,
            clipping=clip,
        )
        _save_webp(img, thumb_dst, settings.thumb_px)
        if preview_dst is not None:
            _save_webp(img, preview_dst, settings.preview_px)
        if want_ai:  # crop from the image already decoded here: the AI stage never re-reads it
            try:
                result.ai_rgb = crop_rgb(img).tobytes()
            except Exception:  # noqa: BLE001 - a crop problem never marks a good photo rusak
                result.ai_rgb = None  # the AI backfill retries it and records any error
        return result
    except NoRawThumbnail as e:
        # Unknown, not corrupt: no thumbnail, and no rusak flag.
        return VisualResult(decode_ok=None, error=_error_text(e))
    except Exception as e:  # a bad file must never abort the run
        return _failure(e)


# --- videos --------------------------------------------------------------------------------


def _run_quiet(cmd: list[str], timeout: float) -> bytes:
    proc = subprocess.run(
        cmd, capture_output=True, timeout=timeout, check=False,
        stdin=subprocess.DEVNULL, creationflags=NO_CONSOLE_FLAGS,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip()[:300] or "ffmpeg failed")
    return proc.stdout


def _probe_duration(path: Path) -> float | None:
    ffprobe = find_tool("ffprobe")
    if ffprobe is None:
        return None
    try:
        out = _run_quiet(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            FFMPEG_TIMEOUT_S,
        )
        value = float(out.decode().strip().splitlines()[0])
    except (RuntimeError, ValueError, IndexError, OSError, subprocess.SubprocessError):
        return None
    return value if value > 0 else None


def _grab_frame(ffmpeg: str, path: Path, t: float) -> bytes:
    data = _run_quiet(
        [ffmpeg, "-nostdin", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path),
         "-frames:v", "1", "-vf", "scale=512:-2", "-f", "image2pipe", "-vcodec", "png", "-"],
        FFMPEG_TIMEOUT_S,
    )
    if not data:
        raise RuntimeError(f"no frame at {t:.3f}s")
    return data


def analyze_video(
    path: Path, duration_s: float | None, settings: Settings, ffmpeg: str, thumb_dst: Path
) -> VisualResult:
    try:
        path = Path(path)
        if not duration_s or duration_s <= 0:
            duration_s = _probe_duration(path)
        if duration_s:
            times = [duration_s * f for f in VIDEO_FRAME_FRACTIONS]
        else:
            times = list(FALLBACK_FRAME_TIMES)
        frames: list[bytes] = []
        for t in times:
            try:
                frames.append(_grab_frame(ffmpeg, path, t))
            except RuntimeError:
                frames.append(_grab_frame(ffmpeg, path, 0.0))  # seek past the end: use the start
        grays = []
        for data in frames:
            g = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
            if g is None:
                raise ValueError("undecodable video frame")
            grays.append(g)
        mid = grays[1]
        mean, std, clip = luma_stats(mid)
        with Image.open(io.BytesIO(frames[1])) as im:
            im.load()
            _save_webp(im.convert("RGB"), thumb_dst, settings.thumb_px)
        return VisualResult(
            decode_ok=True,
            phash=phash_hex(phash64(mid)),
            sharpness=sharpness_score(mid),
            luma_mean=mean,
            luma_std=std,
            clipping=clip,
            frame_phashes=[phash_hex(phash64(g)) for g in grays],
            frame_lumas=[float(g.mean()) for g in grays],
        )
    except Exception as e:
        return _failure(e)


# --- cache paths ---------------------------------------------------------------------------


def thumb_path(root: Path, file_id: str) -> Path:
    return Path(root) / ".arsipbersih" / "cache" / "thumb" / file_id[:2] / f"{file_id}.webp"


def preview_path(root: Path, file_id: str) -> Path:
    return Path(root) / ".arsipbersih" / "cache" / "preview" / file_id[:2] / f"{file_id}.webp"


# --- batch runner --------------------------------------------------------------------------


def _ignore_sigint() -> None:
    """Pool initializer: Ctrl+C pauses the scan through its stop event, not by killing workers."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _pool(workers: int) -> ProcessPoolExecutor:
    return ProcessPoolExecutor(max_workers=workers, initializer=_ignore_sigint)


def _rerun_isolated(jobs: list[tuple]) -> dict[str, VisualResult]:
    """Run jobs one at a time in a single-worker pool; a job that breaks it is the culprit."""
    out: dict[str, VisualResult] = {}
    pool = _pool(1)
    try:
        for job in jobs:
            try:
                _, out[job[0]] = pool.submit(_worker, job).result()
            except BrokenProcessPool:
                out[job[0]] = VisualResult(decode_ok=False, error="worker crashed while decoding")
                pool.shutdown(wait=False, cancel_futures=True)
                pool = _pool(1)
            except Exception as e:
                out[job[0]] = VisualResult(decode_ok=False, error=_error_text(e))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return out


def _worker(job: tuple) -> tuple[str, VisualResult]:
    file_id, path, kind, thumb_dst, preview_dst, duration_s, ffmpeg, settings, want_ai = job
    # Test hook: the env var names a file whose analysis kills the child process.
    crash_on = os.environ.get("ARSIPBERSIH_TEST_CRASH_ON")
    if crash_on and Path(path).name == crash_on:
        os._exit(1)
    kind = Kind(kind)
    if kind == Kind.VIDEO:
        return file_id, analyze_video(Path(path), duration_s, settings, ffmpeg, Path(thumb_dst))
    return file_id, analyze_photo(
        Path(path), kind, settings, Path(thumb_dst), Path(preview_dst) if preview_dst else None,
        want_ai,
    )


_UPSERT = (
    "insert into media_info (file_id, phash, frame_phashes, sharpness, luma_mean, luma_std,"
    " clipping, frame_lumas, decode_ok) values (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    " on conflict(file_id) do update set phash=excluded.phash,"
    " frame_phashes=excluded.frame_phashes, sharpness=excluded.sharpness,"
    " luma_mean=excluded.luma_mean, luma_std=excluded.luma_std, clipping=excluded.clipping,"
    " frame_lumas=excluded.frame_lumas, decode_ok=excluded.decode_ok"
)


def _store(conn: sqlite3.Connection, file_id: str, r: VisualResult) -> None:
    conn.execute(
        _UPSERT,
        (
            file_id, r.phash,
            json.dumps(r.frame_phashes) if r.frame_phashes is not None else None,
            r.sharpness, r.luma_mean, r.luma_std, r.clipping,
            json.dumps(r.frame_lumas) if r.frame_lumas is not None else None,
            None if r.decode_ok is None else int(r.decode_ok),
        ),
    )
    if r.retry:
        status = None  # read it again on the next scan
    else:
        status = "error" if r.decode_ok is False else "done"
    conn.execute(
        "update files set visual_status=?, error=? where id=?",
        (status, None if r.decode_ok else r.error, file_id),
    )


def run_visual(
    conn: sqlite3.Connection,
    root: Path,
    settings: Settings,
    progress: Callable[[int, int], None] | None = None,
    stop: threading.Event | None = None,
    ai_sink: Callable[[str, bytes], None] | None = None,
) -> int:
    """Analyse every file with visual_status NULL. Returns the number of files processed.

    With ``ai_sink``, the primary photo/RAW file of each unit also yields a 256x256 crop from
    the same decode; ``ai_sink(file_id, rgb_bytes)`` receives it in this process, per chunk.
    """
    root = Path(root)
    ffmpeg = find_tool("ffmpeg")
    rows = conn.execute(
        "select f.id, f.rel_path, f.kind, f.ext, m.duration_s,"
        " coalesce(u.primary_file_id = f.id and u.location != 'trash', 0) as is_primary"
        " from files f left join media_info m on m.file_id = f.id"
        " left join units u on u.id = f.unit_id"
        " where f.visual_status is null and f.kind in (?, ?, ?)"
        " and f.missing = 0 and f.purged = 0 order by f.rel_path",
        (Kind.PHOTO.value, Kind.RAW.value, Kind.VIDEO.value),
    ).fetchall()

    fast: list[tuple] = []
    heavy: list[tuple] = []  # HEIC/HEIF/RAW: memory hungry, fewer workers
    for r in rows:
        kind = Kind(r["kind"])
        if kind == Kind.VIDEO and ffmpeg is None:
            continue
        fid = r["id"]
        wants_preview = kind == Kind.RAW or r["ext"] in _PREVIEW_EXTS
        job = (
            fid, str(root / r["rel_path"]), kind.value, str(thumb_path(root, fid)),
            str(preview_path(root, fid)) if wants_preview else None,
            r["duration_s"], ffmpeg, settings,
            ai_sink is not None and kind != Kind.VIDEO and bool(r["is_primary"]),
        )
        (heavy if kind == Kind.RAW or r["ext"] in _SECOND_PASS_EXTS else fast).append(job)

    total = len(fast) + len(heavy)
    done = 0
    if progress:
        progress(0, total)

    for jobs, workers in (
        (fast, max(1, settings.workers)),
        (heavy, max(1, settings.workers // 2)),
    ):
        if not jobs:
            continue
        pool = _pool(workers)
        try:
            for start in range(0, len(jobs), CHUNK_SIZE):
                if stop is not None and stop.is_set():
                    return done
                chunk = jobs[start : start + CHUNK_SIZE]
                futures = []
                for job in chunk:
                    try:
                        futures.append(pool.submit(_worker, job))
                    except BrokenProcessPool:
                        break  # a worker died while the chunk was queued: rest goes isolated
                results: dict[str, VisualResult] = {}
                unfinished: list[tuple] = list(chunk[len(futures):])
                for job, fut in zip(chunk, futures):
                    fid = job[0]
                    try:
                        _, results[fid] = fut.result()
                    except BrokenProcessPool:
                        unfinished.append(job)
                    except Exception as e:
                        results[fid] = VisualResult(decode_ok=False, error=_error_text(e))
                if unfinished:
                    # A worker died natively: drop this pool once and re-run the jobs that did
                    # not return one by one, so only the real culprit is marked as an error.
                    pool.shutdown(wait=False, cancel_futures=True)
                    results.update(_rerun_isolated(unfinished))
                    pool = _pool(workers)
                for job in chunk:
                    _store(conn, job[0], results[job[0]])
                    done += 1
                conn.commit()
                if ai_sink is not None:
                    for job in chunk:
                        r = results[job[0]]
                        if r.ai_rgb is not None:
                            ai_sink(job[0], r.ai_rgb)
                            r.ai_rgb = None
                if progress:
                    progress(done, total)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
    return done
