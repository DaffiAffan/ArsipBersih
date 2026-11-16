from __future__ import annotations

import functools
import random
import subprocess
from pathlib import Path

import pillow_heif
import pytest
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from arsipbersih.tools import find_tool

pillow_heif.register_heif_opener()

# Plain marks (registered in pyproject). Both `@pytest.mark.requires_ffmpeg` and
# `from conftest import requires_ffmpeg` work; the hook below does the skipping.
requires_exiftool = pytest.mark.requires_exiftool
requires_ffmpeg = pytest.mark.requires_ffmpeg

_MARK_TOOLS = {"requires_exiftool": "exiftool", "requires_ffmpeg": "ffmpeg"}


@functools.cache
def _has_tool(name: str) -> bool:
    return find_tool(name) is not None


def pytest_collection_modifyitems(config, items):
    for item in items:
        for mark, tool in _MARK_TOOLS.items():
            if item.get_closest_marker(mark) and not _has_tool(tool):
                item.add_marker(pytest.mark.skip(reason=f"{tool} not available"))


_EXT_FORMATS = {
    ".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP",
    ".heic": "HEIF", ".heif": "HEIF",
}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "Sumber").mkdir()
    return tmp_path


@pytest.fixture
def make_image():
    def _make(
        path,
        *,
        size=(1200, 900),
        seed=0,
        blur=0.0,
        brightness=1.0,
        exif_datetime=None,
        make=None,
        model=None,
        fmt=None,
    ) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        w, h = size
        rng = random.Random(seed)
        # Seeded vertical gradient plus seeded shapes: structured, not noise, so pHash is stable.
        c1 = tuple(rng.randrange(256) for _ in range(3))
        c2 = tuple(rng.randrange(256) for _ in range(3))
        img = Image.new("RGB", size)
        draw = ImageDraw.Draw(img)
        for y in range(h):
            t = y / max(h - 1, 1)
            draw.line([(0, y), (w, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(c1, c2)))
        for _ in range(8):
            x0, y0 = rng.randrange(w), rng.randrange(h)
            x1 = x0 + rng.randrange(max(w // 8, 1), max(w // 3, 2))
            y1 = y0 + rng.randrange(max(h // 8, 1), max(h // 3, 2))
            color = tuple(rng.randrange(256) for _ in range(3))
            if rng.random() < 0.5:
                draw.rectangle([x0, y0, x1, y1], fill=color)
            else:
                draw.ellipse([x0, y0, x1, y1], fill=color)
        if brightness != 1.0:
            img = ImageEnhance.Brightness(img).enhance(brightness)
        if blur:
            img = img.filter(ImageFilter.GaussianBlur(blur))

        fmt_name = (fmt or _EXT_FORMATS.get(path.suffix.lower(), "JPEG")).upper()
        save_kwargs = {}
        if exif_datetime or make or model:
            exif = Image.Exif()
            if make:
                exif[0x010F] = make
            if model:
                exif[0x0110] = model
            if exif_datetime:
                exif.get_ifd(0x8769)[36867] = exif_datetime
            save_kwargs["exif"] = exif
        if fmt_name in ("JPEG", "WEBP", "HEIF"):
            save_kwargs["quality"] = 92
        img.save(path, format=fmt_name, **save_kwargs)
        return path

    return _make


@pytest.fixture
def make_video():
    def _make(path, *, seconds=3.0, dark=False, size=(640, 360)) -> Path:
        ffmpeg = find_tool("ffmpeg")
        if ffmpeg is None:
            pytest.skip("ffmpeg not available")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        w, h = size
        src = (
            f"color=c=black:s={w}x{h}:d={seconds}:r=25"
            if dark
            else f"testsrc2=s={w}x{h}:d={seconds}:r=25"
        )
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", src,
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
            check=True,
        )
        return path

    return _make


_CONN_PHOTO_EXTS = {"jpg", "jpeg", "png", "heic", "heif", "webp", "gif"}
_CONN_RAW_EXTS = {"cr2", "cr3", "nef", "arw", "dng", "raf", "orf", "rw2"}
_CONN_VIDEO_EXTS = {"mp4", "mov", "3gp", "mkv", "avi", "mts"}
_CONN_AUX_EXTS = {"lrv", "thm"}


def _conn_kind(ext: str) -> str:
    if ext in _CONN_PHOTO_EXTS:
        return "photo"
    if ext in _CONN_RAW_EXTS:
        return "raw"
    if ext in _CONN_VIDEO_EXTS:
        return "video"
    if ext in _CONN_AUX_EXTS:
        return "gopro_aux"
    return "other"


@pytest.fixture
def conn_with(root):
    """Open a DB on `root` and insert one unit + primary file + media_info row per dict."""
    import json

    from arsipbersih.db import new_id, open_db

    conn = open_db(root)
    roots = {"source": "Sumber", "archive": "Arsip", "review": "_review"}

    def _insert(rows: list[dict]):
        for r in rows:
            name = r["name"]
            path = r.get("path", "Sumber/HP/DCIM")
            loc = r.get("loc", "source")
            if path.split("/")[0] in ("Sumber", "Arsip", "_review"):
                rel_dir = path
            else:
                rel_dir = f"{roots[loc]}/{path}"
            rel_path = f"{rel_dir}/{name}"
            parts = path.split("/")
            src = r.get("src", parts[1] if parts[0] == "Sumber" and len(parts) > 1 else None)
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            kind = r.get("kind") or _conn_kind(ext)
            sha = r.get("sha")
            unit_id, file_id = new_id(), new_id()
            conn.execute(
                "insert into units(id, primary_file_id, taken_at, location, primary_category) "
                "values(?, NULL, ?, ?, ?)",
                (unit_id, r.get("taken"), loc, r.get("category")),
            )
            conn.execute(
                "insert into files(id, unit_id, rel_path, orig_rel_path, size, mtime_ns, kind, ext, "
                "source_label, sha256, hash_status, meta_status, visual_status) "
                "values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (file_id, unit_id, rel_path, rel_path, r.get("size", 1_000_000), 0, kind, ext,
                 src, sha, "done" if sha else None, "done", "done"),
            )
            conn.execute("update units set primary_file_id=? where id=?", (file_id, unit_id))
            frames = r.get("frames")
            conn.execute(
                "insert into media_info(file_id, make, model, width, height, duration_s, phash, "
                "frame_phashes, sharpness, luma_mean, luma_std, clipping, decode_ok) "
                "values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (file_id, r.get("make"), r.get("model"), r.get("w", 4000), r.get("h", 3000),
                 r.get("duration"), r.get("phash"), json.dumps(frames) if frames else None,
                 r.get("sharp", 80.0), r.get("luma", 120.0), r.get("std", 50.0),
                 r.get("clip", 0.0), r.get("decode_ok", 1)),
            )
            if r.get("category"):
                conn.execute(
                    "insert into flags(id, unit_id, category, score, reason) values(?,?,?,NULL,'test')",
                    (new_id(), unit_id, r["category"]),
                )
        conn.commit()
        return conn

    return _insert


@pytest.fixture
def mini_collection(root, make_image):
    """About 15 files across two sources: dups, WA copy, burst, blur, screenshot, broken, event, undated."""
    from PIL import Image

    s = root / "Sumber"
    cam = dict(make="Canon", model="EOS 100D")
    dt = "2023:06:01 10:00:00"

    # Exact duplicate pair across two sources (byte copy).
    a = make_image(s / "HP-A/DCIM/Camera/IMG_20230601_100000.jpg", seed=1, exif_datetime=dt, **cam)
    (s / "HP-B/DCIM/Camera").mkdir(parents=True)
    (s / "HP-B/DCIM/Camera/IMG_20230601_100000.jpg").write_bytes(a.read_bytes())

    # WhatsApp re-encoded copy of the same picture (smaller, lower quality, no camera info).
    wa = s / "HP-B/WhatsApp/Media/WhatsApp Images/IMG-20230601-WA0001.jpg"
    wa.parent.mkdir(parents=True)
    Image.open(a).resize((600, 450)).save(wa, quality=60)

    # Burst of three: same picture, one second apart.
    for i in range(3):
        make_image(
            s / f"HP-A/DCIM/Camera/IMG_20230605_12000{i}.jpg",
            seed=2, exif_datetime=f"2023:06:05 12:00:0{i}", **cam,
        )

    # Blurry photo, screenshot, truncated JPEG, undated DSC_ file, .nomedia.
    make_image(s / "HP-A/DCIM/Camera/IMG_20230610_090000.jpg", seed=3, blur=8,
               exif_datetime="2023:06:10 09:00:00", **cam)
    make_image(s / "HP-A/Pictures/Screenshots/Screenshot_20230701-101010.png",
               size=(1080, 2400), seed=4)
    broken = make_image(s / "HP-A/DCIM/Camera/IMG_20230615_150000.jpg", seed=5,
                        exif_datetime="2023:06:15 15:00:00", **cam)
    broken.write_bytes(broken.read_bytes()[: broken.stat().st_size // 3])
    make_image(s / "HP-A/DCIM/Camera/DSC_0042.jpg", seed=6)
    (s / "HP-A/DCIM/.nomedia").write_bytes(b"")

    # Rinjani event folder on both sources, dated August 2023.
    for i, (src, seed) in enumerate([("HP-A", 11), ("HP-A", 12), ("HP-B", 13), ("HP-B", 14)]):
        make_image(s / f"{src}/Rinjani/IMG_2023081{i}_080000.jpg", seed=seed,
                   exif_datetime=f"2023:08:1{i} 08:00:00", **cam)
    # Synthetic images are smooth (Laplacian variance 10-47); calibrate so only the blurred one is "burem".
    from dataclasses import replace

    from arsipbersih.config import load_settings, save_settings
    from arsipbersih.db import open_db

    conn = open_db(root)
    save_settings(conn, replace(load_settings(conn), blur_threshold=5.0))
    conn.close()
    return root


@pytest.fixture
def web(root):
    from fastapi.testclient import TestClient

    from arsipbersih.web.app import create_app

    app = create_app(root, allowed_hosts={"testserver"}, start_jobs=False)
    with TestClient(app, headers={"X-ArsipBersih": "1"}) as c:
        c.app_ref = app
        yield c
