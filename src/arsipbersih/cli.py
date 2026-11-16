"""Command-line interface: scan, review the plan, tidy, undo, report."""

import signal
import threading
import time
from collections.abc import Iterator
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Optional

import typer

from .calibrate import suggest_blur_threshold
from .config import load_settings, save_settings
from .db import open_db
from .events import EventSummary, add_override, run_events
from .layout import REVIEW_DIR, SOURCE_DIR
from .lock import RootLocked, acquire_root_lock
from .mover import (
    InsufficientSpace,
    NotUndoable,
    PendingMoves,
    UnknownBatch,
    reconcile_or_refuse,
    undo_all,
    undo_batch,
)
from . import pipeline
from .pipeline import Progress, ScanIncomplete, SCAN_DONE_KEY, rapikan
from .planner import compute_plans
from .report import build_report, verify
from .tools import check_tools

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Clean up and organize a personal photo and video collection.",
)

PAUSED = "Paused. Run the same command again to resume."
WINGET_HINTS = {
    "ExifTool": "winget install OliverBetz.ExifTool",
    "ffmpeg": "winget install Gyan.FFmpeg",
    "ffprobe": "winget install Gyan.FFmpeg",
}


LOCKED = (
    "Another ArsipBersih process is using this collection (scan/tidy/undo or serve). "
    "Close it first."
)


@contextmanager
def _root_locked(root: Path) -> Iterator[None]:
    """Hold the collection lock for a command that can move files or reconcile the journal."""
    try:
        lock = acquire_root_lock(root)
    except RootLocked:
        typer.echo(LOCKED, err=True)
        raise typer.Exit(1)
    try:
        yield
    finally:
        lock.release()


def _root_arg() -> Path:
    return typer.Argument(..., exists=True, file_okay=False, help="Collection root (contains Sumber/).")


def human_bytes(n: float) -> str:
    n = float(n)
    if n < 1024:
        return f"{n:.0f} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} GB"


def _fmt_eta(eta: Optional[float]) -> str:
    if eta is None:
        return ""
    m, s = divmod(int(eta), 60)
    h, m = divmod(m, 60)
    return f", ETA {h}h{m:02d}m" if h else f", ETA {m}m{s:02d}s"


def _echo_events(events: list[EventSummary]) -> None:
    if not events:
        typer.echo("No events found.")
        return
    typer.echo(f"{'Event':<32} {'Month':<9} {'Files':>6}  Sources | sample file")
    for e in events:
        typer.echo(
            f"{e.name:<32} {e.month or 'undated':<9} {e.file_count:>6}  "
            f"{', '.join(e.sources)} | {e.sample_path.replace(chr(92), '/').rsplit('/', 1)[-1]}"
        )


def _scan_done(conn) -> bool:
    return conn.execute("select 1 from settings where key = ?", (SCAN_DONE_KEY,)).fetchone() is not None


def _plan_summary(conn, root: Path) -> None:
    summary = pipeline.summarize_rapikan(conn, root)
    typer.echo(f"To Arsip: {summary.archive_units} units, {human_bytes(summary.archive_bytes)}")
    for cat, (units, size) in summary.review.items():
        typer.echo(f"To {REVIEW_DIR}/{cat}: {units} units, {human_bytes(size)}")
    typer.echo(f"Skipped units: {len(summary.skipped)}")
    for unit_id, reason in summary.skipped:
        typer.echo(f"  skipped {unit_id}: {reason}")
    if summary.space_ok:
        typer.echo("Free space: OK")
    else:
        typer.echo(f"Free space: NOT ENOUGH ({summary.space_error})")


@app.command("check-tools")
def check_tools_cmd() -> None:
    """Show ExifTool, ffmpeg and ffprobe status."""
    st = check_tools()
    missing = False
    for name, version in (("ExifTool", st.exiftool), ("ffmpeg", st.ffmpeg), ("ffprobe", st.ffprobe)):
        typer.echo(f"{name}: {version or 'missing'}")
        if not version:
            missing = True
            typer.echo(f"  install with: {WINGET_HINTS[name]}")
    if missing:
        typer.echo("Some tools are missing; results will be degraded.")


@app.command()
def scan(
    root: Path = _root_arg(),
    workers: Optional[int] = typer.Option(None, "--workers", min=1, help="Parallel workers."),
) -> None:
    """Scan the collection. Safe to interrupt and run again."""
    if pipeline.source_missing(root):
        typer.echo(
            f"No {SOURCE_DIR}/ folder in {root}. Copy each device into "
            f"{root / SOURCE_DIR}/<Device>/ and run scan again.",
            err=True,
        )
        raise typer.Exit(1)
    with _root_locked(root):
        _scan(root, workers)


def _scan(root: Path, workers: Optional[int]) -> None:
    if workers is not None:
        conn = open_db(root)
        try:
            save_settings(conn, replace(load_settings(conn), workers=workers))
        finally:
            conn.close()
    stop = threading.Event()
    last: dict = {}

    def show(p: Progress) -> None:
        now = time.monotonic()
        final = p.total > 0 and p.done >= p.total
        if not final and now - last.get(p.stage, -1.0) < 0.5:
            return
        last[p.stage] = now
        typer.echo(f"[{p.stage}] {p.done}/{p.total}{_fmt_eta(p.eta_s)}")

    previous = None
    try:
        # First Ctrl+C only asks the stages to stop after their current chunk.
        previous = signal.signal(signal.SIGINT, lambda *_: (stop.set(), signal.signal(signal.SIGINT, signal.default_int_handler)))
    except ValueError:
        pass  # not the main thread
    try:
        summary = pipeline.run_scan(root, show, stop)
    except (KeyboardInterrupt, BrokenProcessPool):
        stop.set()
        typer.echo(PAUSED)
        raise typer.Exit(0)
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
    if summary.warnings:
        typer.echo("Warnings:")
        for w in summary.warnings:
            typer.echo(f"  {w}")
    if summary.stopped:
        typer.echo(PAUSED)
        return
    typer.echo("Scan complete.")
    for cat, n in sorted(summary.categories.items()):
        typer.echo(f"  {cat}: {n}")
    ts = summary.tool_status
    typer.echo(f"ExifTool: {ts.exiftool or 'missing'}; ffmpeg: {ts.ffmpeg or 'missing'}; ffprobe: {ts.ffprobe or 'missing'}")
    if not ts.exiftool:
        typer.echo("Warning: ExifTool is missing; metadata dates will be incomplete.")
    if not (ts.ffmpeg and ts.ffprobe):
        typer.echo("Warning: ffmpeg/ffprobe is missing; video analysis is limited.")


def _parse_pairs(values: Optional[list[str]], flag: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for v in values or []:
        left, sep, right = v.partition("=")
        if not sep or not left.strip() or not right.strip():
            typer.echo(f"Error: {flag} expects NAME=NAME, got {v!r}.", err=True)
            raise typer.Exit(2)
        pairs.append((left.strip(), right.strip()))
    return pairs


@app.command()
def events(
    root: Path = _root_arg(),
    not_event: Optional[list[str]] = typer.Option(None, "--not-event", help="Folder name that is not an event (repeatable)."),
    rename: Optional[list[str]] = typer.Option(None, "--rename", help="Rename an event: OLD=NEW (repeatable)."),
    merge: Optional[list[str]] = typer.Option(None, "--merge", help="Merge an event into another: FROM=TO (repeatable)."),
) -> None:
    """Apply event overrides, then list events and recompute plans."""
    rename_pairs = _parse_pairs(rename, "--rename")
    merge_pairs = _parse_pairs(merge, "--merge")
    conn = open_db(root)
    try:
        for name in not_event or []:
            add_override(conn, "not_event", name)
        for old, new in rename_pairs:
            add_override(conn, "rename", old, new)
        for src, dst in merge_pairs:
            add_override(conn, "merge", src, dst)
        found = run_events(conn, load_settings(conn))
        compute_plans(conn)
        conn.commit()
        _echo_events(found)
    finally:
        conn.close()


@app.command()
def plan(root: Path = _root_arg()) -> None:
    """Dry run: show what tidy would move. Moves nothing."""
    conn = open_db(root)
    try:
        if not _scan_done(conn):
            typer.echo("Warning: The last scan did not finish. Run `scan` again before `tidy`.")
        _plan_summary(conn, root)
    finally:
        conn.close()


@app.command()
def tidy(
    root: Path = _root_arg(),
    yes: bool = typer.Option(False, "--yes", help="Do not ask for confirmation."),
) -> None:
    """Move everything from Sumber/ into Arsip/ and _review/."""
    with _root_locked(root):
        _tidy(root, yes)


def _tidy(root: Path, yes: bool) -> None:
    conn = open_db(root)
    try:
        if not _scan_done(conn):
            typer.echo("The last scan did not finish. Run `scan` again before `tidy`.")
            raise typer.Exit(1)
        _plan_summary(conn, root)
        typer.echo("")
        _echo_events(run_events(conn, load_settings(conn)))
    finally:
        conn.close()
    if not yes and not typer.confirm("Continue?", default=False):
        typer.echo("Cancelled. Nothing was moved.")
        return
    try:
        result = rapikan(root)
    except (ScanIncomplete, PendingMoves) as e:
        typer.echo(str(e))
        raise typer.Exit(1)
    except InsufficientSpace as e:
        typer.echo(f"Not enough free space: {e}")
        raise typer.Exit(1)
    if not result.moved_units and not result.failed_units and not result.skipped_units:
        typer.echo("Nothing to move.")
    typer.echo(f"Moved units: {result.moved_units}; failed units: {result.failed_units}")
    if result.skipped_units:
        typer.echo(f"Skipped units: {result.skipped_units} (nothing was moved for them)")
    for err in result.errors:
        typer.echo(f"  {err}")
    if result.batch_id:
        typer.echo(f"Batch: {result.batch_id}")
    if result.failed_units or result.skipped_units:
        raise typer.Exit(1)


@app.command()
def undo(
    root: Path = _root_arg(),
    batch: Optional[str] = typer.Option(None, "--batch", help="Undo one batch by ID."),
    all_: bool = typer.Option(False, "--all", help="Undo every batch."),
) -> None:
    """Undo a batch of moves, or all of them."""
    if bool(batch) == all_:
        typer.echo("Error: give exactly one of --batch ID or --all.", err=True)
        raise typer.Exit(2)
    with _root_locked(root):
        _undo(root, batch, all_)


def _undo(root: Path, batch: Optional[str], all_: bool) -> None:
    conn = open_db(root)
    journal_errors: list[str] = []
    try:
        try:
            reconcile_or_refuse(conn, root, journal_errors)
        except PendingMoves as e:
            typer.echo(str(e))
            raise typer.Exit(1)
        try:
            result = undo_all(conn, root) if all_ else undo_batch(conn, root, batch)
        except UnknownBatch:
            typer.echo(f"Unknown batch: {batch}")
            raise typer.Exit(1)
        except NotUndoable as e:
            typer.echo(f"Cannot undo: {e}")
            raise typer.Exit(1)
    finally:
        conn.close()
    typer.echo(f"Restored units: {result.moved_units}; failed units: {result.failed_units}")
    for err in [f"journal: {e}" for e in journal_errors] + result.errors:
        typer.echo(f"  {err}")
    if result.failed_units:
        raise typer.Exit(1)


@app.command()
def report(root: Path = _root_arg()) -> None:
    """Show collection totals and verify the database against the disk."""
    conn = open_db(root)
    try:
        r = build_report(conn, root)
        v = verify(conn, root)
    finally:
        conn.close()
    typer.echo(f"Total media: {r.total_media}")
    for loc, n in sorted(r.by_location.items()):
        typer.echo(f"  {loc}: {n}")
    typer.echo("By category:")
    for cat, n in sorted(r.by_category.items()):
        typer.echo(f"  {cat}: {n}")
    typer.echo(f"In _sampah: {human_bytes(r.bytes_in_trash)}")
    typer.echo(f"Saved by removing duplicates: {human_bytes(r.bytes_saved_duplicates)}")
    typer.echo(f"Non-media files left: {r.non_media_left}")
    typer.echo(f"Undated: {r.undated}")
    typer.echo(f"Cache size: {human_bytes(r.cache_bytes)}")
    typer.echo(f"Verify: {'OK' if v.ok else 'FAILED'} (database {v.db_count}, disk {v.disk_count})")
    for p in v.missing:
        typer.echo(f"  missing: {p}")
    for p in v.unexpected:
        typer.echo(f"  unexpected: {p}")
    if not v.ok:
        raise typer.Exit(1)


@app.command()
def calibrate(
    root: Path = _root_arg(),
    sample: int = typer.Option(200, "--sample", min=1, help="Number of photos to sample."),
    save: bool = typer.Option(False, "--save", help="Store the threshold in settings."),
) -> None:
    """Suggest a blur threshold from your own camera photos."""
    conn = open_db(root)
    try:
        value = suggest_blur_threshold(conn, sample=sample)
        if not value or value <= 0:
            typer.echo("Not enough camera photos with sharpness data to calibrate.")
            return
        typer.echo(f"Suggested blur threshold: {value:.2f}")
        if save:
            save_settings(conn, replace(load_settings(conn), blur_threshold=value))
            typer.echo("Saved as blur_threshold.")
        else:
            typer.echo("Run again with --save to store it.")
    finally:
        conn.close()


@app.command()
def serve(
    root: Path = _root_arg(),
    port: int = typer.Option(8765, help="Local port (127.0.0.1 only)."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open the browser."),
) -> None:
    """Start the local review UI."""
    import webbrowser

    import uvicorn

    from .web.app import create_app

    # Lock before create_app so its startup reconcile never races another process.
    try:
        lock = acquire_root_lock(root)
    except RootLocked:
        typer.echo(LOCKED, err=True)
        raise typer.Exit(1)
    web_app = None
    try:
        web_app = create_app(root, port=port, root_lock=lock)
        if not no_browser:
            threading.Timer(1.0, webbrowser.open, args=(f"http://127.0.0.1:{port}",)).start()
        uvicorn.run(web_app, host="127.0.0.1", port=port)
    finally:
        lock.release()  # no-op if a root switch already released it
        if web_app is not None and web_app.state.root_lock is not None:
            web_app.state.root_lock.release()  # the lock of the root switched to


if __name__ == "__main__":
    app()
