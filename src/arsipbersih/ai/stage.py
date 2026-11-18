"""Scan stage ``ai``: image embeddings for photo/RAW units, then labels from the active head.

Photos the visual stage analyses in this scan hand over a 256x256 crop from their own decode
(``AiCollector`` as the visual ``ai_sink``), so they are never decoded twice. Units whose visual
analysis finished in an earlier scan are backfilled here with a small decode. One onnxruntime
session in the main process embeds everything in batches; at most one batch of crops (plus the
results in flight) is held in memory.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .. import visual
from ..classify import _is_screen_aspect, download_reason
from ..config import Settings
from ..formats import Kind
from ..grouping import UnitView, load_unit_views
from ..pathrules import segments_of
from ..tools import find_tool
from ..videoframes import extract, sample_plan
from .heads import UnitFacts, get_head
from .model import INPUT_PX, MODEL_NAME, VisionEncoder, crop_rgb, model_status
from .store import (
    load_embeddings,
    record_error,
    save_embeddings,
    save_labels,
    units_missing_embedding,
)

NOT_READY = "Model AI belum disiapkan — jalankan: arsipbersih setup-models"
BACKFILL_WORKERS = 2
BACKFILL_DRAFT_PX = 2 * INPUT_PX  # decode at most ~512 px: enough for a 256 px crop
LABEL_CHUNK = 2048
_CROP_SHAPE = (INPUT_PX, INPUT_PX, 3)
_CROP_BYTES = INPUT_PX * INPUT_PX * 3


@dataclass
class AiResult:
    embedded: int
    errors: int
    skipped_reason: str | None
    labeled: int = 0
    warnings: list[str] = field(default_factory=list)


def _error_text(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"[:300]


def load_encoder(settings: Settings, encoder_factory=None) -> tuple[object | None, str | None]:
    """(encoder, None) when the model loads; (None, warning) when it is missing or broken."""
    status = model_status()
    if not status.ready:
        return None, NOT_READY
    factory = encoder_factory or VisionEncoder
    try:
        return factory(status.path, threads=settings.ai_threads), None
    except Exception as e:  # a corrupt or incompatible model file must not abort the scan
        return None, f"Model AI gagal dimuat ({_error_text(e)}) — jalankan: arsipbersih setup-models"


def _stack(crops: list[bytes]) -> np.ndarray:
    return np.frombuffer(b"".join(crops), dtype=np.uint8).reshape(len(crops), *_CROP_SHAPE)


def embed_groups(encoder, groups: list[tuple[str, list[bytes]]]) -> list[tuple[str, np.ndarray]]:
    """One embedding per unit: a video's crops are embedded together and averaged (re-normalized)."""
    flat = _stack([c for _, crops in groups for c in crops])
    vecs = np.asarray(encoder.embed(flat), dtype=np.float32)
    out, i = [], 0
    for unit_id, crops in groups:
        v = vecs[i : i + len(crops)].mean(axis=0)
        i += len(crops)
        out.append((unit_id, v / max(float(np.linalg.norm(v)), 1e-12)))
    return out


class AiCollector:
    """Visual-stage sink: maps a file to its unit (primary files only) and embeds in batches."""

    def __init__(self, encoder, conn: sqlite3.Connection, model: str = MODEL_NAME,
                 batch: int = 16) -> None:
        self._encoder = encoder
        self._conn = conn
        self._model = model
        self._batch = max(1, batch)
        self._pending: list[tuple[str, list[bytes]]] = []
        self._crops = 0
        self.embedded = 0
        self.error: str | None = None  # set when embedding failed; the collector then stops

    def add(self, file_id: str, rgb: bytes | list[bytes]) -> None:
        self.add_many(file_id, [rgb] if isinstance(rgb, bytes) else rgb)

    def add_many(self, file_id: str, rgb_list: list[bytes]) -> None:
        crops = [c for c in rgb_list if len(c) == _CROP_BYTES]
        if self.error is not None or not crops:
            return
        row = self._conn.execute(
            "select u.id from files f join units u on u.id = f.unit_id"
            " where f.id = ? and u.primary_file_id = f.id and u.location != 'trash'",
            (file_id,),
        ).fetchone()
        if row is None:  # a companion file (RAW of a RAW+JPG pair, Live Photo video, ...)
            return
        self._pending.append((row[0], crops))
        self._crops += len(crops)
        if self._crops >= self._batch:
            self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        pending, self._pending, self._crops = self._pending, [], 0
        try:
            rows = embed_groups(self._encoder, pending)
        except Exception as e:  # the backfill retries these units on the ai stage
            self.error = _error_text(e)
            return
        save_embeddings(self._conn, self._model, rows)
        self.embedded += len(pending)


# --- backfill ------------------------------------------------------------------------------


def _video_crops(path: str) -> list[bytes]:
    ffmpeg = find_tool("ffmpeg")
    if ffmpeg is None:
        raise MemoryError("ffmpeg not found")  # treated as transient: retried once it is installed
    duration = visual._probe_duration(Path(path))
    times = [duration * f for f in (0.1, 0.5, 0.9)] if duration else sample_plan(None)
    return [c.tobytes() for c in extract(ffmpeg, Path(path), times)[1]]


def _backfill_worker(job: tuple[str, str, str]) -> tuple[str, list[bytes] | None, str | None, bool]:
    """(unit_id, crops, error, retry later). Runs in a worker process."""
    unit_id, path, kind = job
    try:
        if Kind(kind) == Kind.VIDEO:
            return unit_id, _video_crops(path), None, False
        img = visual._load_rgb(Path(path), Kind(kind), BACKFILL_DRAFT_PX)
        return unit_id, [crop_rgb(img).tobytes()], None, False
    except Exception as e:
        transient = isinstance(e, MemoryError) or visual._is_transient(e)
        return unit_id, None, _error_text(e), transient


def _decode_batch(pool_box: list, jobs: list[tuple]) -> list[tuple]:
    """Decode a batch; a native worker crash re-runs the unfinished jobs one at a time."""
    out: dict[str, tuple] = {}
    unfinished: list[tuple] = []
    futures = []
    for job in jobs:
        try:
            futures.append((job, pool_box[0].submit(_backfill_worker, job)))
        except BrokenProcessPool:
            unfinished.append(job)
    for job, fut in futures:
        try:
            out[job[0]] = fut.result()
        except BrokenProcessPool:
            unfinished.append(job)
        except Exception as e:
            out[job[0]] = (job[0], None, _error_text(e), False)
    if unfinished:
        pool_box[0].shutdown(wait=False, cancel_futures=True)
        for job in unfinished:
            single = visual._pool(1)
            try:
                out[job[0]] = single.submit(_backfill_worker, job).result()
            except BrokenProcessPool:
                out[job[0]] = (job[0], None, "worker crashed while decoding", False)
            except Exception as e:
                out[job[0]] = (job[0], None, _error_text(e), False)
            finally:
                single.shutdown(wait=True, cancel_futures=True)
        pool_box[0] = visual._pool(BACKFILL_WORKERS)
    return [out[job[0]] for job in jobs]


def _backfill_jobs(conn: sqlite3.Connection, root: Path, unit_ids: list[str]) -> list[tuple]:
    jobs = []
    for i in range(0, len(unit_ids), 500):
        part = unit_ids[i : i + 500]
        marks = ",".join("?" * len(part))
        rows = conn.execute(
            "select u.id, f.rel_path, f.kind from units u join files f on f.id = u.primary_file_id"
            f" where u.id in ({marks})",
            part,
        ).fetchall()
        jobs += [(r[0], str(Path(root) / r[1]), r[2]) for r in rows]
    jobs.sort(key=lambda j: j[0])
    return jobs


# --- labels --------------------------------------------------------------------------------


def unit_facts(u: UnitView) -> UnitFacts:
    """Rule hints for the head, from the same notions the category rules use."""
    ext = u.filename.rsplit(".", 1)[-1].lower() if "." in u.filename else ""
    return UnitFacts(
        has_camera=bool(u.make or u.model),
        is_png=ext == "png",
        screen_ratio=_is_screen_aspect(u.width, u.height),
        download_like=download_reason(u, segments_of(u.orig_rel_path), ext) is not None,
    )


def relabel_all(
    conn: sqlite3.Connection,
    root: Path,
    settings: Settings,
    head_name: str | None = None,
    *,
    unit_ids: list[str] | None = None,
) -> int:
    """Predict labels with a head for every unit (or ``unit_ids``) that has an embedding."""
    ids, mat = load_embeddings(conn, MODEL_NAME, unit_ids)
    if not ids:
        return 0
    views = {v.unit_id: v for v in load_unit_views(conn, ids)}  # skips trash / missing files
    keep = [i for i, u in enumerate(ids) if u in views]
    if not keep:
        return 0
    head = get_head(head_name or settings.ai_head, Path(root))
    count = 0
    for start in range(0, len(keep), LABEL_CHUNK):
        rows = keep[start : start + LABEL_CHUNK]
        unit_chunk = [ids[i] for i in rows]
        preds = head.predict(mat[rows], [unit_facts(views[u]) for u in unit_chunk])
        save_labels(
            conn, head.name, head.version,
            [(u, g, s, p) for u, (g, s, p) in zip(unit_chunk, preds)], model=MODEL_NAME,
        )
        count += len(unit_chunk)
    return count


# --- the stage -----------------------------------------------------------------------------


def run_ai(
    conn: sqlite3.Connection,
    root: Path,
    settings: Settings,
    progress: Callable[[int, int], None] | None = None,
    stop: threading.Event | None = None,
    encoder_factory=VisionEncoder,
) -> AiResult:
    """Backfill missing embeddings, then label every embedded unit. Pausable between batches."""
    root = Path(root)
    encoder, reason = load_encoder(settings, encoder_factory)
    if encoder is None:
        return AiResult(0, 0, reason)
    result = AiResult(0, 0, None)
    jobs = _backfill_jobs(conn, root, units_missing_embedding(conn, MODEL_NAME))
    total, done = len(jobs), 0
    batch = max(1, settings.ai_batch)
    if progress:
        progress(0, total)
    if jobs:
        pool_box = [visual._pool(BACKFILL_WORKERS)]
        try:
            for start in range(0, total, batch):
                if stop is not None and stop.is_set():
                    return result
                decoded = _decode_batch(pool_box, jobs[start : start + batch])
                ok = [(u, crop) for u, crop, _, _ in decoded if crop is not None]
                for unit_id, crop, error, transient in decoded:
                    if crop is None:
                        result.errors += 1
                        if not transient:  # a timeout or I/O error is retried next scan
                            record_error(conn, unit_id, MODEL_NAME, error or "decode failed")
                if ok:
                    try:
                        rows = embed_groups(encoder, ok)
                    except Exception as e:
                        result.warnings.append(f"Model AI gagal menghitung embedding ({_error_text(e)})")
                        return result
                    save_embeddings(conn, MODEL_NAME, rows)
                    result.embedded += len(ok)
                done += len(decoded)
                if progress:
                    progress(done, total)
        finally:
            pool_box[0].shutdown(wait=True, cancel_futures=True)
    if stop is not None and stop.is_set():
        return result
    try:
        result.labeled = relabel_all(conn, root, settings)
    except Exception as e:  # e.g. a broken text model: embeddings stay, labels come next scan
        result.warnings.append(f"Label AI gagal dibuat ({_error_text(e)})")
    return result
