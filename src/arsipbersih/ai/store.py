"""SQLite storage for AI embeddings, labels and owner feedback."""

from __future__ import annotations

import sqlite3
from datetime import datetime

import numpy as np

from ..db import new_id


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def save_embeddings(conn: sqlite3.Connection, model: str, rows: list[tuple[str, np.ndarray]]) -> None:
    now = _now()
    data = []
    for unit_id, vec in rows:
        v = np.asarray(vec, dtype=np.float16).reshape(-1)
        data.append((new_id(), unit_id, model, int(v.shape[0]), v.tobytes(), now))
    conn.executemany(
        "insert into ai_embeddings (id, unit_id, model, dim, vector, created_at)"
        " values (?, ?, ?, ?, ?, ?)"
        " on conflict(unit_id, model) do update set dim=excluded.dim,"
        " vector=excluded.vector, created_at=excluded.created_at",
        data,
    )
    conn.commit()


def load_embeddings(
    conn: sqlite3.Connection, model: str, unit_ids: list[str] | None = None
) -> tuple[list[str], np.ndarray]:
    """Return (unit_ids sorted ascending, float32 matrix of shape (n, dim))."""
    rows = conn.execute(
        "select unit_id, vector from ai_embeddings where model = ? order by unit_id", (model,)
    ).fetchall()
    if unit_ids is not None:
        wanted = set(unit_ids)
        rows = [r for r in rows if r["unit_id"] in wanted]
    if not rows:
        return [], np.zeros((0, 0), dtype=np.float32)
    ids = [r["unit_id"] for r in rows]
    mat = np.stack([np.frombuffer(r["vector"], dtype=np.float16) for r in rows]).astype(np.float32)
    return ids, mat


def units_missing_embedding(conn: sqlite3.Connection, model: str) -> list[str]:
    """Photo/RAW units outside trash (primary file present) with no embedding and no error."""
    rows = conn.execute(
        "select u.id from units u join files f on f.id = u.primary_file_id"
        " where u.location != 'trash' and f.kind in ('photo','raw')"
        " and f.missing = 0 and f.purged = 0"
        " and not exists (select 1 from ai_embeddings e where e.unit_id = u.id and e.model = ?)"
        " and not exists (select 1 from ai_errors x where x.unit_id = u.id and x.model = ?)"
        " order by u.id",
        (model, model),
    ).fetchall()
    return [r["id"] for r in rows]


def record_error(conn: sqlite3.Connection, unit_id: str, model: str, error: str) -> None:
    conn.execute(
        "insert into ai_errors (id, unit_id, model, error, created_at) values (?, ?, ?, ?, ?)"
        " on conflict(unit_id, model) do update set error=excluded.error,"
        " created_at=excluded.created_at",
        (new_id(), unit_id, model, error, _now()),
    )
    conn.commit()


def save_labels(
    conn: sqlite3.Connection,
    head: str,
    head_version: str,
    rows: list[tuple[str, str, str | None, float]],
    *,
    model: str = "mobileclip2-s0",
) -> None:
    now = _now()
    conn.executemany(
        "insert into ai_labels (id, unit_id, head, head_version, grp, sub, prob, model, created_at)"
        " values (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " on conflict(unit_id, head) do update set head_version=excluded.head_version,"
        " grp=excluded.grp, sub=excluded.sub, prob=excluded.prob, model=excluded.model,"
        " created_at=excluded.created_at",
        [(new_id(), u, head, head_version, g, s, float(p), model, now) for u, g, s, p in rows],
    )
    conn.commit()


def latest_labels(conn: sqlite3.Connection, head: str) -> dict[str, tuple[str, str | None, float]]:
    rows = conn.execute(
        "select unit_id, grp, sub, prob from ai_labels where head = ?", (head,)
    ).fetchall()
    return {r["unit_id"]: (r["grp"], r["sub"], r["prob"]) for r in rows}


def add_feedback(conn: sqlite3.Connection, unit_id: str, grp: str, source: str, *, commit: bool = True) -> None:
    conn.execute(
        "insert into ai_feedback (id, unit_id, grp, source, created_at) values (?, ?, ?, ?, ?)",
        (new_id(), unit_id, grp, source, _now()),
    )
    if commit:
        conn.commit()


def effective_feedback(conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """unit_id -> (grp, source). Newest correction wins; otherwise newest confirmation."""
    out: dict[str, tuple[str, str]] = {}
    # rowid order is insertion order; timestamps only have 1 s resolution so they can tie.
    for source in ("konfirmasi", "koreksi"):
        for r in conn.execute(
            "select unit_id, grp from ai_feedback where source = ? order by rowid", (source,)
        ):
            out[r["unit_id"]] = (r["grp"], source)
    return out
