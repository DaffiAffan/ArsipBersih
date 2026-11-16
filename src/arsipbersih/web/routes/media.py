"""GET /media/*: thumbnails, full-size views and playable video, always looked up by file_id."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Response
from fastapi.responses import FileResponse

from ...config import load_settings
from ...db import open_db
from ..deps import get_root
from ..media import (
    BROWSER_IMAGE_EXTS,
    MediaUnavailable,
    ensure_playable_video,
    ensure_preview,
    ensure_thumb,
    source_path,
)
from ..messages import UserError

router = APIRouter(prefix="/media")

_PLACEHOLDER_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 240">'
    '<rect width="320" height="240" fill="#2b2f36"/>'
    '<path d="M110 150l30-40 24 30 16-20 30 30z" fill="#59616e"/>'
    '<circle cx="124" cy="96" r="12" fill="#59616e"/></svg>'
)


def _lookup(root: Path, file_id: str, *, settings: bool = False):
    """Open the DB inside the handler thread (a yield-dependency may close it on another thread)."""
    conn = open_db(root)
    try:
        return _row(conn, file_id), (load_settings(conn) if settings else None)
    finally:
        conn.close()


def _row(conn: sqlite3.Connection, file_id: str):
    row = conn.execute(
        "select f.id, f.rel_path, f.kind, f.ext, m.duration_s, m.video_codec "
        "from files f left join media_info m on m.file_id = f.id where f.id = ?",
        (file_id,),
    ).fetchone()
    if row is None:
        raise UserError("File tidak ditemukan", 404)
    return row


@router.get("/thumb/{file_id}")
def thumb(file_id: str, root: Path = Depends(get_root)):
    row, settings = _lookup(root, file_id, settings=True)
    try:
        path = ensure_thumb(root, row, settings)
    except MediaUnavailable:
        path = None
    if path is None:
        return Response(_PLACEHOLDER_SVG, media_type="image/svg+xml")
    return FileResponse(path, media_type="image/webp")


@router.get("/view/{file_id}")
def view(file_id: str, root: Path = Depends(get_root)):
    row, settings = _lookup(root, file_id, settings=True)
    try:
        if row["ext"] in BROWSER_IMAGE_EXTS:
            return FileResponse(source_path(root, row))
        path = ensure_preview(root, row, settings)
    except MediaUnavailable as exc:
        raise UserError(str(exc), 404) from exc
    if path is None:
        raise UserError("Pratinjau tidak bisa dibuat untuk file ini", 404)
    return FileResponse(path, media_type="image/webp")


@router.get("/video/{file_id}")
def video(file_id: str, root: Path = Depends(get_root)):
    row, _ = _lookup(root, file_id)
    try:
        return FileResponse(ensure_playable_video(root, row))
    except MediaUnavailable as exc:
        raise UserError(str(exc), 404) from exc
