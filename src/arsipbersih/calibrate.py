"""Blur threshold suggestion from the owner's own camera photos."""

from __future__ import annotations

import random
import sqlite3

import numpy as np


def suggest_blur_threshold(conn: sqlite3.Connection, sample: int = 200, percentile: float = 5.0) -> float:
    """Percentile of sharpness over a deterministic sample of photos that have a camera make/model."""
    rows = conn.execute(
        "select m.sharpness from units u"
        " join files f on f.id = u.primary_file_id"
        " join media_info m on m.file_id = f.id"
        " where f.kind in ('photo','raw') and m.sharpness is not null"
        " and (m.make is not null or m.model is not null)"
    ).fetchall()
    values = sorted(r[0] for r in rows)  # sorted so the sample does not depend on row order
    if not values:
        return 0.0
    if len(values) > sample:
        values = random.Random(0).sample(values, sample)
    return float(np.percentile(values, percentile))
