"""File kind detection by magic bytes, with extension fallback."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from .layout import long_path


class Kind(StrEnum):
    PHOTO = "photo"
    RAW = "raw"
    VIDEO = "video"
    SIDECAR = "sidecar"
    GOPRO_AUX = "gopro_aux"
    OTHER = "other"


PHOTO_EXTS = {"jpg", "jpeg", "heic", "heif", "png", "webp", "gif", "bmp", "tif", "tiff"}
RAW_EXTS = {"cr2", "cr3", "nef", "arw", "dng", "raf", "orf", "rw2"}
VIDEO_EXTS = {"mp4", "mov", "m4v", "3gp", "mkv", "avi", "mts", "m2ts", "webm"}
SIDECAR_EXTS = {"xmp", "aae"}
GOPRO_AUX_EXTS = {"lrv", "thm"}

SKIP_DIRS = {".thumbnails", "cache", ".cache", "lost.dir"}
IGNORED_NAMES = {".nomedia", "thumbs.db", "desktop.ini"}

_PHOTO_BRANDS = {b"heic", b"heix", b"hevc", b"mif1", b"msf1", b"avif"}
_VIDEO_BRANDS = {b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"M4V ", b"qt  "}


def _ext(path: Path) -> str:
    return path.suffix.lower().lstrip(".")


def _kind_by_ext(ext: str) -> Kind:
    if ext in PHOTO_EXTS:
        return Kind.PHOTO
    if ext in RAW_EXTS:
        return Kind.RAW
    if ext in VIDEO_EXTS:
        return Kind.VIDEO
    if ext in SIDECAR_EXTS:
        return Kind.SIDECAR
    if ext in GOPRO_AUX_EXTS:
        return Kind.GOPRO_AUX
    return Kind.OTHER


def _kind_by_magic(head: bytes, ext: str) -> Kind | None:
    if head[:3] == b"\xff\xd8\xff" or head[:8] == b"\x89PNG\r\n\x1a\n":
        return Kind.PHOTO
    if head[:6] in (b"GIF87a", b"GIF89a") or head[:2] == b"BM":
        return Kind.PHOTO
    if head[:4] == b"RIFF" and len(head) >= 12:
        if head[8:12] == b"WEBP":
            return Kind.PHOTO
        if head[8:12] == b"AVI ":
            return Kind.VIDEO
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand == b"crx ":
            return Kind.RAW
        if brand in _PHOTO_BRANDS:
            return Kind.PHOTO
        if brand in _VIDEO_BRANDS or brand[:3] == b"3gp":
            return Kind.VIDEO
        return None
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return Kind.VIDEO
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return Kind.RAW if ext in RAW_EXTS else Kind.PHOTO
    return None


def detect_kind(path: Path) -> Kind:
    path = Path(path)
    ext = _ext(path)
    # Sidecars and GoPro aux files are identified by extension: .thm is really a JPEG
    # and .lrv a real MP4, so magic bytes would misclassify them.
    if ext in SIDECAR_EXTS:
        return Kind.SIDECAR
    if ext in GOPRO_AUX_EXTS:
        return Kind.GOPRO_AUX
    try:
        with open(long_path(path), "rb") as f:
            head = f.read(32)
            kind = _kind_by_magic(head, ext) if head else None
            if kind is None and head[:1] == b"\x47":
                f.seek(188)
                if f.read(1) == b"\x47":
                    return Kind.VIDEO
    except OSError:
        return _kind_by_ext(ext)
    if kind is not None:
        return kind
    return _kind_by_ext(ext)
