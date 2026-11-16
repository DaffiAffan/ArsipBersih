"""Settings page and API: validated form, blur threshold examples, calibrate suggestion."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import asdict, fields, replace
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ...calibrate import suggest_blur_threshold
from ...config import Settings, load_settings, save_settings
from ..deps import get_conn, render
from ..messages import UserError

router = APIRouter()

EXAMPLES = 12
INT_RANGES = {
    "workers": (1, 8),
    "phash_version_max": (0, 64),
    "phash_burst_max": (0, 64),
    "video_frame_phash_max": (0, 64),
    "thumb_px": (1, 100000),
    "preview_px": (1, 100000),
    "analysis_px": (1, 100000),
}
FLOAT_NONNEG = {
    "burst_window_s", "short_video_s", "live_photo_mov_max_s", "blur_fallback",
    "kepencet_sharpness_max", "kepencet_luma_max", "kepencet_std_max", "dark_luma_max",
    "bright_luma_min", "clipping_max", "dark_video_luma_max",
}


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _clean_folders(v) -> tuple[str, ...]:
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise UserError("Daftar folder bawaan harus berupa daftar nama.", 400)
    seen: set[str] = set()
    out: list[str] = []
    for x in v:
        name = x.strip()
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(name)
    if not out:
        raise UserError("Daftar folder bawaan tidak boleh kosong.", 400)
    return tuple(out)


def _validate(name: str, v):
    if name in INT_RANGES:
        lo, hi = INT_RANGES[name]
        if not _is_num(v) or int(v) != v or not lo <= v <= hi:
            raise UserError(f"{name} harus bilangan bulat {lo}-{hi}.", 400)
        return int(v)
    if name in FLOAT_NONNEG:
        if not _is_num(v) or v < 0:
            raise UserError(f"{name} harus angka 0 atau lebih.", 400)
        return float(v)
    if name == "blur_threshold":
        if v is None:
            return None
        if not _is_num(v) or not 0 <= v <= 100:
            raise UserError("blur_threshold harus kosong atau angka 0-100.", 400)
        return float(v)
    if name == "timezone":
        if not isinstance(v, str) or not v.strip():
            raise UserError("Zona waktu tidak valid.", 400)
        try:
            ZoneInfo(v.strip())
        except Exception:
            raise UserError("Zona waktu tidak dikenal.", 400) from None
        return v.strip()
    if name == "generic_folders":
        return _clean_folders(v)
    raise UserError(f"Pengaturan tidak dikenal: {name}", 400)


def _payload(s: Settings) -> dict:
    d = asdict(s)
    d["generic_folders"] = list(s.generic_folders)
    d["blur_effective"] = s.blur_threshold if s.blur_threshold is not None else s.blur_fallback
    return d


@router.get("/pengaturan", response_class=HTMLResponse)
def page(request: Request):
    return render(request, "settings.html", active="pengaturan", title="Pengaturan")


@router.get("/api/settings")
def get_settings(conn: sqlite3.Connection = Depends(get_conn)):
    return _payload(load_settings(conn))


@router.put("/api/settings")
def put_settings(changes: dict, conn: sqlite3.Connection = Depends(get_conn)):
    known = {f.name for f in fields(Settings)}
    unknown = sorted(set(changes) - known)
    if unknown:
        raise UserError(f"Pengaturan tidak dikenal: {', '.join(unknown)}", 400)
    cleaned = {k: _validate(k, v) for k, v in changes.items()}
    current = load_settings(conn)
    changed = {k for k, v in cleaned.items() if getattr(current, k) != v}
    new = replace(current, **cleaned)
    save_settings(conn, new)
    out = _payload(new)
    out["rescan_needed"] = bool(changed - {"workers"})
    return out


def _example(row: sqlite3.Row) -> dict:
    return {
        "unit_id": row["unit_id"],
        "file_id": row["file_id"],
        "thumb": f"/media/thumb/{row['file_id']}",
        "score": row["sharpness"],
        "reason": None,
        "seen": False,
    }


@router.get("/api/settings/blur-examples")
def blur_examples(threshold: float, conn: sqlite3.Connection = Depends(get_conn)):
    if not 0 <= threshold <= 100:
        raise UserError("Ambang harus 0-100.", 400)
    base = (
        "select u.id as unit_id, f.id as file_id, m.sharpness from units u"
        " join files f on f.id = u.primary_file_id join media_info m on m.file_id = f.id"
        " where f.kind='photo' and m.sharpness is not null and "
    )
    below = conn.execute(base + "m.sharpness < ? order by m.sharpness desc limit ?", (threshold, EXAMPLES)).fetchall()
    above = conn.execute(base + "m.sharpness >= ? order by m.sharpness asc limit ?", (threshold, EXAMPLES)).fetchall()
    return {"below": [_example(r) for r in below], "above": [_example(r) for r in above]}


@router.post("/api/settings/calibrate")
def calibrate(conn: sqlite3.Connection = Depends(get_conn)):
    suggested = suggest_blur_threshold(conn)  # 0.0 means no analysed photos
    if not suggested:
        raise UserError("Belum ada foto yang dianalisis. Jalankan scan dulu.", 400)
    return {"suggested": suggested}
