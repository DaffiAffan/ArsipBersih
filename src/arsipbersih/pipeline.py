"""Scan orchestration (resumable) and the tidy ("rapikan") step."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .classify import run_classify
from .config import load_settings
from .db import open_db
from .events import EventSummary, run_events
from .grouping import run_grouping
from .hashing import run_hashing
from .inventory import InventoryResult, run_inventory
from .layout import SOURCE_DIR
from .metadata import run_metadata
from .mover import BatchResult, check_space, execute, reconcile, remove_empty_dirs
from .pairing import build_units, confirm_live_photos
from .planner import compute_plans, plan_rapikan
from .tools import ToolStatus, check_tools
from .visual import run_visual

STAGES = ("inventory", "pairing", "hash", "metadata", "visual", "grouping", "classify", "events", "plan")


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
    conn = open_db(root)
    try:
        settings = load_settings(conn)
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

        reconcile(conn, root)  # settle any interrupted batch before looking at the disk

        begin("inventory")
        summary.inventory = run_inventory(conn, root)
        if stopped():
            return summary

        begin("pairing")
        build_units(conn)
        if stopped():
            return summary

        run_hashing(conn, root, settings, begin("hash"), stop)
        if stopped():
            return summary

        run_metadata(conn, root, settings, begin("metadata"), stop)
        if stopped():
            return summary
        confirm_live_photos(conn, settings)

        run_visual(conn, root, settings, begin("visual"), stop)
        if stopped():
            return summary

        begin("grouping")
        run_grouping(conn, settings)
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

        begin("plan")
        compute_plans(conn)
        conn.commit()
        return summary
    finally:
        conn.close()


def rapikan(root: Path) -> BatchResult:
    """Move every source unit to Arsip/ or _review/ as one journaled batch."""
    root = Path(root)
    conn = open_db(root)
    try:
        skipped: list[tuple[str, str]] = []
        moves = plan_rapikan(conn, root, skipped)
        notes = [f"skipped {unit_id}: {reason}" for unit_id, reason in skipped]
        if not moves:
            return BatchResult(batch_id="", moved_units=0, failed_units=0, errors=notes)
        check_space(root, moves)
        result = execute(conn, root, moves, "rapikan", label=f"rapikan {date.today().isoformat()}")
        remove_empty_dirs(root / SOURCE_DIR)
        result.errors.extend(notes)
        return result
    finally:
        conn.close()
