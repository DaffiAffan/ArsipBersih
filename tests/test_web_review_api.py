from __future__ import annotations

import os
import threading

import pytest

from arsipbersih.db import new_id, open_db


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
def review_units(root):
    def make(n, category="burem", seen=()):
        conn = open_db(root)
        try:
            return [
                _add_unit(conn, root, f"_review/{category}/HP/IMG_{i}.jpg", location="review",
                          category=category, seen=1 if i in seen else 0)
                for i in range(n)
            ]
        finally:
            conn.close()

    return make


def _db_row(root, uid):
    conn = open_db(root)
    try:
        return conn.execute("select location from units where id=?", (uid,)).fetchone()[0]
    finally:
        conn.close()


def test_grid_apply_never_discards_unseen(web, root, review_units):
    a, b, c = review_units(3, seen={0})
    assert web.post("/api/review/decision", json={"unit_id": b, "decision": "keep"}).status_code == 200
    r = web.get("/api/review/apply/burem").json()
    assert (r["keep"], r["discard"], r["unseen"]) == (1, 1, 1)
    assert r["text"] == "2 sudah dilihat: 1 delete, 1 keep · 1 belum dilihat (tetap di review)"
    assert web.post("/api/review/apply/burem", json={"keep": 1, "discard": 1}).status_code == 202
    state = web.app_ref.state.jobs.wait()
    assert state.status == "done", state.error
    assert _db_row(root, a) == "trash"
    assert _db_row(root, b) == "archive"
    assert _db_row(root, c) == "review"
    assert (root / "_review/burem/HP/IMG_2.jpg").exists()


def test_decision_null_clears(web, root, review_units):
    (u,) = review_units(1)
    web.post("/api/review/decision", json={"unit_id": u, "decision": "discard"})
    conn = open_db(root)
    assert conn.execute("select count(*) from decisions").fetchone()[0] == 1
    assert web.post("/api/review/decision", json={"unit_id": u, "decision": None}).status_code == 200
    assert conn.execute("select count(*) from decisions").fetchone()[0] == 0
    conn.close()
    r = web.post("/api/review/decision", json={"unit_id": "nope", "decision": "keep"})
    assert r.status_code == 404
    assert web.post("/api/review/decision", json={"unit_id": u, "decision": "x"}).status_code == 400


def test_seen_rejects_huge_batches(web, review_units, root):
    assert web.post("/api/review/seen", json={"unit_ids": ["x"] * 1001}).status_code == 400
    (u,) = review_units(1)
    assert web.post("/api/review/seen", json={"unit_ids": [u]}).status_code == 200
    conn = open_db(root)
    assert conn.execute("select seen from units where id=?", (u,)).fetchone()[0] == 1
    conn.close()


def test_apply_conflicts_while_job_running(web, review_units):
    (u,) = review_units(1)
    web.post("/api/review/decision", json={"unit_id": u, "decision": "keep"})
    gate = threading.Event()
    web.app_ref.state.jobs.start("scan", "Scan", lambda report, stop: gate.wait(10))
    try:
        r = web.post("/api/review/apply/burem", json={"keep": 1, "discard": 0})
        assert r.status_code == 409
        assert "Masih ada proses yang berjalan" in r.json()["detail"]
    finally:
        gate.set()
        web.app_ref.state.jobs.wait()


def test_apply_validation(web, review_units):
    body = {"keep": 0, "discard": 0}
    assert web.post("/api/review/apply/nope", json=body).status_code == 400
    assert web.get("/api/review/apply/nope").status_code == 400
    r = web.post("/api/review/apply/burem", json=body)
    assert r.status_code == 400
    assert r.json()["detail"] == "Belum ada keputusan untuk di-apply"


def test_apply_refuses_changed_counts(web, root, review_units):
    a, b = review_units(2, seen={0, 1})
    web.post("/api/review/decision", json={"unit_id": b, "decision": "keep"})
    r = web.post("/api/review/apply/burem", json={"keep": 0, "discard": 2})
    assert r.status_code == 409
    assert r.json()["detail"] == "Jumlah berubah sejak kamu konfirmasi. Muat ulang, lalu periksa lagi."
    assert web.get("/api/job").json()["status"] == "idle"
    assert _db_row(root, a) == "review"
    assert _db_row(root, b) == "review"
    assert (root / "_review/burem/HP/IMG_0.jpg").exists()
    assert (root / "_review/burem/HP/IMG_1.jpg").exists()


def test_apply_requires_valid_body(web, review_units):
    (u,) = review_units(1, seen={0})
    assert web.post("/api/review/apply/burem").status_code == 422
    assert web.post("/api/review/apply/burem", json={"keep": 0}).status_code == 422
    assert web.post("/api/review/apply/burem", json={"keep": -1, "discard": 1}).status_code == 422
    assert web.post("/api/review/apply/burem", json={"keep": "0", "discard": 1}).status_code == 422


def test_query_param_validation(web):
    assert web.get("/api/review/grid/burem?sort=zzz").status_code == 400
    assert web.get("/api/review/grid/burem?offset=-1").status_code == 400
    assert web.get("/api/review/grid/duplikat-persis").status_code == 400
    assert web.get("/api/review/groups/burem").status_code == 400
    assert web.get("/api/review/groups/duplikat-persis?page=0").status_code == 400
    assert web.get("/api/review/group/nope").status_code == 404
    assert web.get("/api/review/unit/nope").status_code == 404
    assert web.get("/api/review/categories").status_code == 200
    assert web.get("/api/review/grid/burem?sort=date").json()["total"] == 0


def test_accept_page_records_suggestions(web, root):
    conn = open_db(root)
    keeper = _add_unit(conn, root, "Arsip/2024/2024-01/K.jpg", location="archive")
    other = _add_unit(conn, root, "_review/duplikat-persis/HP/O.jpg", location="review")
    gid = new_id()
    conn.execute("insert into groups(id, kind, suggested_keep_unit_id) values(?,?,?)",
                 (gid, "duplikat-persis", keeper))
    conn.executemany("insert into group_members(group_id, unit_id) values(?,?)",
                     [(gid, keeper), (gid, other)])
    conn.commit()
    conn.close()
    assert web.post("/api/review/accept", json={"group_ids": [gid]}).json() == {"recorded": 2}
    assert web.get("/api/review/apply/duplikat-persis").json()["text"] == "1 keep, 1 delete"
    assert web.get(f"/api/review/group/{gid}").status_code == 200


def test_apply_job_rechecks_counts_inside_job(web, root, review_units):
    from arsipbersih.web.routes.review import _apply_job

    a, b = review_units(2, seen={0, 1})
    # Route check would pass with (0, 2); state then changes before the job runs.
    web.post("/api/review/decision", json={"unit_id": b, "decision": "keep"})
    web.app_ref.state.jobs.start("apply", "Apply Blur", _apply_job(root, "burem", 0, 2))
    state = web.app_ref.state.jobs.wait()
    assert state.status == "failed"
    assert "Jumlah berubah sejak kamu konfirmasi. Muat ulang, lalu periksa lagi." in state.error
    assert _db_row(root, a) == "review"
    assert _db_row(root, b) == "review"
    assert (root / "_review/burem/HP/IMG_0.jpg").exists()
    assert (root / "_review/burem/HP/IMG_1.jpg").exists()


def test_unseen_snapshot_does_not_skip_when_items_become_seen(web, review_units):
    ids = review_units(5)
    order = web.get("/api/review/grid/burem/order?view=unseen").json()
    assert order["total"] == 5 and set(order["unit_ids"]) == set(ids)
    first = order["unit_ids"][:2]
    web.post("/api/review/seen", json={"unit_ids": first})
    rest = web.post("/api/review/grid/burem/items", json={"unit_ids": order["unit_ids"][2:]}).json()
    assert [i["unit_id"] for i in rest["items"]] == order["unit_ids"][2:]
    again = web.get("/api/review/grid/burem/order?view=unseen").json()
    assert again["total"] == 3


def test_keep_view_and_items_skip_units_no_longer_in_category(web, review_units):
    a, b, c = review_units(3)
    web.post("/api/review/decision", json={"unit_id": b, "decision": "keep"})
    assert web.get("/api/review/grid/burem/order?view=keep").json()["unit_ids"] == [b]
    items = web.post("/api/review/grid/burem/items", json={"unit_ids": [a, "nope", c]}).json()["items"]
    assert [i["unit_id"] for i in items] == [a, c]


def test_grid_items_rejects_oversized_and_bad_view(web, review_units):
    review_units(1)
    assert web.post("/api/review/grid/burem/items", json={"unit_ids": ["x"] * 501}).status_code == 400
    assert web.get("/api/review/grid/burem/order?view=semua").status_code == 400
