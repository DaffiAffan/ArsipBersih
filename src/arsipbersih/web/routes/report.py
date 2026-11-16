"""Report page: totals, verification job and cache cleanup."""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends, Request

from ...db import open_db
from ...layout import STATE_DIR
from ...report import _dir_bytes, build_report, verify
from ..deps import get_conn, get_root, render, start_job

router = APIRouter()

VERIFY_LIST_LIMIT = 200


@router.get("/laporan")
def page(request: Request):
    return render(request, "report.html", active="laporan", title="Laporan")


@router.get("/api/report")
def report(conn: sqlite3.Connection = Depends(get_conn), root: Path = Depends(get_root)):
    return asdict(build_report(conn, root))


def verify_result_dict(result) -> dict:
    d = asdict(result)
    d["missing_total"] = len(result.missing)
    d["unexpected_total"] = len(result.unexpected)
    d["missing"] = result.missing[:VERIFY_LIST_LIMIT]
    d["unexpected"] = result.unexpected[:VERIFY_LIST_LIMIT]
    return d


def _verify_job(root: Path):
    def run(report_cb, stop):
        conn = open_db(root)
        try:
            return verify_result_dict(verify(conn, root))
        finally:
            conn.close()

    return run


def clear_cache(root: Path) -> dict:
    expected = (root / STATE_DIR / "cache").resolve()
    target = root / STATE_DIR / "cache"
    if not target.exists():
        return {"removed_bytes": 0}
    if target.resolve() != expected or not target.is_dir() or target.is_symlink():
        raise RuntimeError("Folder cache tidak valid, tidak ada yang dihapus")
    if expected.parent != (root / STATE_DIR).resolve():
        raise RuntimeError("Folder cache di luar folder status, tidak ada yang dihapus")
    size = _dir_bytes(expected)
    shutil.rmtree(expected)
    expected.mkdir()
    return {"removed_bytes": size}


def _cache_job(root: Path):
    def run(report_cb, stop):
        return clear_cache(root)

    return run


@router.post("/api/report/verify")
def start_verify(request: Request, root: Path = Depends(get_root)):
    return start_job(request, "verify", "Verifikasi", _verify_job(root))


@router.post("/api/cache/clear")
def start_clear_cache(request: Request, root: Path = Depends(get_root)):
    return start_job(request, "cache", "Hapus cache", _cache_job(root))
