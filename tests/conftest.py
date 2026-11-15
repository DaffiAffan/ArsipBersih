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
