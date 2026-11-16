from __future__ import annotations

from PIL import Image

from arsipbersih.config import Settings
from arsipbersih.formats import Kind
from arsipbersih.tools import find_tool
from arsipbersih.visual import (
    analyze_photo,
    analyze_video,
    hamming,
    load_gray_small,
    luma_stats,
    phash64,
    sharpness_score,
)
from conftest import requires_ffmpeg


def _gray(path, s=Settings()):
    return load_gray_small(path, Kind.PHOTO, s.analysis_px)


def test_phash_survives_resize_and_recompress(root, make_image):
    a = make_image(root / "a.jpg", seed=3, size=(4000, 3000))
    b = root / "b.jpg"
    Image.open(a).resize((1600, 1200)).save(b, quality=60)
    other = make_image(root / "c.jpg", seed=99)
    assert hamming(phash64(_gray(a)), phash64(_gray(b))) <= 6
    assert hamming(phash64(_gray(a)), phash64(_gray(other))) > 12


def test_blur_and_darkness_scores(root, make_image):
    sharp = make_image(root / "s.jpg", seed=4)
    blurry = make_image(root / "b.jpg", seed=4, blur=6.0)
    dark = make_image(root / "d.jpg", seed=4, brightness=0.05)
    assert sharpness_score(_gray(blurry)) < 0.5 * sharpness_score(_gray(sharp))
    assert luma_stats(_gray(dark))[0] < 20


def test_truncated_and_empty_files_do_not_raise(root, make_image):
    good = make_image(root / "g.jpg")
    data = good.read_bytes()
    (root / "t.jpg").write_bytes(data[: len(data) // 3])
    (root / "e.jpg").write_bytes(b"")
    for name in ("t.jpg", "e.jpg"):
        r = analyze_photo(root / name, Kind.PHOTO, Settings(), root / f"{name}.webp", None)
        assert r.decode_ok is False and r.error


def test_heic_gets_preview_and_thumbnail(root, make_image):
    h = make_image(root / "Sumber/HP/IMG_1.HEIC", fmt="HEIF")
    r = analyze_photo(h, Kind.PHOTO, Settings(), root / "t.webp", root / "p.webp")
    assert r.decode_ok and (root / "t.webp").exists() and (root / "p.webp").exists()
    assert max(Image.open(root / "t.webp").size) == 320


@requires_ffmpeg
def test_dark_video_frames(root, make_video):
    v = make_video(root / "v.mp4", seconds=3, dark=True)
    r = analyze_video(v, 3.0, Settings(), find_tool("ffmpeg"), root / "v.webp")
    assert r.decode_ok and len(r.frame_lumas) == 3 and max(r.frame_lumas) < 15


def test_worker_crash_only_marks_the_culprit(root, make_image, monkeypatch):
    from arsipbersih.db import open_db
    from arsipbersih.inventory import run_inventory
    from arsipbersih.visual import run_visual

    for name in ("a.jpg", "crash.jpg", "c.jpg"):
        make_image(root / "Sumber" / name, size=(200, 150))
    conn = open_db(root)
    run_inventory(conn, root)
    monkeypatch.setenv("ARSIPBERSIH_TEST_CRASH_ON", "crash.jpg")  # read by visual._worker in the child
    assert run_visual(conn, root, Settings(workers=2)) == 3
    status = {
        r["rel_path"].rsplit("/", 1)[-1]: (r["visual_status"], r["error"])
        for r in conn.execute("select rel_path, visual_status, error from files")
    }
    assert status["crash.jpg"] == ("error", "worker crashed while decoding")
    assert status["a.jpg"][0] == "done" and status["c.jpg"][0] == "done"


def test_pool_broken_during_submit_does_not_abort(root, make_image, monkeypatch):
    from concurrent.futures import Future
    from concurrent.futures.process import BrokenProcessPool

    from arsipbersih import visual
    from arsipbersih.db import open_db
    from arsipbersih.inventory import run_inventory

    class FakePool:
        made = 0

        def __init__(self, *a, **k):
            FakePool.made += 1
            self.breaks = FakePool.made == 1  # only the first pool breaks, on its 2nd submit
            self.n = 0

        def submit(self, fn, job):
            self.n += 1
            if self.breaks and self.n >= 2:
                raise BrokenProcessPool("died while queueing")
            f = Future()
            f.set_result(fn(job))
            return f

        def shutdown(self, *a, **k):
            pass

    for name in ("a.jpg", "b.jpg", "c.jpg"):
        make_image(root / "Sumber" / name, size=(200, 150))
    conn = open_db(root)
    run_inventory(conn, root)
    monkeypatch.setattr(visual, "_pool", FakePool)
    assert visual.run_visual(conn, root, Settings(workers=2)) == 3
    assert {r[0] for r in conn.execute("select visual_status from files")} == {"done"}


def test_huge_images_are_unknown_not_broken(root, make_image, monkeypatch):
    from arsipbersih import visual

    monkeypatch.setattr(visual, "MAX_DECODE_PIXELS", 1_100_000)
    jpg = make_image(root / "pano.jpg", size=(2400, 1800))  # drafted to 1200x900 before decoding
    png = root / "pano.png"
    Image.open(jpg).save(png)
    ok = analyze_photo(jpg, Kind.PHOTO, Settings(), root / "j.webp", None)
    big = analyze_photo(png, Kind.PHOTO, Settings(), root / "p.webp", None)
    assert ok.decode_ok is True
    assert big.decode_ok is None and not big.retry and "too large" in big.error


def test_memory_error_is_retried_not_broken(root, make_image, monkeypatch):
    from arsipbersih import visual

    def oom(*a, **k):
        raise MemoryError()

    monkeypatch.setattr(visual, "_load_rgb", oom)
    r = analyze_photo(make_image(root / "a.jpg"), Kind.PHOTO, Settings(), root / "a.webp", None)
    assert r.decode_ok is None and r.retry
