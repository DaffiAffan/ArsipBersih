"""Scan orchestration (resumable) and the tidy ("rapikan") step."""

from __future__ import annotations

import json
import os
import posixpath
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .ai import stage as ai_stage
from .classify import run_classify
from .config import load_settings
from .dating.infer import DatesResult, run_dates
from .db import open_db
from .events import EventSummary, run_events
from .grouping import run_grouping
from .hashing import run_hashing
from .inventory import InventoryResult, run_inventory
from .layout import REVIEW_DIR, SOURCE_DIR, long_path, to_abs
from .metadata import ExifToolUnavailable, run_metadata
from .mover import (
    BatchResult,
    check_space,
    InsufficientSpace,
    execute,
    reconcile,
    reconcile_or_refuse,
    remove_empty_dirs,
    remove_empty_parents,
)
from .pairing import build_units, confirm_live_photos
from .planner import compute_plans, plan_rapikan
from .tools import ToolStatus, check_tools
from .visual import run_visual

STAGES = (
    "inventory", "pairing", "hash", "metadata", "visual", "ai",
    "grouping", "classify", "events", "dates", "plan",
)


SCAN_DONE_KEY = "last_scan_completed_at"
MAX_WARNED_PATHS = 20


class ScanIncomplete(Exception):
    """Raised when tidy is attempted after a scan that did not finish."""


class SourceMissing(Exception):
    """Raised when the collection root has no Sumber/ folder to scan."""


def source_missing(root: Path) -> bool:
    return not (Path(root) / SOURCE_DIR).is_dir()


@dataclass
class Progress:
    stage: str
    done: int
    total: int
    eta_s: float | None


@dataclass
class ScanSummary:
    inventory: InventoryResult
    categories: dict[str, int]
    events: list[EventSummary]
    stopped: bool
    tool_status: ToolStatus
    warnings: list[str] = field(default_factory=list)
    dates: DatesResult | None = None


def _inventory_warnings(inv: InventoryResult) -> list[str]:
    out: list[str] = []
    if inv.errors:
        out.append(f"{inv.errors} paths could not be read")
        out.extend(f"unreadable: {p}" for p in inv.error_paths[:MAX_WARNED_PATHS])
    if inv.missing:
        out.append(f"{inv.missing} files are gone since the last scan")
        out.extend(f"gone: {p}" for p in inv.missing_paths[:MAX_WARNED_PATHS])
    if inv.skipped_dirs:
        out.append(f"{inv.skipped_dirs} cache folders skipped")
    if inv.non_media:
        out.append(f"{inv.non_media} non-media files left in place")
    return out


def _reporter(stage: str, progress: Callable[[Progress], None] | None):
    """Return a (done, total) callback for a stage; ETA = elapsed / done * remaining."""
    start = time.monotonic()

    def cb(done: int, total: int) -> None:
        if progress is None:
            return
        eta = None
        if done > 0 and total >= done:
            eta = (time.monotonic() - start) / done * (total - done)
        progress(Progress(stage, done, total, eta))

    return cb


def run_scan(
    root: Path,
    progress: Callable[[Progress], None] | None = None,
    stop: threading.Event | None = None,
) -> ScanSummary:
    root = Path(root)
    if source_missing(root):
        # Scanning an empty walk would report "complete" with 0 files.
        raise SourceMissing(str(root / SOURCE_DIR))
    conn = open_db(root)
    try:
        settings = load_settings(conn)
        with conn:
            conn.execute("delete from settings where key = ?", (SCAN_DONE_KEY,))
        summary = ScanSummary(InventoryResult(), {}, [], False, check_tools())

        def begin(stage: str):
            if progress is not None:
                progress(Progress(stage, 0, 0, None))
            return _reporter(stage, progress)

        def stopped() -> bool:
            if stop is not None and stop.is_set():
                summary.stopped = True
                return True
            return False

        journal_errors: list[str] = []
        reconcile(conn, root, journal_errors)  # settle any interrupted batch first
        summary.warnings.extend(f"journal: {e}" for e in journal_errors)

        begin("inventory")
        summary.inventory = run_inventory(conn, root)
        summary.warnings.extend(_inventory_warnings(summary.inventory))
        if stopped():
            return summary

        begin("pairing")
        build_units(conn)
        if stopped():
            return summary

        run_hashing(conn, root, settings, begin("hash"), stop)
        if stopped():
            return summary

        try:
            run_metadata(conn, root, settings, begin("metadata"), stop)
        except ExifToolUnavailable as e:
            # Unread files stay NULL; pausing keeps tidy from running on missing dates.
            summary.warnings.append(f"ExifTool stopped working ({e}); run scan again")
            summary.stopped = True
            return summary
        if stopped():
            return summary
        confirm_live_photos(conn, settings)

        # One encoder for the whole scan; None when the model is missing or will not load.
        encoder, ai_warning = ai_stage.load_encoder(settings)
        collector = (
            ai_stage.AiCollector(encoder, conn, ai_stage.MODEL_NAME, batch=settings.ai_batch)
            if encoder is not None else None
        )
        try:
            run_visual(conn, root, settings, begin("visual"), stop,
                       ai_sink=collector.add if collector is not None else None)
        finally:
            if collector is not None:
                collector.flush()  # also on pause: crops already decoded are not lost
        if collector is not None and collector.error:
            summary.warnings.append(f"Model AI gagal menghitung embedding ({collector.error})")
        if stopped():
            return summary

        ai_progress = begin("ai")
        if encoder is None:
            summary.warnings.append(ai_warning)  # the scan still finishes without AI labels
        else:
            ai = ai_stage.run_ai(conn, root, settings, ai_progress, stop,
                                 encoder_factory=lambda *a, **k: encoder)
            summary.warnings.extend(ai.warnings)
        if stopped():
            return summary

        begin("grouping")
        run_grouping(conn, settings, root)
        if stopped():
            return summary

        begin("classify")
        summary.categories = run_classify(conn, settings)
        if stopped():
            return summary

        begin("events")
        summary.events = run_events(conn, settings)
        if stopped():
            return summary

        begin("dates")
        summary.dates = run_dates(conn, settings)
        summary.warnings.extend(summary.dates.clock_warnings)
        if stopped():
            return summary

        begin("plan")
        compute_plans(conn)
        with conn:
            conn.execute(
                "insert into settings(key, value) values(?, ?)"
                " on conflict(key) do update set value = excluded.value",
                (SCAN_DONE_KEY, json.dumps(datetime.now().isoformat(timespec="seconds"))),
            )
        return summary
    finally:
        conn.close()


def rapikan(root: Path) -> BatchResult:
    """Move every source unit to Arsip/ or _review/ as one journaled batch."""
    root = Path(root)
    conn = open_db(root)
    try:
        journal_errors: list[str] = []
        reconcile_or_refuse(conn, root, journal_errors)  # raises PendingMoves
        if conn.execute("select 1 from settings where key = ?", (SCAN_DONE_KEY,)).fetchone() is None:
            raise ScanIncomplete("The last scan did not finish. Run `scan` again before `tidy`.")
        skipped: list[tuple[str, str]] = []
        moves = plan_rapikan(conn, root, skipped)
        notes = [f"journal: {e}" for e in journal_errors]
        notes += [f"skipped {unit_id}: {reason}" for unit_id, reason in skipped]
        if not moves:
            return BatchResult("", 0, 0, notes, skipped_units=len(skipped))
        check_space(root, moves)
        result = execute(conn, root, moves, "rapikan", label=f"rapikan {date.today().isoformat()}")
        remove_empty_dirs(root / SOURCE_DIR)
        # Folders that redated moves left behind (Arsip/Tanpa-Tanggal/<label>, an old month).
        for d in sorted({posixpath.dirname(fm.src_rel) for um in moves if um.reason for fm in um.files}):
            remove_empty_parents(root, d)
        result.errors.extend(notes)
        result.skipped_units = len(skipped)
        return result
    finally:
        conn.close()


@dataclass
class RapikanSummary:
    archive_units: int
    archive_bytes: int
    review: dict[str, tuple[int, int]]  # category -> (units, bytes)
    skipped: list[tuple[str, str]]  # (unit_id, reason)
    space_ok: bool
    space_error: str | None
    scan_done: bool
    # Archive units moved for another reason (e.g. a corrected date): reason -> (units, bytes).
    # Not counted in archive_units, which are the units that newly enter Arsip.
    by_reason: dict[str, tuple[int, int]] = field(default_factory=dict)


def summarize_rapikan(conn, root: Path) -> RapikanSummary:
    """Dry-run summary of what `rapikan` would do; moves nothing."""
    skipped: list[tuple[str, str]] = []
    moves = plan_rapikan(conn, root, skipped)
    archive_units = archive_bytes = 0
    review_units: dict[str, int] = {}
    review_bytes: dict[str, int] = {}
    by_reason: dict[str, list[int]] = {}
    for um in moves:
        size = 0
        for fm in um.files:
            try:
                size += os.stat(long_path(to_abs(root, fm.src_rel))).st_size
            except OSError:
                pass
        if um.reason:
            acc = by_reason.setdefault(um.reason, [0, 0])
            acc[0] += 1
            acc[1] += size
        elif um.to_location == "archive":
            archive_units += 1
            archive_bytes += size
        else:
            parts = um.files[0].dst_rel.replace("\\", "/").split("/") if um.files else []
            cat = parts[1] if len(parts) > 1 and parts[0] == REVIEW_DIR else "other"
            review_units[cat] = review_units.get(cat, 0) + 1
            review_bytes[cat] = review_bytes.get(cat, 0) + size
    space_error = None
    try:
        check_space(root, moves)
    except InsufficientSpace as e:
        space_error = str(e)
    scan_done = conn.execute("select 1 from settings where key = ?", (SCAN_DONE_KEY,)).fetchone() is not None
    return RapikanSummary(
        archive_units,
        archive_bytes,
        {c: (review_units[c], review_bytes[c]) for c in sorted(review_units)},
        skipped,
        space_error is None,
        space_error,
        scan_done,
        {r: (n, b) for r, (n, b) in by_reason.items()},
    )
