from __future__ import annotations

import hashlib
import json
import os
import subprocess

import pytest
from conftest import requires_exiftool
from typer.testing import CliRunner

from arsipbersih.cli import app
from arsipbersih.config import Settings
from arsipbersih.dating import store
from arsipbersih.dating.xmp import render_xmp, write_dates
from arsipbersih.db import new_id, open_db
from arsipbersih.decisions import apply_category, mark_seen, record
from arsipbersih.grouping import load_unit_views
from arsipbersih.mover import execute
from arsipbersih.planner import NameReserver, _UNIT_SQL, _build_move
from arsipbersih.tools import find_tool

SETTINGS = Settings()


def _add_unit(conn, root, rel, *, location="archive", taken_at="2023-06-01T00:00:00",
              date_source="filename", category=None, data=b"jpegbytes"):
    uid, fid = new_id(), new_id()
    p = root / rel
    os.makedirs(p.parent, exist_ok=True)
    p.write_bytes(data)
    conn.execute(
        "insert into units(id, primary_file_id, location, taken_at, date_source, primary_category,"
        " planned_archive_dir) values(?,?,?,?,?,?,?)",
        (uid, fid, location, taken_at, date_source, category, "Arsip/2023/2023-06"),
    )
    conn.execute(
        "insert into files(id, unit_id, rel_path, orig_rel_path, size, kind, ext, source_label)"
        " values(?,?,?,?,?,?,?,?)",
        (fid, uid, rel, rel, len(data), "photo", rel.rsplit(".", 1)[-1].lower(), "HP-A"),
    )
    conn.commit()
    return uid, fid


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_render_xmp_contains_dates_with_offset():
    month = render_xmp("2024-07-01T12:00:00", "month", "Asia/Jakarta").decode("utf-8")
    for tag in ("exif:DateTimeOriginal", "xmp:CreateDate", "photoshop:DateCreated"):
        assert f'{tag}="2024-07-01T12:00:00+07:00"' in month
    assert 'xmp:CreatorTool="ArsipBersih"' in month
    # month precision: day 1 at noon, whatever day the stored value carries
    assert "2024-07-01T12:00:00+07:00" in render_xmp("2024-07-15T08:00:00", "month", "Asia/Jakarta").decode()
    # day precision: noon of that day, so no time zone shift can move it to another date
    day = render_xmp("2024-07-15T00:00:00", "day", "Asia/Jakarta").decode()
    assert 'exif:DateTimeOriginal="2024-07-15T12:00:00+07:00"' in day
    sec = render_xmp("2024-07-15T08:09:10", "second", "Europe/Amsterdam").decode()
    assert 'exif:DateTimeOriginal="2024-07-15T08:09:10+02:00"' in sec  # summer time


def test_write_dates_creates_sidecar_and_registers_it(root):
    conn = open_db(root)
    uid, fid = _add_unit(conn, root, "Arsip/2023/2023-06/IMG-20230601-WA0001.jpg")
    media = root / "Arsip/2023/2023-06/IMG-20230601-WA0001.jpg"
    before = (media.read_bytes(), os.stat(media).st_mtime_ns)

    res = write_dates(conn, root, SETTINGS)

    assert (res.written, res.rewritten, res.skipped_foreign, res.not_needed) == (1, 0, [], 0)
    xmp = root / "Arsip/2023/2023-06/IMG-20230601-WA0001.jpg.xmp"
    assert 'exif:DateTimeOriginal="2023-06-01T12:00:00+07:00"' in xmp.read_text("utf-8")
    assert (media.read_bytes(), os.stat(media).st_mtime_ns) == before
    assert not list(root.rglob("*.partial"))
    row = conn.execute(
        "select * from files where rel_path=?", ("Arsip/2023/2023-06/IMG-20230601-WA0001.jpg.xmp",)
    ).fetchone()
    assert (row["kind"], row["ext"], row["unit_id"], row["source_label"]) == ("sidecar", "xmp", uid, "HP-A")
    assert row["orig_rel_path"] is None
    assert row["size"] == xmp.stat().st_size and row["sha256"] == _sha(xmp)
    assert row["mtime_ns"] == os.stat(xmp).st_mtime_ns
    w = conn.execute("select * from xmp_writes").fetchone()
    assert (w["file_id"], w["xmp_rel_path"], w["sha256"]) == (row["id"], row["rel_path"], _sha(xmp))
    assert w["taken_at"] == "2023-06-01T00:00:00"

    # Unchanged date: nothing rewritten.
    again = write_dates(conn, root, SETTINGS)
    assert (again.written, again.rewritten, again.unchanged) == (0, 0, 1)


def test_exif_dated_unit_gets_no_xmp(root):
    conn = open_db(root)
    _add_unit(conn, root, "Arsip/2023/2023-06/IMG_1.jpg", taken_at="2023-06-01T10:00:00",
              date_source="exif")
    _add_unit(conn, root, "Arsip/2023/2023-06/VID_1.mp4", taken_at="2023-06-01T10:00:00",
              date_source="quicktime")
    _add_unit(conn, root, "Arsip/Tanpa-Tanggal/HP-A/DSC_1.jpg", taken_at=None, date_source=None)
    _add_unit(conn, root, "_review/burem/HP-A/IMG-20230601-WA0002.jpg", location="review")
    res = write_dates(conn, root, SETTINGS)
    assert (res.written, res.not_needed) == (0, 2)
    assert not list(root.rglob("*.xmp"))
    assert conn.execute("select count(*) from xmp_writes").fetchone()[0] == 0


@pytest.mark.parametrize("name", ["IMG-20230601-WA0001.jpg.xmp", "IMG-20230601-WA0001.xmp"])
def test_foreign_xmp_is_never_overwritten(root, name):
    conn = open_db(root)
    _add_unit(conn, root, "Arsip/2023/2023-06/IMG-20230601-WA0001.jpg")
    foreign = root / "Arsip/2023/2023-06" / name
    foreign.write_bytes(b"<lightroom/>")
    res = write_dates(conn, root, SETTINGS)
    assert res.written == 0
    assert res.skipped_foreign == ["Arsip/2023/2023-06/IMG-20230601-WA0001.jpg"]
    assert foreign.read_bytes() == b"<lightroom/>"
    assert sorted(p.name for p in foreign.parent.iterdir()) == sorted(
        ["IMG-20230601-WA0001.jpg", name]
    )
    assert conn.execute("select count(*) from xmp_writes").fetchone()[0] == 0


def test_own_xmp_rewritten_when_date_changes(root):
    conn = open_db(root)
    uid, _ = _add_unit(conn, root, "Arsip/2023/2023-06/IMG-20230601-WA0001.jpg")
    write_dates(conn, root, SETTINGS)
    xmp = root / "Arsip/2023/2023-06/IMG-20230601-WA0001.jpg.xmp"
    store.set_override(conn, uid, "2022-12-01T12:00:00", "month", "manual", None, "owner")

    res = write_dates(conn, root, SETTINGS, unit_ids=[uid])

    assert (res.written, res.rewritten, res.skipped_foreign) == (0, 1, [])
    assert "2022-12-01T12:00:00+07:00" in xmp.read_text("utf-8")
    rows = conn.execute("select * from xmp_writes").fetchall()
    assert len(rows) == 1 and rows[0]["sha256"] == _sha(xmp)
    assert rows[0]["taken_at"] == "2022-12-01T12:00:00"
    f = conn.execute("select sha256, size from files where id=?", (rows[0]["file_id"],)).fetchone()
    assert (f["sha256"], f["size"]) == (_sha(xmp), xmp.stat().st_size)
    assert conn.execute("select count(*) from files where ext='xmp'").fetchone()[0] == 1


def test_own_xmp_edited_by_someone_is_left_alone(root):
    conn = open_db(root)
    uid, _ = _add_unit(conn, root, "Arsip/2023/2023-06/IMG-20230601-WA0001.jpg")
    write_dates(conn, root, SETTINGS)
    xmp = root / "Arsip/2023/2023-06/IMG-20230601-WA0001.jpg.xmp"
    xmp.write_bytes(xmp.read_bytes() + b"<!-- edited -->")
    edited = xmp.read_bytes()
    store.set_override(conn, uid, "2022-12-01T12:00:00", "month", "manual", None, "owner")

    res = write_dates(conn, root, SETTINGS)

    assert res.rewritten == 0 and res.skipped_foreign == ["Arsip/2023/2023-06/IMG-20230601-WA0001.jpg"]
    assert xmp.read_bytes() == edited


def test_written_xmp_moves_with_its_unit(root):
    conn = open_db(root)
    uid, _ = _add_unit(conn, root, "Arsip/2023/2023-06/IMG-20230601-WA0001.jpg")
    # The new folder already holds the name: the moved pair gets the same suffix.
    (root / "Arsip/2023/2023-07").mkdir(parents=True)
    (root / "Arsip/2023/2023-07/IMG-20230601-WA0001.jpg").write_bytes(b"other")
    write_dates(conn, root, SETTINGS)

    u = conn.execute(_UNIT_SQL + " where u.id=?", (uid,)).fetchone()
    um = _build_move(conn, NameReserver(root, conn), u, "Arsip/2023/2023-07", "archive")
    assert len(um.files) == 2
    result = execute(conn, root, [um], "rapikan", "test")
    assert result.moved_units == 1 and not result.errors

    moved = root / "Arsip/2023/2023-07/IMG-20230601-WA0001__HP-A.jpg.xmp"
    assert moved.is_file()
    assert not (root / "Arsip/2023/2023-06/IMG-20230601-WA0001.jpg.xmp").exists()
    assert conn.execute("select rel_path from files f join xmp_writes w on w.file_id=f.id").fetchone()[0] == (
        "Arsip/2023/2023-07/IMG-20230601-WA0001__HP-A.jpg.xmp"
    )
    # Still recognised as ours at its new place.
    again = write_dates(conn, root, SETTINGS)
    assert (again.written, again.unchanged, again.skipped_foreign) == (0, 1, [])


def test_apply_keep_writes_xmp_for_units_moved_to_archive(root):
    conn = open_db(root)
    uid, _ = _add_unit(conn, root, "_review/burem/HP-A/IMG-20230601-WA0001.jpg",
                       location="review", category="burem")
    conn.execute("insert into flags(id, unit_id, category) values(?,?,?)", (new_id(), uid, "burem"))
    conn.commit()
    mark_seen(conn, [uid])
    record(conn, uid, "keep")

    result = apply_category(conn, root, "burem")

    assert result.moved_units == 1
    assert (root / "Arsip/2023/2023-06/IMG-20230601-WA0001.jpg.xmp").is_file()


def test_write_dates_failure_does_not_fail_apply(root, monkeypatch):
    from arsipbersih.dating import xmp as xmp_mod

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(xmp_mod, "write_dates", boom)
    conn = open_db(root)
    uid, _ = _add_unit(conn, root, "_review/burem/HP-A/IMG-20230601-WA0001.jpg",
                       location="review", category="burem")
    conn.execute("insert into flags(id, unit_id, category) values(?,?,?)", (new_id(), uid, "burem"))
    conn.commit()
    mark_seen(conn, [uid])
    record(conn, uid, "keep")
    result = apply_category(conn, root, "burem")
    assert result.moved_units == 1
    assert any("disk full" in e for e in result.errors)


def test_completeness_ignores_our_xmp(root):
    conn = open_db(root)
    uid, _ = _add_unit(conn, root, "Arsip/2023/2023-06/IMG-20230601-WA0001.jpg")
    write_dates(conn, root, SETTINGS)
    view = load_unit_views(conn, [uid])[0]
    assert view.file_count == 1 and view.unit_bytes == len(b"jpegbytes")


def test_write_dates_cli(root):
    conn = open_db(root)
    _add_unit(conn, root, "Arsip/2023/2023-06/IMG-20230601-WA0001.jpg")
    _add_unit(conn, root, "Arsip/2023/2023-06/IMG_1.jpg", taken_at="2023-06-01T10:00:00",
              date_source="exif")
    conn.close()
    res = CliRunner().invoke(app, ["write-dates", str(root)])
    assert res.exit_code == 0, res.stdout
    assert "Ditulis: 1" in res.stdout and "Tidak perlu (EXIF): 1" in res.stdout
    assert (root / "Arsip/2023/2023-06/IMG-20230601-WA0001.jpg.xmp").is_file()


@requires_exiftool
def test_exiftool_reads_our_xmp(root, make_image):
    conn = open_db(root)
    rel = "Arsip/2023/2023-06/IMG-20230601-WA0001.jpg"
    _add_unit(conn, root, rel)
    make_image(root / rel)  # a real JPEG without EXIF
    conn.execute("update files set size=? where rel_path=?", ((root / rel).stat().st_size, rel))
    conn.commit()
    write_dates(conn, root, SETTINGS)
    out = subprocess.run(
        [find_tool("exiftool"), "-j", "-G1", "-DateTimeOriginal", "-CreateDate", "-DateCreated",
         str(root / (rel + ".xmp"))],
        capture_output=True, check=True,
    )
    tags = json.loads(out.stdout)[0]
    assert tags["XMP-exif:DateTimeOriginal"] == "2023:06:01 12:00:00+07:00"
    assert tags["XMP-xmp:CreateDate"] == "2023:06:01 12:00:00+07:00"
    assert tags["XMP-photoshop:DateCreated"] == "2023:06:01 12:00:00+07:00"


def test_rapikan_writes_xmp_for_units_moved_to_archive(root):
    from arsipbersih.pipeline import SCAN_DONE_KEY, rapikan
    from arsipbersih.planner import compute_plans

    conn = open_db(root)
    _add_unit(conn, root, "Sumber/HP-A/WhatsApp/IMG-20230601-WA0001.jpg", location="source")
    _add_unit(conn, root, "Sumber/HP-A/DCIM/IMG_1.jpg", location="source",
              taken_at="2023-06-01T10:00:00", date_source="exif")
    conn.execute("insert into settings(key, value) values(?, ?)", (SCAN_DONE_KEY, '"x"'))
    conn.commit()
    compute_plans(conn)
    conn.close()

    result = rapikan(root)

    assert result.moved_units == 2 and result.failed_units == 0
    assert sorted(p.name for p in (root / "Arsip/2023/2023-06").iterdir()) == [
        "IMG-20230601-WA0001.jpg", "IMG-20230601-WA0001.jpg.xmp", "IMG_1.jpg",
    ]
