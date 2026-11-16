import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import requires_ffmpeg

from arsipbersih.db import new_id
from arsipbersih.web import media
from arsipbersih.web.media import needs_transcode


@pytest.mark.parametrize("ext,codec,expected", [
    ("mp4", "h264", False), ("mov", "hevc", True), ("3gp", "h264", True),
    ("avi", "mpeg4", True), ("webm", "vp9", False), ("mp4", None, True),
])
def test_needs_transcode(ext, codec, expected):
    assert needs_transcode(ext, codec) is expected


def _fid(conn, name):
    return conn.execute("select id from files where rel_path like ?", (f"%/{name}",)).fetchone()["id"]


def test_view_serves_original_jpeg_bytes(web, root, make_image, conn_with):
    p = make_image(root / "Sumber/HP/DCIM/a.jpg", seed=1)
    conn = conn_with([{"name": "a.jpg"}])
    before = os.stat(p).st_mtime_ns
    data = p.read_bytes()
    r = web.get(f"/media/view/{_fid(conn, 'a.jpg')}")
    assert r.status_code == 200 and r.content == data
    assert os.stat(p).st_mtime_ns == before


def test_tif_view_uses_generated_preview(web, root, make_image, conn_with):
    make_image(root / "Sumber/HP/DCIM/a.tif", seed=2)
    conn = conn_with([{"name": "a.tif", "kind": "photo"}])
    fid = _fid(conn, "a.tif")
    r = web.get(f"/media/view/{fid}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert (root / ".arsipbersih/cache/preview" / fid[:2] / f"{fid}.webp").exists()


def test_thumb_created_and_placeholder_when_missing(web, root, make_image, conn_with):
    make_image(root / "Sumber/HP/DCIM/a.jpg", seed=1)
    conn = conn_with([{"name": "a.jpg"}, {"name": "gone.jpg"}])
    r = web.get(f"/media/thumb/{_fid(conn, 'a.jpg')}")
    assert r.headers["content-type"] == "image/webp"
    r = web.get(f"/media/thumb/{_fid(conn, 'gone.jpg')}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg")


def test_missing_file_is_404_not_500(web, conn_with):
    conn = conn_with([{"name": "gone.jpg"}])
    r = web.get(f"/media/view/{_fid(conn, 'gone.jpg')}")
    assert r.status_code == 404 and "tidak ditemukan" in r.json()["detail"]


def test_unknown_file_id_is_404(web):
    assert web.get(f"/media/view/{new_id()}").status_code == 404


@requires_ffmpeg
def test_transcode_runs_once_and_is_cached(web, root, make_video, conn_with, monkeypatch):
    tmp = make_video(root / "Sumber/HP/DCIM/tmp.mp4", seconds=1.0)
    tmp.rename(root / "Sumber/HP/DCIM/v.avi")  # ffmpeg sniffs the real container
    conn = conn_with([{"name": "v.avi"}])
    fid = _fid(conn, "v.avi")
    calls = []
    real = subprocess.run

    def spy(cmd, *a, **kw):
        if "libx264" in cmd:
            calls.append(cmd)
        return real(cmd, *a, **kw)

    monkeypatch.setattr(media.subprocess, "run", spy)
    with ThreadPoolExecutor(2) as ex:
        rs = list(ex.map(lambda _: web.get(f"/media/video/{fid}"), range(2)))
    assert [r.status_code for r in rs] == [200, 200]
    assert len(calls) == 1
    assert not list((root / ".arsipbersih/cache/video").rglob("*.partial.mp4"))
