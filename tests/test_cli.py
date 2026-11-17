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
def test_root_with_trailing_dot_is_normalised(root, mini_collection):
    # "D:\TESTING." opens the DB fine, but \\?\ long paths keep the dot and miss every file.
    runner.invoke(app, ["scan", str(root)])
    assert runner.invoke(app, ["report", str(root) + "."]).exit_code == 0


@requires_exiftool
def test_tidy_refuses_after_stopped_scan(root, mini_collection):
    ev = threading.Event()
    run_scan(root, progress=lambda p: ev.set() if p.stage == "visual" else None, stop=ev)
    before = _files(root)
    res = runner.invoke(app, ["tidy", str(root), "--yes"])
    assert res.exit_code == 1
    assert before == _files(root) and not (root / "Arsip").exists()


@requires_exiftool
def test_events_rename_and_bad_format(root, make_image):
    make_image(root / "Sumber/Laptop/Users/Bali/IMG_20230812_101010.jpg")
    runner.invoke(app, ["scan", str(root)])
    res = runner.invoke(app, ["events", str(root), "--rename", "Users=Bali Trip"])
    assert res.exit_code == 0 and "Bali Trip" in res.stdout
    assert runner.invoke(app, ["events", str(root), "--merge", "nope"]).exit_code == 2


def test_scan_pauses_on_broken_pool(root, monkeypatch):
    from concurrent.futures.process import BrokenProcessPool

    from arsipbersih import pipeline

    def boom(*a, **k):
        raise BrokenProcessPool("child killed")

    monkeypatch.setattr(pipeline, "run_scan", boom)
    res = runner.invoke(app, ["scan", str(root)])
    assert res.exit_code == 0 and "Run the same command again to resume." in res.stdout
    assert "child killed" in res.stdout
    log = root / ".arsipbersih" / "logs" / "scan-crash.log"
    assert "BrokenProcessPool: child killed" in log.read_text(encoding="utf-8")


def test_calibrate_save_without_data_does_not_save(root):
    from arsipbersih.config import load_settings
    from arsipbersih.db import open_db

    res = runner.invoke(app, ["calibrate", str(root), "--save"])
    assert res.exit_code == 0 and "Not enough camera photos" in res.stdout
    conn = open_db(root)
    assert load_settings(conn).blur_threshold is None
    conn.close()


def test_scan_refuses_without_sumber(tmp_path):
    res = runner.invoke(app, ["scan", str(tmp_path)])
    assert res.exit_code == 1
    assert "Sumber" in res.output and "Scan complete" not in res.output
    assert not (tmp_path / ".arsipbersih").exists()


def test_run_scan_raises_without_sumber(tmp_path):
    import pytest

    from arsipbersih.pipeline import SourceMissing

    with pytest.raises(SourceMissing):
        run_scan(tmp_path)
