from __future__ import annotations

import threading

from conftest import requires_exiftool

from arsipbersih.calibrate import suggest_blur_threshold
from arsipbersih.db import open_db
from arsipbersih.pipeline import rapikan, run_scan
from arsipbersih.report import build_report, verify


@requires_exiftool
def test_full_scan_rapikan_verify(root, mini_collection):
    s = run_scan(root)
    assert not s.stopped and s.categories["rusak"] == 1 and s.categories["duplikat-persis"] == 1
    r = rapikan(root)
    assert r.failed_units == 0
    conn = open_db(root)
    assert verify(conn, root).ok
    assert (root / "Arsip/2023/2023-08 Rinjani").is_dir()
    assert any((root / "Arsip/Tanpa-Tanggal").rglob("DSC_*"))


@requires_exiftool
def test_second_scan_after_rapikan_is_stable(root, mini_collection):
    run_scan(root)
    rapikan(root)
    conn = open_db(root)
    before = build_report(conn, root)
    run_scan(root)
    r2 = rapikan(root)
    assert r2.moved_units == 0
    after = build_report(open_db(root), root)
    assert (after.total_media, after.by_location) == (before.total_media, before.by_location)
    (root / "Sumber/x.jpg.partial").write_bytes(b"\xff\xd8\xff")  # mover temp files are not inventoried
    assert run_scan(root).inventory.added == 0


def test_stop_then_resume(root, mini_collection):
    stop = threading.Event()

    def cb(p):
        if p.stage == "visual":
            stop.set()

    assert run_scan(root, progress=cb, stop=stop).stopped
    assert not run_scan(root).stopped


def test_scan_survives_corrupt_files(root, mini_collection):
    (root / "Sumber/HP-A/DCIM/zero.mp4").write_bytes(b"")
    s = run_scan(root)
    assert not s.stopped and s.categories.get("rusak", 0) >= 2


def test_suggest_blur_threshold(conn_with):
    conn = conn_with([dict(name=f"IMG_{i}.jpg", make="x", sharp=float(i)) for i in range(1, 101)])
    assert 4.0 <= suggest_blur_threshold(conn, sample=100) <= 7.0
