from conftest import requires_exiftool, requires_ffmpeg

from arsipbersih.config import Settings
from arsipbersih.db import open_db
from arsipbersih.inventory import run_inventory
from arsipbersih.metadata import ExifTool, ExifToolDied, probe_video, run_metadata
from arsipbersih.pairing import build_units
from arsipbersih.tools import find_tool


@requires_exiftool
def test_exiftool_reads_unicode_paths_and_dates(root, make_image):
    p = make_image(root / "Sumber/HP/Bali 🏝️/IMG_1.jpg", exif_datetime="2023:08:12 14:30:22", make="samsung", model="SM-A515F")
    with ExifTool(find_tool("exiftool")) as et:
        [d] = et.read([p])
    assert d["ExifIFD:DateTimeOriginal"] == "2023:08:12 14:30:22" and d["IFD0:Make"] == "samsung"


@requires_ffmpeg
def test_probe_video(root, make_video):
    v = make_video(root / "Sumber/HP/VID_1.mp4", seconds=3.0)
    info = probe_video(v, find_tool("ffprobe"))
    assert abs(info["duration_s"] - 3.0) < 0.2 and info["width"] == 640


@requires_exiftool
def test_run_metadata_sets_taken_at_and_survives_bad_file(root, make_image):
    make_image(root / "Sumber/HP/DCIM/DSC_1.JPG", exif_datetime="2023:08:12 14:30:22")
    (root / "Sumber/HP/DCIM/IMG_20220101_101010.jpg").write_bytes(b"\xff\xd8\xff garbage")
    conn = open_db(root); run_inventory(conn, root); build_units(conn)
    run_metadata(conn, root, Settings())
    got = dict(conn.execute("select f.rel_path, u.taken_at from units u join files f on f.id=u.primary_file_id").fetchall())
    assert got["Sumber/HP/DCIM/DSC_1.JPG"] == "2023-08-12T14:30:22"
    assert got["Sumber/HP/DCIM/IMG_20220101_101010.jpg"] == "2022-01-01T10:10:10"


@requires_exiftool
@requires_ffmpeg
def test_ffprobe_failure_marks_video_error(root, make_video, monkeypatch):
    make_video(root / "Sumber/HP/VID_1.mp4", seconds=1.0)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("arsipbersih.metadata.probe_video", boom)
    conn = open_db(root); run_inventory(conn, root); build_units(conn)
    run_metadata(conn, root, Settings())
    status, error = conn.execute("select meta_status, error from files where kind='video'").fetchone()
    assert status == "error" and error.startswith("ffprobe:")


@requires_ffmpeg
def test_ffprobe_failure_without_exiftool_stays_retryable(root, make_video, monkeypatch):
    make_video(root / "Sumber/HP/VID_1.mp4", seconds=1.0)

    def boom(*a, **k):
        raise RuntimeError("boom")

    real_find = find_tool
    monkeypatch.setattr("arsipbersih.metadata.probe_video", boom)
    monkeypatch.setattr("arsipbersih.metadata.find_tool", lambda n: None if n == "exiftool" else real_find(n))
    conn = open_db(root); run_inventory(conn, root); build_units(conn)
    run_metadata(conn, root, Settings())
    status, error = conn.execute("select meta_status, error from files where kind='video'").fetchone()
    assert status is None and error.startswith("ffprobe:")


@requires_exiftool
def test_exiftool_dying_mid_batch_is_retried_not_error(root, make_image, monkeypatch):
    make_image(root / "Sumber/HP/DCIM/DSC_1.JPG", exif_datetime="2023:08:12 14:30:22")
    make_image(root / "Sumber/HP/DCIM/DSC_2.JPG", seed=1, exif_datetime="2023:08:13 09:00:00")
    real_read, calls = ExifTool.read, []

    def dies_once(self, paths):
        calls.append(len(paths))
        if len(calls) == 1:
            raise ExifToolDied("ExifTool exited unexpectedly")
        return real_read(self, paths)

    monkeypatch.setattr(ExifTool, "read", dies_once)
    conn = open_db(root); run_inventory(conn, root); build_units(conn)
    run_metadata(conn, root, Settings())
    assert calls == [2, 1, 1]  # the batch, then each file alone after a restart
    rows = conn.execute("select meta_status, error from files").fetchall()
    assert [tuple(r) for r in rows] == [("done", None), ("done", None)]


@requires_exiftool
def test_unreadable_reply_is_retried_per_file(root, make_image, monkeypatch):
    import json

    make_image(root / "Sumber/HP/DCIM/DSC_1.JPG", exif_datetime="2023:08:12 14:30:22")
    make_image(root / "Sumber/HP/DCIM/DSC_2.JPG", seed=1, exif_datetime="2023:08:13 09:00:00")
    real_read, calls = ExifTool.read, []

    def garbled(self, paths):
        calls.append(len(paths))
        if len(paths) > 1 or paths[0].name == "DSC_2.JPG":
            raise json.JSONDecodeError("Expecting value", "garbage", 0)
        return real_read(self, paths)

    monkeypatch.setattr(ExifTool, "read", garbled)
    conn = open_db(root); run_inventory(conn, root); build_units(conn)
    run_metadata(conn, root, Settings())
    assert calls == [2, 1, 1]
    rows = {r["rel_path"].rsplit("/", 1)[-1]: (r["meta_status"], r["error"])
            for r in conn.execute("select rel_path, meta_status, error from files")}
    assert rows["DSC_1.JPG"] == ("done", None)
    assert rows["DSC_2.JPG"][0] == "error" and "unreadable reply" in rows["DSC_2.JPG"][1]
