from __future__ import annotations

import sqlite3

import numpy as np

from arsipbersih.ai import store
from arsipbersih.db import SCHEMA, open_db

M = "mobileclip2-s0"


def _unit(conn, uid, kind="photo", location="source", missing=0):
    fid = "f-" + uid
    conn.execute("insert into units (id, primary_file_id, location) values (?, ?, ?)", (uid, fid, location))
    conn.execute(
        "insert into files (id, unit_id, rel_path, kind, missing) values (?, ?, ?, ?, ?)",
        (fid, uid, uid + ".x", kind, missing),
    )
    conn.commit()


def test_embeddings_roundtrip_float16(tmp_path):
    conn = open_db(tmp_path)
    for u in ("c", "a", "b"):
        _unit(conn, u)
    rng = np.random.default_rng(0)
    vecs = {u: rng.uniform(-1, 1, size=8).astype(np.float32) for u in ("c", "a", "b")}
    store.save_embeddings(conn, M, list(vecs.items()))
    ids, mat = store.load_embeddings(conn, M)
    assert ids == ["a", "b", "c"]
    assert mat.dtype == np.float32
    for i, u in enumerate(ids):
        assert np.allclose(mat[i], vecs[u], atol=1e-3)
    ids2, mat2 = store.load_embeddings(conn, M, ["b"])
    assert ids2 == ["b"] and mat2.shape == (1, 8)


def test_units_missing_embedding_skips_videos_trash_and_errors(tmp_path):
    conn = open_db(tmp_path)
    _unit(conn, "photo-ok")
    _unit(conn, "raw-ok", kind="raw")
    _unit(conn, "photo-done")
    _unit(conn, "photo-err")
    _unit(conn, "vid", kind="video")
    _unit(conn, "trashed", location="trash")
    _unit(conn, "gone", missing=1)
    store.save_embeddings(conn, M, [("photo-done", np.ones(4))])
    store.record_error(conn, "photo-err", M, "boom")
    assert store.units_missing_embedding(conn, M) == ["photo-ok", "raw-ok"]
    assert "photo-done" in store.units_missing_embedding(conn, "other-model")


def test_labels_upsert(tmp_path):
    conn = open_db(tmp_path)
    _unit(conn, "a")
    store.save_labels(conn, "zeroshot", "v1", [("a", "kertas", "nota", 0.8)])
    store.save_labels(conn, "zeroshot", "v2", [("a", "lainnya", None, 0.9)])
    assert store.latest_labels(conn, "zeroshot") == {"a": ("lainnya", None, 0.9)}
    assert conn.execute("select model from ai_labels").fetchone()[0] == M


def test_effective_feedback_correction_beats_newer_confirmation(tmp_path):
    conn = open_db(tmp_path)
    for u in ("a", "b", "c"):
        _unit(conn, u)
    store.add_feedback(conn, "a", "kertas", "koreksi")
    store.add_feedback(conn, "a", "layar", "konfirmasi")
    store.add_feedback(conn, "b", "grafis", "konfirmasi")
    store.add_feedback(conn, "b", "layar", "konfirmasi")
    store.add_feedback(conn, "c", "kertas", "koreksi")
    store.add_feedback(conn, "c", "lainnya", "koreksi")
    assert store.effective_feedback(conn) == {
        "a": ("kertas", "koreksi"),
        "b": ("layar", "konfirmasi"),
        "c": ("lainnya", "koreksi"),
    }


def test_old_db_gets_ai_tables(tmp_path):
    state = tmp_path / ".arsipbersih"
    state.mkdir()
    old = SCHEMA.split("create table if not exists ai_embeddings")[0]
    c = sqlite3.connect(state / "arsip.db")
    c.executescript(old)
    c.execute("pragma user_version=1")
    c.commit()
    c.close()
    conn = open_db(tmp_path)
    names = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert {"ai_embeddings", "ai_errors", "ai_labels", "ai_feedback"} <= names
