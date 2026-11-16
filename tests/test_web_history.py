from __future__ import annotations

import os

import pytest

from arsipbersih.db import new_id, open_db
from arsipbersih.decisions import apply_category, mark_seen, preview_apply


def _add_unit(conn, root, rel, *, location, category=None, seen=0):
    uid, fid = new_id(), new_id()
    p = root / rel
    os.makedirs(p.parent, exist_ok=True)
    p.write_bytes(b"x" * 10)
    conn.execute(
        "insert into units(id, primary_file_id, location, primary_category, planned_archive_dir, seen)"
        " values(?,?,?,?,?,?)",
        (uid, fid, location, category, "Arsip/2024/2024-01", seen),
    )
    conn.execute(
        "insert into files(id, unit_id, rel_path, orig_rel_path, size, kind, ext, source_label)"
        " values(?,?,?,?,?,?,?,?)",
        (fid, uid, rel, rel, 10, "photo", "jpg", "HP"),
    )
    conn.commit()
    return uid


@pytest.fixture
def trashed_units(root):
    conn = open_db(root)
    try:
        return [
            _add_unit(conn, root, f"_sampah/burem/HP/IMG_{i}.jpg", location="trash", category="burem")
            for i in range(3)
        ]
    finally:
        conn.close()


@pytest.fixture
def applied_units(root):
    """Three seen review units discarded by a real apply batch (files now in _sampah)."""
    conn = open_db(root)
    try:
        ids = [
            _add_unit(conn, root, f"_review/burem/HP/IMG_{i}.jpg", location="review", category="burem")
            for i in range(3)
        ]
        mark_seen(conn, ids)
        apply_category(conn, root, "burem")
        assert {r[0] for r in conn.execute("select location from units")} == {"trash"}
        return ids
    finally:
        conn.close()


def _finish(web):
    return web.app_ref.state.jobs.wait()


def _trash_files(root):
    d = root / "_sampah"
    return [p for p in d.rglob("*") if p.is_file()] if d.exists() else []


def test_history_lists_batches_and_trash(web, root, applied_units):
    h = web.get("/api/history").json()
    assert h["trash"] == {"files": 3, "bytes": 30}
    assert [b["kind"] for b in h["batches"]] == ["apply"]
    assert h["batches"][0]["undoable"] is True
    assert h["pending"] == [] and h["missing"] == []


def test_history_page_renders(web):
    r = web.get("/riwayat")
    assert r.status_code == 200
    assert "Riwayat" in r.text and "/static/history.js" in r.text


def test_purge_requires_typed_confirmation(web, trashed_units):
    files, size = web.get("/api/history").json()["trash"].values()
    r = web.post("/api/trash/purge", json={"expected_files": files, "expected_bytes": size, "confirm": "ya"})
    assert r.status_code == 400
    r = web.post("/api/trash/purge", json={"expected_files": files, "expected_bytes": size})
    assert r.status_code == 400
    assert web.get("/api/job").json()["status"] == "idle"


def test_purge_deletes_with_matching_totals(web, root, trashed_units):
    files, size = web.get("/api/history").json()["trash"].values()
    r = web.post("/api/trash/purge", json={"expected_files": files, "expected_bytes": size, "confirm": "HAPUS"})
    assert r.status_code == 202
    st = _finish(web)
    assert st.status == "done" and st.result == {"deleted": 3, "errors": []}
    assert _trash_files(root) == []
    assert web.get("/api/history").json()["trash"] == {"files": 0, "bytes": 0}


def test_purge_with_stale_totals_fails_safely(web, root, trashed_units):
    files, size = web.get("/api/history").json()["trash"].values()
    r = web.post("/api/trash/purge", json={"expected_files": files + 1, "expected_bytes": size, "confirm": "HAPUS"})
    assert r.status_code == 202
    st = _finish(web)
    assert st.status == "failed"
    assert st.error.startswith("Isi _sampah berubah")
    assert len(_trash_files(root)) == 3


def test_undo_requires_exactly_one_target(web, applied_units):
    assert web.post("/api/undo", json={}).status_code == 400
    assert web.post("/api/undo", json={"batch_id": "x", "all": True}).status_code == 400


def test_undo_all_job_restores(web, root, applied_units):
    r = web.post("/api/undo", json={"all": True})
    assert r.status_code == 202 and r.json()["label"] == "Kembalikan semua"
    st = _finish(web)
    assert st.status == "done" and st.result["moved_units"] == 3 and st.result["failed_units"] == 0
    conn = open_db(root)
    try:
        assert {r[0] for r in conn.execute("select location from units")} == {"review"}
        p = preview_apply(conn, "burem")
        assert (p.keep, p.discard, p.unseen) == (0, 0, 3)
    finally:
        conn.close()
    assert _trash_files(root) == []
    assert len(list((root / "_review").rglob("*.jpg"))) == 3


def test_undo_one_batch_job(web, root, applied_units):
    batch = web.get("/api/history").json()["batches"][0]
    r = web.post("/api/undo", json={"batch_id": batch["id"]})
    assert r.status_code == 202 and r.json()["label"] == "Kembalikan batch"
    assert _finish(web).status == "done"
    assert web.get("/api/history").json()["trash"]["files"] == 0


def test_undo_unknown_batch_fails_with_message(web, applied_units):
    web.post("/api/undo", json={"batch_id": "nope"})
    st = _finish(web)
    assert st.status == "failed" and st.error == "Batch tidak ditemukan."


def _pending(conn, root):
    (root / "Sumber" / "HP" / "DCIM").mkdir(parents=True, exist_ok=True)
    (root / "Arsip" / "2024" / "2024-01").mkdir(parents=True, exist_ok=True)
    fid, uid = conn.execute("select id, unit_id from files").fetchone()
    bid, rid = new_id(), new_id()
    conn.execute("insert into batches(id, kind, label, created_at) values(?, 'rapikan', 't', 'now')", (bid,))
    conn.execute(
        "insert into moves(id, batch_id, unit_id, file_id, src_rel, dst_rel, status)"
        " values(?,?,?,?,?,?, 'pending')",
        (rid, bid, uid, fid, "Sumber/HP/DCIM/IMG_1.jpg", "Arsip/2024/2024-01/IMG_1.jpg"),
    )
    conn.commit()
    return rid, fid


def test_release_endpoint_unblocks(web, root, conn_with):
    conn = conn_with([{"name": "IMG_1.jpg"}])
    rid, fid = _pending(conn, root)
    h = web.get("/api/history").json()
    assert [p["row_id"] for p in h["pending"]] == [rid]
    # While the row is pending, an undo refuses to run.
    web.post("/api/undo", json={"all": True})
    assert _finish(web).status == "failed"
    r = web.post("/api/journal/release", json={"row_id": rid, "deleted_on_purpose": False})
    assert r.status_code == 202
    st = _finish(web)
    assert st.status == "done" and st.result == {"ok": True}
    h = web.get("/api/history").json()
    assert h["pending"] == []
    assert [m["file_id"] for m in h["missing"]] == [fid]
    web.post("/api/undo", json={"all": True})
    assert _finish(web).status == "done"


def test_release_refuses_while_file_on_disk(web, root, conn_with):
    conn = conn_with([{"name": "IMG_1.jpg"}])
    rid, _ = _pending(conn, root)
    part = root / "Sumber" / "HP" / "DCIM" / "IMG_1.jpg.partial"
    part.write_bytes(b"x")
    web.post("/api/journal/release", json={"row_id": rid, "deleted_on_purpose": True})
    st = _finish(web)
    assert st.status == "failed" and "masih ada di disk" in st.error
    assert len(web.get("/api/history").json()["pending"]) == 1


def test_forget_missing_file(web, root, conn_with):
    conn = conn_with([{"name": "IMG_3.jpg"}])
    fid = conn.execute("select id from files").fetchone()[0]
    conn.execute("update files set missing=1")
    conn.commit()
    (root / "Sumber" / "HP" / "DCIM").mkdir(parents=True, exist_ok=True)
    assert [m["file_id"] for m in web.get("/api/history").json()["missing"]] == [fid]
    r = web.post("/api/files/forget", json={"file_id": fid})
    assert r.status_code == 202
    st = _finish(web)
    assert st.status == "done" and st.result == {"ok": True}
    assert web.get("/api/history").json()["missing"] == []


@pytest.mark.parametrize("bad", [-1, "3", 3.5, None])
def test_purge_rejects_bad_totals(web, trashed_units, bad):
    r = web.post("/api/trash/purge", json={"expected_files": bad, "expected_bytes": 30, "confirm": "HAPUS"})
    assert 400 <= r.status_code < 500
    r = web.post("/api/trash/purge", json={"expected_files": 3, "expected_bytes": bad, "confirm": "HAPUS"})
    assert 400 <= r.status_code < 500
    assert web.get("/api/job").json()["status"] == "idle"


def test_forget_unknown_file_is_404(web):
    r = web.post("/api/files/forget", json={"file_id": "nope"})
    assert r.status_code == 404 and r.json()["detail"] == "File tidak ditemukan"
