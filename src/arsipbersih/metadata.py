"""Photo/video metadata via ExifTool (stay_open) and ffprobe; sets each unit's capture date."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path

from arsipbersih.config import Settings
from arsipbersih.dates import pick_taken_at
from arsipbersih.layout import long_path
from arsipbersih.pairing import confirm_live_photos
from arsipbersih.tools import find_tool

BATCH = 200
_KINDS = "('photo','raw','video')"
_READY = b"{ready}"
_EXIF_ARGS = [
    "-json", "-G1", "-charset", "filename=utf8",
    "-DateTimeOriginal", "-CreateDate", "-Make", "-Model",
    "-ImageWidth", "-ImageHeight", "-GPSLatitude", "-n",
]


class ExifTool:
    """One long-lived `exiftool -stay_open True -@ -` process."""

    def __init__(self, exe: str):
        self.exe = exe
        self._proc: subprocess.Popen | None = None

    def __enter__(self) -> "ExifTool":
        self._proc = subprocess.Popen(
            [self.exe, "-stay_open", "True", "-@", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        return self

    def __exit__(self, *exc) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.write(b"-stay_open\nFalse\n")
            proc.stdin.flush()
            proc.stdin.close()
            proc.wait(timeout=10)
        except (OSError, subprocess.SubprocessError):
            proc.kill()
        finally:
            if proc.stdout:
                proc.stdout.close()

    def _execute(self, paths: list[Path]) -> list[dict]:
        proc = self._proc
        if proc is None:
            raise RuntimeError("ExifTool is not running")
        lines = [*_EXIF_ARGS, *(str(p) for p in paths), "-execute"]
        proc.stdin.write(("\n".join(lines) + "\n").encode("utf-8"))
        proc.stdin.flush()
        out = bytearray()
        while True:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("ExifTool exited unexpectedly")
            if line.strip() == _READY:
                break
            out += line
        text = out.decode("utf-8", errors="replace").strip()
        return json.loads(text) if text else []

    def read(self, paths: list[Path]) -> list[dict | None]:
        results: list[dict | None] = []
        for i in range(0, len(paths), BATCH):
            chunk = paths[i : i + BATCH]
            entries = self._execute(chunk)
            if len(entries) == len(chunk):
                results.extend(entries)
                continue
            by_src = {str(e.get("SourceFile", "")).replace("\\", "/"): e for e in entries}
            results.extend(by_src.get(str(p).replace("\\", "/")) for p in chunk)
        return results


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def probe_video(path: Path, ffprobe: str) -> dict:
    info = {"duration_s": None, "width": None, "height": None,
            "video_codec": None, "has_audio": False, "creation_time": None}
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
         long_path(Path(path))],
        capture_output=True, timeout=120, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace").strip() or "ffprobe failed")
    data = json.loads(proc.stdout.decode("utf-8", errors="replace") or "{}")
    fmt = data.get("format") or {}
    info["duration_s"] = _num(fmt.get("duration"))
    info["creation_time"] = (fmt.get("tags") or {}).get("creation_time")
    for s in data.get("streams") or []:
        if s.get("codec_type") == "video" and info["video_codec"] is None:
            info["video_codec"] = s.get("codec_name")
            info["width"] = s.get("width")
            info["height"] = s.get("height")
            if info["duration_s"] is None:
                info["duration_s"] = _num(s.get("duration"))
            info["creation_time"] = info["creation_time"] or (s.get("tags") or {}).get("creation_time")
        elif s.get("codec_type") == "audio":
            info["has_audio"] = True
    return info


def _present(v) -> bool:
    return v is not None and str(v).strip() != ""


def _to_utc_text(v) -> str:
    """'YYYY:MM:DD HH:MM:SS+07:00' -> same instant in UTC without offset; others unchanged."""
    s = str(v).strip()
    if len(s) > 19 and s[19] in "+-":
        try:
            dt = datetime.strptime(s[:19], "%Y:%m:%d %H:%M:%S")
            digits = s[20:].replace(":", "")
            off = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
            dt = dt - off if s[19] == "+" else dt + off
            return dt.strftime("%Y:%m:%d %H:%M:%S")
        except ValueError:
            return s
    return s


def extract_exif_fields(d: dict) -> dict:
    """Map ExifTool -G1 keys onto media_info fields."""
    out = {"make": None, "model": None, "width": None, "height": None,
           "has_gps": 0, "exif_datetime": None, "qt_datetime": None}
    for tag in ("DateTimeOriginal", "CreateDate"):
        v = d.get(f"ExifIFD:{tag}")
        if _present(v):
            out["exif_datetime"] = str(v)
            break
    qt: dict[str, str] = {}
    for key, v in d.items():
        g, _, t = key.rpartition(":")
        if not _present(v):
            continue
        if g.endswith(("QuickTime", "Keys", "UserData")) and t in (
            "CreateDate", "CreationDate", "DateTimeOriginal"
        ):
            qt.setdefault(g, _to_utc_text(v))
        if t == "GPSLatitude":
            out["has_gps"] = 1
    if qt:
        best = min(qt, key=lambda x: (not x.endswith("QuickTime"), x))
        out["qt_datetime"] = qt[best]
    for field, key in (("make", "IFD0:Make"), ("model", "IFD0:Model")):
        v = d.get(key)
        out[field] = str(v) if _present(v) else None
    for field, tag, exif_tag in (("width", "ImageWidth", "ExifImageWidth"),
                                 ("height", "ImageHeight", "ExifImageHeight")):
        cands = [d.get(f"File:{tag}"), d.get(f"ExifIFD:{exif_tag}")]
        cands += [v for k, v in d.items() if k.endswith(f":{tag}")]
        for v in cands:
            n = _num(v)
            if n:
                out[field] = int(n)
                break
    return out


def _set_unit_dates(conn: sqlite3.Connection, settings: Settings, include_unread: bool) -> None:
    cond = "f.meta_status is not null"
    if include_unread:
        cond = f"(f.meta_status is not null or f.kind in {_KINDS})"
    rows = conn.execute(
        "select u.id, f.rel_path, mi.exif_datetime, mi.qt_datetime"
        " from units u join files f on f.id=u.primary_file_id"
        " left join media_info mi on mi.file_id=f.id"
        f" where {cond}"
    ).fetchall()
    now = datetime.now()
    for r in rows:
        name = r["rel_path"].rsplit("/", 1)[-1]
        dt, src = pick_taken_at(r["exif_datetime"], r["qt_datetime"], name, settings.timezone, now)
        conn.execute(
            "update units set taken_at=?, date_source=? where id=?",
            (dt.isoformat() if dt else None, src, r["id"]),
        )
    conn.commit()


def _store(conn, fid, fields, status, error=None) -> None:
    conn.execute(
        "insert into media_info (file_id, make, model, width, height, duration_s, video_codec,"
        " has_audio, has_gps, exif_datetime, qt_datetime) values (?,?,?,?,?,?,?,?,?,?,?)"
        " on conflict(file_id) do update set make=excluded.make, model=excluded.model,"
        " width=excluded.width, height=excluded.height, duration_s=excluded.duration_s,"
        " video_codec=excluded.video_codec, has_audio=excluded.has_audio,"
        " has_gps=excluded.has_gps, exif_datetime=excluded.exif_datetime,"
        " qt_datetime=excluded.qt_datetime",
        (fid, fields.get("make"), fields.get("model"), fields.get("width"),
         fields.get("height"), fields.get("duration_s"), fields.get("video_codec"),
         fields.get("has_audio"), fields.get("has_gps"), fields.get("exif_datetime"),
         fields.get("qt_datetime")),
    )
    if status is not None:
        conn.execute("update files set meta_status=?, error=? where id=?", (status, error, fid))


def _process(conn, root: Path, et, ffprobe, batch) -> None:
    paths = [root / r["rel_path"] for r in batch]
    entries: list[dict | None] = [None] * len(batch)
    batch_err = None
    if et is not None:
        try:
            entries = et.read(paths)
        except Exception as e:  # noqa: BLE001
            batch_err = f"{type(e).__name__}: {e}"
    for row, path, entry in zip(batch, paths, entries):
        fid = row["id"]
        try:
            if batch_err is not None:
                raise RuntimeError(batch_err)
            fields = extract_exif_fields(entry) if entry else {}
            status, err = None, None
            if et is not None:
                if entry:
                    status = "done"
                else:
                    status, err = "error", "exiftool: no metadata returned"
            if row["kind"] == "video" and ffprobe:
                try:
                    v = probe_video(path, ffprobe)
                    fields.update(duration_s=v["duration_s"], video_codec=v["video_codec"],
                                  has_audio=int(v["has_audio"]))
                    fields["width"] = fields.get("width") or v["width"]
                    fields["height"] = fields.get("height") or v["height"]
                    if not fields.get("qt_datetime") and v["creation_time"]:
                        fields["qt_datetime"] = v["creation_time"]
                except Exception as e:  # noqa: BLE001
                    status = "error"
                    msg = f"ffprobe: {type(e).__name__}: {e}" if not str(e) else f"ffprobe: {e}"
                    err = msg[:500]
            _store(conn, fid, fields, status, err)
        except Exception as e:  # noqa: BLE001
            _store(conn, fid, {}, "error", f"{type(e).__name__}: {e}")


def run_metadata(
    conn: sqlite3.Connection,
    root: Path,
    settings: Settings,
    progress=None,
    stop: threading.Event | None = None,
) -> int:
    root = Path(root)
    pending = conn.execute(
        "select id, rel_path, kind from files where meta_status is null and missing=0"
        f" and purged=0 and kind in {_KINDS} order by id"
    ).fetchall()
    total = len(pending)
    exe = find_tool("exiftool")
    ffprobe = find_tool("ffprobe")
    done = 0

    def run(et) -> None:
        nonlocal done
        for i in range(0, total, BATCH):
            if stop is not None and stop.is_set():
                break
            batch = pending[i : i + BATCH]
            _process(conn, root, et, ffprobe, batch)
            conn.commit()
            done += len(batch)
            if progress:
                progress(done, total)

    if exe:
        with ExifTool(exe) as et:
            run(et)
    else:
        run(None)

    _set_unit_dates(conn, settings, include_unread=exe is None)
    confirm_live_photos(conn, settings)
    return done
