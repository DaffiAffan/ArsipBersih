from __future__ import annotations

import threading

from conftest import requires_exiftool
from typer.testing import CliRunner

from arsipbersih.cli import app
from arsipbersih.pipeline import run_scan

runner = CliRunner()


def _files(root):
    return sorted(
        p.relative_to(root) for p in root.rglob("*") if p.is_file() and ".arsipbersih" not in p.parts
    )


@requires_exiftool
def test_plan_is_dry_run(root, mini_collection):
    runner.invoke(app, ["scan", str(root)])
    before = _files(root)
    res = runner.invoke(app, ["plan", str(root)])
    assert res.exit_code == 0 and "_review" in res.stdout
    assert before == _files(root)


@requires_exiftool
def test_tidy_asks_for_confirmation(root, mini_collection):
    runner.invoke(app, ["scan", str(root)])
    res = runner.invoke(app, ["tidy", str(root)], input="n\n")
    assert res.exit_code == 0 and not (root / "Arsip").exists()


@requires_exiftool
def test_events_not_event_override(root, make_image):
    make_image(root / "Sumber/Laptop/Users/Bali/IMG_20230812_101010.jpg")
    runner.invoke(app, ["scan", str(root)])
    res = runner.invoke(app, ["events", str(root), "--not-event", "Users"])
    assert "Bali" in res.stdout and "Users" not in res.stdout


@requires_exiftool
def test_report_exit_code_reflects_verify(root, mini_collection):
    runner.invoke(app, ["scan", str(root)])
    assert runner.invoke(app, ["report", str(root)]).exit_code == 0


@requires_exiftool
def test_tidy_refuses_after_stopped_scan(root, mini_collection):
    ev = threading.Event()
    run_scan(root, progress=lambda p: ev.set() if p.stage == "visual" else None, stop=ev)
    before = _files(root)
    res = runner.invoke(app, ["tidy", str(root), "--yes"])
    assert res.exit_code == 1
    assert before == _files(root) and not (root / "Arsip").exists()
