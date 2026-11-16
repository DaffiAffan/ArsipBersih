"""Photo/video metadata via ExifTool (stay_open) and ffprobe; sets each unit's capture date."""
from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path

from arsipbersih.config import Settings
from arsipbersih.dates import pick_taken_at
from arsipbersih.layout import long_path
from arsipbersih.tools import NO_CONSOLE_FLAGS, find_tool

log = logging.getLogger(__name__)

BATCH = 200
_KINDS = "('photo','raw','video')"
_READY = b"{ready}"
_EXIF_ARGS = [
    "-json", "-G1", "-charset", "filename=utf8",
    "-DateTimeOriginal", "-CreateDate", "-Make", "-Model",
    "-ImageWidth", "-ImageHeight", "-GPSLatitude", "-n",
]


class ExifToolDied(RuntimeError):
    """The ExifTool process itself failed (EOF, broken pipe, exited), not a file it read."""


class ExifToolUnavailable(RuntimeError):
    """ExifTool died and could not be restarted; the unread files stay NULL (retryable)."""


class TransientProbeError(RuntimeError):
    """ffprobe timed out, was interrupted, or could not start: retry on the next scan."""


# Windows STATUS_CONTROL_C_EXIT: the child was ended by a console Ctrl+C.
_CTRL_C_EXIT = 0xC000013A


class ExifTool:
    """One long-lived `exiftool -stay_open True -@ -` process."""

    def __init__(self, exe: str):
        self.exe = exe
        self._proc: subprocess.Popen | None = None

    def __enter__(self) -> "ExifTool":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def start(self) -> None:
        """Start the process and wait for it to answer. Raises ExifToolDied when it does not."""
        try:
            self._proc = subprocess.Popen(
                [self.exe, "-stay_open", "True", "-@", "-"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=NO_CONSOLE_FLAGS,
            )
        except OSError as e:
            raise ExifToolDied(f"ExifTool could not be started: {e}") from e
        try:
            self._command(["-ver"])
        except ExifToolDied:
            self.close()
            raise

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.write(b"-stay_open\nFalse\n")
            proc.stdin.flush()
            proc.stdin.close()
            proc.wait(timeout=10)
        except (OSError, ValueError, subprocess.SubprocessError):
            try:
                proc.kill()
                proc.wait(timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
        finally:
            for stream in (proc.stdin, proc.stdout):
                try:
                    if stream:
                        stream.close()
                except OSError:
                    pass

    def restart(self) -> None:
        """Replace a dead process. Raises ExifToolDied when the new one does not answer."""
        self.close()
        self.start()

    def _command(self, args: list[str]) -> bytes:
        """Send one -execute command; return its output up to {ready}."""
        proc = self._proc
        if proc is None:
            raise ExifToolDied("ExifTool is not running")
        try:
            proc.stdin.write(("\n".join([*args, "-execute"]) + "\n").encode("utf-8"))
            proc.stdin.flush()
            out = bytearray()
            while True:
                line = proc.stdout.readline()
                if not line:
                    raise ExifToolDied("ExifTool exited unexpectedly")
                if line.strip() == _READY:
                    return bytes(out)
                out += line
        except (OSError, ValueError) as e:  # broken pipe, or a stream closed under us
            raise ExifToolDied(f"ExifTool pipe failed: {type(e).__name__}: {e}") from e

    def _execute(self, paths: list[Path]) -> list[dict]:
        out = self._command([*_EXIF_ARGS, *(str(p) for p in paths)])
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
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
             long_path(Path(path))],
            capture_output=True, timeout=120, check=False,
            stdin=subprocess.DEVNULL, creationflags=NO_CONSOLE_FLAGS,
        )
    except subprocess.TimeoutExpired as e:
        raise TransientProbeError(f"timed out after {e.timeout:.0f} s") from e
    except OSError as e:
        raise TransientProbeError(f"could not run: {e}") from e
    if proc.returncode < 0 or proc.returncode == _CTRL_C_EXIT:
        raise TransientProbeError(f"interrupted (exit code {proc.returncode})")
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
    # A row left retryable (NULL after an ffprobe timeout) still has its ExifTool fields.
    cond = "(f.meta_status is not null or mi.file_id is not null)"
    if include_unread:
        cond = f"(f.meta_status is not null or mi.file_id is not null or f.kind in {_KINDS})"
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
    if status is not None or error:
        conn.execute("update files set meta_status=?, error=? where id=?", (status, error, fid))


def _restart(et: ExifTool) -> None:
    try:
        et.restart()
    except ExifToolDied as e:
        raise ExifToolUnavailable(str(e)) from e


def _read_exif(et: ExifTool, paths: list[Path]) -> tuple[list[dict | None], dict[int, str]]:
    """ExifTool entries for paths, plus {index: error} for files that crashed ExifTool
    or got an unreadable (non-JSON) reply.

    When the process dies mid-batch the files are not to blame yet: ExifTool is restarted
    once and the batch is read again one file at a time. A file that kills it again is
    marked, and ExifTool is restarted for the next file. Raises ExifToolUnavailable when a
    restart fails; the caller then stores nothing, so the whole batch stays NULL.
    """
    try:
        return et.read(paths), {}
    except ExifToolDied as e:
        log.warning("ExifTool died mid-batch (%s); retrying one file at a time", e)
        _restart(et)
    except ValueError as e:  # json.JSONDecodeError: one bad reply must not fail the batch
        log.warning("ExifTool sent an unreadable reply (%s); retrying one file at a time", e)
    entries: list[dict | None] = []
    crashed: dict[int, str] = {}
    for i, p in enumerate(paths):
        try:
            entries.extend(et.read([p]))
        except ExifToolDied as e:
            _restart(et)  # a failed restart leaves this file unmarked too
            entries.append(None)
            crashed[i] = f"exiftool: crashed on this file ({e})"
        except ValueError as e:
            entries.append(None)
            crashed[i] = f"exiftool: unreadable reply for this file ({e})"[:500]
    return entries, crashed


def _process(conn, root: Path, et, ffprobe, batch) -> None:
    paths = [root / r["rel_path"] for r in batch]
    entries: list[dict | None] = [None] * len(batch)
    crashed: dict[int, str] = {}
    batch_err = None
    if et is not None:
        try:
            entries, crashed = _read_exif(et, paths)
        except ExifToolUnavailable:
            raise
        except Exception as e:  # noqa: BLE001 - a bad reply, not a dead process
            batch_err = f"{type(e).__name__}: {e}"
    for i, (row, path, entry) in enumerate(zip(batch, paths, entries)):
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
                    status = "error"
                    err = crashed.get(i, "exiftool: no metadata returned")
            if row["kind"] == "video" and ffprobe:
                try:
                    v = probe_video(path, ffprobe)
                    fields.update(duration_s=v["duration_s"], video_codec=v["video_codec"],
                                  has_audio=int(v["has_audio"]))
                    fields["width"] = fields.get("width") or v["width"]
                    fields["height"] = fields.get("height") or v["height"]
                    if not fields.get("qt_datetime") and v["creation_time"]:
                        fields["qt_datetime"] = v["creation_time"]
                except TransientProbeError as e:
                    status = None  # not the file's fault: read it again on the next scan
                    msg = f"ffprobe: {e}"
                    err = f"{err}; {msg}"[:500] if err else msg
                except Exception as e:  # noqa: BLE001
                    if et is not None:
                        status = "error"
                    msg = f"ffprobe: {e}" if str(e) else f"ffprobe: {type(e).__name__}"
                    err = f"{err}; {msg}" if err else msg
                    err = err[:500]
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
    """Read metadata for every unread file. Returns files processed.

    Raises ExifToolUnavailable (after setting dates for what was read) when ExifTool dies
    and cannot be restarted; the batch in progress and the rest stay NULL for the next scan.
    """
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

    unavailable: ExifToolUnavailable | None = None
    if exe:
        et = ExifTool(exe)
        try:
            et.start()
            run(et)
        except ExifToolDied as e:  # did not start
            unavailable = ExifToolUnavailable(str(e))
        except ExifToolUnavailable as e:
            unavailable = e
        finally:
            et.close()
    else:
        run(None)

    _set_unit_dates(conn, settings, include_unread=exe is None)
    if unavailable is not None:
        raise unavailable
    return done
