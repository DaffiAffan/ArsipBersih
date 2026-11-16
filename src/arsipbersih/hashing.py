"""SHA-256 hashing of size-colliding media files (exact-duplicate detection)."""
from __future__ import annotations

import hashlib
import signal
import sqlite3
import threading
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from arsipbersih.config import Settings
from arsipbersih.layout import long_path

CHUNK_FILES = 200
_MEDIA = "('photo','raw','video','gopro_aux')"


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(long_path(Path(path)), "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def _ignore_sigint() -> None:
    """Pool initializer: Ctrl+C pauses the scan through its stop event, not by killing workers."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _hash_worker(abs_path: str) -> tuple[str | None, str | None]:
    try:
        return sha256_file(Path(abs_path)), None
    except Exception as e:  # noqa: BLE001 - any read error is recorded, not raised
        return None, f"{type(e).__name__}: {e}"


def _pending(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute(
        f"select id, rel_path from files where missing=0 and purged=0 and kind in {_MEDIA}"
        " and hash_status is null and size in ("
        f"  select size from files where missing=0 and purged=0 and kind in {_MEDIA}"
        "  group by size having count(*) > 1)"
        " order by id"
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def run_hashing(
    conn: sqlite3.Connection,
    root: Path,
    settings: Settings,
    progress=None,
    stop: threading.Event | None = None,
) -> int:
    pending = _pending(conn)
    total = len(pending)
    done = 0
    ok = 0
    if not total:
        return 0
    root = Path(root)
    with ProcessPoolExecutor(max_workers=settings.workers, initializer=_ignore_sigint) as pool:
        for i in range(0, total, CHUNK_FILES):
            if stop is not None and stop.is_set():
                break
            chunk = pending[i : i + CHUNK_FILES]
            paths = [str(root / rel) for _, rel in chunk]
            results = list(pool.map(_hash_worker, paths))
            with conn:
                for (fid, _), (digest, err) in zip(chunk, results):
                    if err is None:
                        conn.execute(
                            "update files set sha256=?, hash_status='done', error=null where id=?",
                            (digest, fid),
                        )
                        ok += 1
                    else:
                        conn.execute(
                            "update files set hash_status='error', error=? where id=?",
                            (err, fid),
                        )
            done += len(chunk)
            if progress is not None:
                progress(done, total)
    return ok
