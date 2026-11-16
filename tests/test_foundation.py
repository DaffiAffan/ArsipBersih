import uuid
from dataclasses import replace

from arsipbersih.config import load_settings, save_settings
from arsipbersih.db import new_id, open_db
from arsipbersih.layout import sanitize_component, to_abs, to_rel
from arsipbersih.tools import ToolStatus, check_tools


def test_open_db_creates_schema_and_wal(root):
    conn = open_db(root)
    tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert {"files","units","media_info","flags","groups","group_members","decisions",
            "batches","moves","event_overrides","settings"} <= tables
    assert conn.execute("pragma journal_mode").fetchone()[0] == "wal"
    assert (root / ".arsipbersih" / "arsip.db").exists()
    open_db(root)  # idempotent


def test_open_db_waits_for_a_busy_writer(root):
    import threading, time
    from arsipbersih.db import open_db
    # sqlite3 connections are thread-bound, so the writer connection is
    # created and committed inside its own thread.
    b = open_db(root)
    assert b.execute("pragma busy_timeout").fetchone()[0] == 30000
    locked = threading.Event()

    def hold_write_lock():
        a = open_db(root)
        a.execute("begin immediate")
        a.execute("insert into settings(key, value) values('x', '1')")
        locked.set()
        time.sleep(0.5)
        a.commit()
        a.close()

    writer = threading.Thread(target=hold_write_lock)
    writer.start()
    assert locked.wait(5)
    t0 = time.monotonic()
    with b:
        b.execute("insert into settings(key, value) values('y', '2')")  # waits, no "database is locked"
    assert time.monotonic() - t0 >= 0.4
    writer.join()


def test_new_id_is_uuid4():
    assert uuid.UUID(new_id()).version == 4


def test_settings_roundtrip_and_defaults(root):
    conn = open_db(root)
    s = load_settings(conn)
    assert (s.phash_version_max, s.phash_burst_max, s.burst_window_s) == (6, 12, 10.0)
    assert (s.short_video_s, s.live_photo_mov_max_s, s.timezone, s.workers) == (2.0, 4.0, "Asia/Jakarta", 3)
    assert (s.thumb_px, s.preview_px, s.analysis_px) == (320, 1600, 512)
    assert s.blur_threshold is None and "dcim" in {g.casefold() for g in s.generic_folders}
    save_settings(conn, replace(s, blur_threshold=31.5))
    assert load_settings(conn).blur_threshold == 31.5


def test_sanitize_component():
    assert sanitize_component('Rinjani: "Hari/1"?  ') == "Rinjani Hari 1"
    assert sanitize_component("...") == "_"


def test_check_tools_reports_missing(monkeypatch):
    monkeypatch.setattr("arsipbersih.tools.find_tool", lambda name: None)
    assert check_tools() == ToolStatus(None, None, None)


def test_rel_abs_roundtrip(root):
    p = root / "Arsip" / "2023" / "IMG 1.jpg"
    assert to_rel(root, p) == "Arsip/2023/IMG 1.jpg"
    assert to_abs(root, "Arsip/2023/IMG 1.jpg") == p


def test_tool_markers_skip_when_binary_missing(monkeypatch):
    import conftest

    class FakeItem:
        def __init__(self, marks):
            self.marks, self.added = marks, []

        def get_closest_marker(self, name):
            return name in self.marks or None

        def add_marker(self, mark):
            self.added.append(mark)

    monkeypatch.setattr(conftest, "_has_tool", lambda name: name != "ffmpeg")
    ffmpeg_item, exif_item, plain = FakeItem({"requires_ffmpeg"}), FakeItem({"requires_exiftool"}), FakeItem(set())
    conftest.pytest_collection_modifyitems(None, [ffmpeg_item, exif_item, plain])
    assert [m.name for m in ffmpeg_item.added] == ["skip"]
    assert exif_item.added == [] and plain.added == []


def test_open_db_does_not_wait_for_an_active_writer(root):
    import time

    a = open_db(root)
    a.execute("begin immediate")
    a.execute("insert into settings(key, value) values ('probe', '1')")
    t0 = time.monotonic()
    b = open_db(root)
    b.execute("select count(*) from settings").fetchone()
    assert time.monotonic() - t0 < 1.0
    b.close()
    a.rollback()
    a.close()
