from __future__ import annotations

import threading

import numpy as np
import pytest

from arsipbersih import visual
from arsipbersih.ai import stage
from arsipbersih.ai.model import MODEL_NAME
from arsipbersih.ai.store import load_embeddings
from arsipbersih.config import Settings
from arsipbersih.db import open_db
from arsipbersih.inventory import run_inventory
from arsipbersih.pairing import build_units
from arsipbersih.pipeline import SCAN_DONE_KEY, run_scan
from arsipbersih.videostage import run_video_backfill
from conftest import requires_ffmpeg
from test_ai_stage import FakeEncoder, InlinePool, ai_ready  # noqa: F401 (fixture)

pytestmark = requires_ffmpeg


def _old_videos(root, make_video, monkeypatch, n):
    """n videos that went through an older scan: visual done, no frame data."""
    for i in range(n):
        make_video(root / f"Sumber/HP/v{i}.mp4", seconds=2)
    monkeypatch.setattr(visual, "_pool", InlinePool)
    conn = open_db(root)
    run_inventory(conn, root)
    build_units(conn)
    visual.run_visual(conn, root, Settings())
    conn.execute("delete from video_frames")
    conn.commit()
    return conn


def _frames(conn) -> int:
    return conn.execute("select count(*) from video_frames").fetchone()[0]


def test_backfill_fills_old_videos_once(root, make_video, monkeypatch):
    conn = _old_videos(root, make_video, monkeypatch, 2)
    r = run_video_backfill(conn, root, Settings())
    assert (r.done, r.errors) == (2, 0) and _frames(conn) == 2
    assert run_video_backfill(conn, root, Settings()).done == 0


def test_backfill_resumes_after_stop(root, make_video, monkeypatch):
    conn = _old_videos(root, make_video, monkeypatch, 3)
    stop = threading.Event()
    r1 = run_video_backfill(conn, root, Settings(), progress=lambda d, t: stop.set() if d else None,
                            stop=stop)
    assert r1.done == 2 and _frames(conn) == 2  # the finished chunk is kept
    assert run_video_backfill(conn, root, Settings()).done == 1 and _frames(conn) == 3


def test_failed_video_is_recorded_and_not_retried(root, make_video, monkeypatch):
    conn = _old_videos(root, make_video, monkeypatch, 2)
    (root / "Sumber/HP/v0.mp4").write_bytes(b"not a video")
    r = run_video_backfill(conn, root, Settings())
    assert (r.done, r.errors) == (2, 1) and _frames(conn) == 1
    assert conn.execute("select count(*) from files where error like 'video: %'").fetchone()[0] == 1
    assert run_video_backfill(conn, root, Settings()).done == 0


def test_stopped_backfill_keeps_scan_unfinished(root, make_video):
    make_video(root / "Sumber/HP/v.mp4", seconds=2)
    assert not run_scan(root).stopped
    conn = open_db(root)
    conn.execute("delete from video_frames")
    conn.commit()
    stop = threading.Event()
    s = run_scan(root, progress=lambda p: stop.set() if p.stage == "video" else None, stop=stop)
    assert s.stopped and _frames(conn) == 0
    assert not conn.execute("select 1 from settings where key = ?", (SCAN_DONE_KEY,)).fetchone()
    assert not run_scan(root).stopped and _frames(conn) == 1


def test_new_video_is_decoded_once(root, make_video, ai_ready, monkeypatch):  # noqa: F811
    make_video(root / "Sumber/HP/v.mp4", seconds=2)
    calls = []
    real = visual.extract
    monkeypatch.setattr(visual, "extract", lambda *a, **k: calls.append(a[1]) or real(*a, **k))
    assert not run_scan(root).stopped
    assert len(calls) == 1
    conn = open_db(root)
    assert _frames(conn) == 1
    assert len(load_embeddings(conn, MODEL_NAME)[0]) == 1  # embedded from that same decode


def test_video_embedding_is_mean_of_frames(conn_with):
    conn = conn_with([{"name": "v.mp4"}])
    unit_id, primary = conn.execute("select id, primary_file_id from units").fetchone()
    enc = FakeEncoder()
    col = stage.AiCollector(enc, conn, MODEL_NAME, batch=16)
    crops = [np.full((256, 256, 3), v, np.uint8) for v in (20, 120, 220)]
    col.add_many(primary, [c.tobytes() for c in crops])
    col.flush()
    assert enc.batches == [3]
    each = enc.embed(np.stack(crops))
    want = each.mean(axis=0)
    want /= np.linalg.norm(want)
    ids, mat = load_embeddings(conn, MODEL_NAME)
    assert ids == [unit_id] and np.allclose(mat[0], want, atol=1e-3)


def test_ai_stage_backfills_video_units_with_frames(root, make_video, monkeypatch, ai_ready):  # noqa: F811
    conn = _old_videos(root, make_video, monkeypatch, 1)
    run_video_backfill(conn, root, Settings())  # frames, but no model then: no embedding yet
    assert not load_embeddings(conn, MODEL_NAME)[0]
    r = stage.run_ai(conn, root, Settings(), encoder_factory=FakeEncoder)
    assert (r.embedded, r.errors) == (1, 0)
    assert len(load_embeddings(conn, MODEL_NAME)[0]) == 1
