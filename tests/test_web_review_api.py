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
    assert r["text"] == "1 delete, 1 keep · 1 belum diputuskan (tetap di review)"
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


def _decisions(root):
    conn = open_db(root)
    try:
        return dict(conn.execute("select unit_id, decision from decisions where applied=0").fetchall())
    finally:
        conn.close()


def test_bulk_discard_records_only_undecided(web, root, review_units):
    a, b, c = review_units(3, category="kiriman-chat")
    assert web.post("/api/review/decision", json={"unit_id": a, "decision": "keep"}).status_code == 200
    r = web.post("/api/review/decisions", json={"unit_ids": [a, b, c], "decision": "discard"})
    assert r.status_code == 200
    assert r.json() == {"recorded": 2}
    assert _decisions(root) == {a: "keep", b: "discard", c: "discard"}


def test_bulk_discard_skips_group_categories(web, root, review_units):
    (grid_unit,) = review_units(1, category="kiriman-chat")
    (group_unit,) = review_units(1, category="duplikat-persis")
    r = web.post("/api/review/decisions", json={"unit_ids": [grid_unit, group_unit], "decision": "discard"})
    assert r.json() == {"recorded": 1}
    assert _decisions(root) == {grid_unit: "discard"}


def test_bulk_discard_validation(web, root, review_units):
    (a,) = review_units(1, category="kiriman-chat")
    r = web.post("/api/review/decisions", json={"unit_ids": ["x"] * 501, "decision": "discard"})
    assert r.status_code == 400
    assert "Maksimal 500 item per permintaan" in r.text
    assert web.post("/api/review/decisions", json={"unit_ids": [a], "decision": "keep"}).status_code == 400
    assert web.post("/api/review/decisions", json={"unit_ids": [a]}).status_code in (400, 422)
    assert web.post("/api/review/decisions", json={"unit_ids": [a], "decision": "discard"},
                    headers={"X-ArsipBersih": ""}).status_code == 403
    assert _decisions(root) == {}


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


def test_grid_items_carry_the_primary_file_name(web, review_units):
    a, b = review_units(2)
    items = web.post("/api/review/grid/burem/items", json={"unit_ids": [a, b]}).json()["items"]
    assert [i["name"] for i in items] == ["IMG_0.jpg", "IMG_1.jpg"]
    page = web.get("/api/review/grid/burem").json()["items"]
    assert sorted(i["name"] for i in page) == ["IMG_0.jpg", "IMG_1.jpg"]


# --- AI guesses (Fase 1.5) ---------------------------------------------------------------


def _label(root, uid, grp, sub, prob, head="zeroshot"):
    from arsipbersih.ai.store import save_labels

    conn = open_db(root)
    try:
        save_labels(conn, head, "v1", [(uid, grp, sub, prob)])
    finally:
        conn.close()


def _feedback(root):
    conn = open_db(root)
    try:
        return [tuple(r) for r in conn.execute("select unit_id, grp, source from ai_feedback order by rowid")]
    finally:
        conn.close()


def test_ai_group_correction_moves_unit_category(web, root, review_units):
    a, other = review_units(2, category="download-meme")
    conn = open_db(root)
    conn.execute(
        "insert into flags(id, unit_id, category, score, reason) values(?,?,?,?,?)",
        (new_id(), other, "download-meme", None, "berasal dari folder unduhan"),
    )
    conn.commit()
    conn.close()
    _label(root, a, "grafis", None, 0.8)
    r = web.post("/api/review/ai-group", json={"unit_id": a, "grp": "keuangan"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["primary_category"] == "dokumen-keuangan"
    assert body["ai"]["source"] == "koreksi" and body["ai"]["grp"] == "keuangan"
    assert _feedback(root) == [(a, "keuangan", "koreksi")]
    assert web.get("/api/review/grid/download-meme/order").json()["unit_ids"] == [other]
    assert web.get("/api/review/grid/dokumen-keuangan/order").json()["unit_ids"] == [a]
    # no file moved, the other unit untouched
    assert (root / "_review/download-meme/HP/IMG_0.jpg").exists()
    assert _db_row(root, a) == "review"
    conn = open_db(root)
    try:
        assert conn.execute("select primary_category from units where id=?", (other,)).fetchone()[0] == "download-meme"
        assert [tuple(x) for x in conn.execute("select category, reason from flags where unit_id=?", (other,))] == [
            ("download-meme", "berasal dari folder unduhan")]
    finally:
        conn.close()
    # "Foto biasa" removes the AI flag; nothing else flags the unit
    r = web.post("/api/review/ai-group", json={"unit_id": a, "grp": "lainnya"})
    assert r.status_code == 200
    assert r.json()["primary_category"] is None


def test_ai_group_rejects_unknown_group(web, root, review_units):
    (a,) = review_units(1, category="screenshot")
    assert web.post("/api/review/ai-group", json={"unit_id": a, "grp": "kucing"}).status_code == 400
    assert web.post("/api/review/ai-group", json={"unit_id": "nope", "grp": "layar"}).status_code == 404
    assert web.post("/api/review/ai-group", json={"unit_id": a, "grp": "layar"},
                    headers={"X-ArsipBersih": ""}).status_code == 403
    assert _feedback(root) == []
    gate = threading.Event()
    web.app_ref.state.jobs.start("scan", "Scan", lambda report, stop: gate.wait(10))
    try:
        r = web.post("/api/review/ai-group", json={"unit_id": a, "grp": "layar"})
        assert r.status_code == 409
    finally:
        gate.set()
        web.app_ref.state.jobs.wait()
    assert _feedback(root) == []


def test_keep_in_document_category_records_confirmation(web, root, review_units):
    doc, doc2 = review_units(2, category="dokumen-keuangan")
    (blur,) = review_units(1, category="burem")
    assert web.post("/api/review/decision", json={"unit_id": doc, "decision": "keep"}).status_code == 200
    assert web.post("/api/review/decision", json={"unit_id": doc2, "decision": "discard"}).status_code == 200
    assert web.post("/api/review/decision", json={"unit_id": blur, "decision": "keep"}).status_code == 200
    assert _feedback(root) == [(doc, "keuangan", "konfirmasi")]


def test_grid_order_ai_doubt(web, root, review_units):
    a, b, c, d = review_units(4, category="dokumen-keuangan")
    _label(root, a, "keuangan", "struk", 0.95)  # 0.25 from 0.7
    _label(root, b, "keuangan", "struk", 0.72)  # 0.02 from 0.7
    _label(root, c, "identitas", "KTP", 0.96)  # 0.01 from 0.95
    r = web.get("/api/review/grid/dokumen-keuangan/order?sort=ai_doubt")
    assert r.status_code == 200
    assert r.json()["unit_ids"] == [c, b, a, d]
    assert web.get("/api/review/grid/dokumen-keuangan/order?sort=ragu").status_code == 400


def test_unit_detail_has_ai_guess(web, root, review_units):
    a, b = review_units(2, category="dokumen-keuangan")
    _label(root, a, "keuangan", "struk", 0.91)
    ai = web.get(f"/api/review/unit/{a}").json()["ai"]
    assert ai == {"grp": "keuangan", "sub": "struk", "prob": 0.91, "source": "ai", "label": "Keuangan"}
    assert web.get(f"/api/review/unit/{b}").json()["ai"] is None
    web.post("/api/review/ai-group", json={"unit_id": a, "grp": "kertas"})
    ai = web.get(f"/api/review/unit/{a}").json()["ai"]
    assert ai == {"grp": "kertas", "sub": None, "prob": None, "source": "koreksi", "label": "Kertas & catatan"}


def test_bulk_discard_skips_identity_documents(web, root, review_units):
    (ident,) = review_units(1, category="dokumen-identitas")
    (money,) = review_units(1, category="dokumen-keuangan")
    r = web.post("/api/review/decisions", json={"unit_ids": [ident, money], "decision": "discard"})
    assert r.json() == {"recorded": 1}
    assert _decisions(root) == {money: "discard"}


def _unit_state(root, uid):
    conn = open_db(root)
    try:
        seen = conn.execute("select seen from units where id=?", (uid,)).fetchone()[0]
        d = conn.execute("select decision from decisions where unit_id=?", (uid,)).fetchone()
        return seen, d[0] if d else None
    finally:
        conn.close()


def test_ai_group_move_resets_seen_so_apply_does_not_discard(web, root, review_units):
    (a,) = review_units(1, category="download-meme", seen={0})
    r = web.post("/api/review/ai-group", json={"unit_id": a, "grp": "identitas"})
    assert r.json()["primary_category"] == "dokumen-identitas"
    assert _unit_state(root, a) == (0, None)
    p = web.get("/api/review/apply/dokumen-identitas").json()
    assert (p["keep"], p["discard"], p["unseen"]) == (0, 0, 1)


def test_ai_group_move_clears_pending_discard_keeps_keep(web, root, review_units):
    a, b = review_units(2, category="download-meme")
    web.post("/api/review/decisions", json={"unit_ids": [a], "decision": "discard"})
    web.post("/api/review/decision", json={"unit_id": b, "decision": "keep"})
    web.post("/api/review/ai-group", json={"unit_id": a, "grp": "identitas"})
    web.post("/api/review/ai-group", json={"unit_id": b, "grp": "identitas"})
    assert _unit_state(root, a) == (0, None)
    assert _unit_state(root, b) == (0, "keep")
    p = web.get("/api/review/apply/dokumen-identitas").json()
    assert (p["keep"], p["discard"], p["unseen"]) == (1, 0, 1)


def test_ai_group_same_category_leaves_seen_and_decision(web, root, review_units):
    (a,) = review_units(1, category="dokumen-keuangan", seen={0})
    _label(root, a, "keuangan", "struk", 0.9)
    web.post("/api/review/decision", json={"unit_id": a, "decision": "discard"})
    r = web.post("/api/review/ai-group", json={"unit_id": a, "grp": "keuangan"})
    assert r.json()["primary_category"] == "dokumen-keuangan"
    assert _unit_state(root, a) == (1, "discard")
