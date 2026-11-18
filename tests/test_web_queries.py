from __future__ import annotations

import json

from arsipbersih import decisions
from arsipbersih.db import new_id
from arsipbersih.web import queries


def _unit(conn, name):
    return conn.execute(
        "select u.id uid, u.primary_file_id fid from units u join files f on f.id=u.primary_file_id"
        " where f.rel_path like ?",
        (f"%/{name}",),
    ).fetchone()


def _group(conn, kind, keeper, members):
    gid = new_id()
    conn.execute(
        "insert into groups(id, kind, suggested_keep_unit_id) values(?,?,?)", (gid, kind, keeper)
    )
    for m in members:
        conn.execute("insert into group_members(group_id, unit_id) values(?,?)", (gid, m))
    conn.commit()
    return gid


def test_counts_hide_empty_and_follow_priority(conn_with):
    conn = conn_with([
        {"name": "a.jpg", "loc": "review", "category": "burem"},
        {"name": "b.jpg", "loc": "review", "category": "burem"},
        {"name": "c.jpg", "loc": "review", "category": "rusak"},
    ])
    got = queries.category_counts(conn)
    assert [(c["category"], c["count"]) for c in got] == [("rusak", 1), ("burem", 2)]
    assert got[0]["mode"] == "grid"
    json.dumps(got)


def test_group_with_archived_old_member_stays_open(conn_with):
    conn = conn_with([
        {"name": "new.jpg", "loc": "review", "taken": "2024-01-01T10:00:00"},
        {"name": "old.jpg", "loc": "archive", "taken": "2024-01-01T10:00:00"},
    ])
    new, old = _unit(conn, "new.jpg"), _unit(conn, "old.jpg")
    _group(conn, "duplikat-versi", new["uid"], [new["uid"], old["uid"]])
    assert queries.list_groups(conn, "duplikat-versi", 1)["total"] == 1
    # keeper gets archived too, old member's keep decision is applied -> group closes
    decisions.record(conn, old["uid"], "keep")
    conn.execute("update decisions set applied=1 where unit_id=?", (old["uid"],))
    conn.execute("update units set location='archive' where id=?", (new["uid"],))
    conn.commit()
    assert queries.list_groups(conn, "duplikat-versi", 1)["total"] == 0


def test_group_summary_counts_seconds(conn_with):
    times = ["2024-01-01T10:00:00", "2024-01-01T10:00:03", "2024-01-01T10:00:06"]
    conn = conn_with([
        {"name": f"{i}.jpg", "loc": "review", "taken": t} for i, t in enumerate(times)
    ])
    ids = [_unit(conn, f"{i}.jpg")["uid"] for i in range(3)]
    _group(conn, "mirip-beruntun", ids[0], ids)
    res = queries.list_groups(conn, "mirip-beruntun", 1)
    g = res["groups"][0]
    assert g["summary"] == "3 foto dalam 6 detik"
    assert g["span_s"] == 6 and g["members"][0]["is_keeper"]
    assert queries.group_detail(conn, g["id"])["members"][0]["unit_id"] == ids[0]
    json.dumps(res)


def test_grid_sort_by_score_puts_lowest_first(conn_with):
    conn = conn_with([
        {"name": "a.jpg", "loc": "review", "category": "burem"},
        {"name": "b.jpg", "loc": "review", "category": "burem"},
        {"name": "c.jpg", "loc": "review", "category": "burem"},
    ])
    for name, score in (("a.jpg", 40.0), ("b.jpg", 10.0), ("c.jpg", None)):
        uid = _unit(conn, name)["uid"]
        conn.execute("update flags set score=? where unit_id=?", (score, uid))
    conn.commit()
    res = queries.grid_items(conn, "burem", "score", 0)
    assert [i["score"] for i in res["items"]] == [10.0, 40.0, None]
    assert res["total"] == 3


def test_unit_detail_has_reason_and_badges(conn_with):
    conn = conn_with([{"name": "a.jpg", "loc": "review", "category": "burem"}])
    u = _unit(conn, "a.jpg")
    conn.execute(
        "insert into flags(id, unit_id, category, score, reason) values(?,?,?,?,?)",
        (new_id(), u["uid"], "gelap-terang", 1.0, "gelap"),
    )
    conn.commit()
    d = queries.unit_detail(conn, u["uid"])
    assert d["flags"][0]["category"] == "burem" and len(d["flags"]) == 2
    assert d["media"]["kind"] == "photo" and d["media"]["video"] is None
    assert d["primary"]["size_total"] == 1_000_000
    assert queries.unit_detail(conn, "nope") is None
    json.dumps(d)


def test_batches_mark_undoable(conn_with):
    conn = conn_with([{"name": "a.jpg", "loc": "archive"}, {"name": "b.jpg", "loc": "archive"}])
    a, b = _unit(conn, "a.jpg"), _unit(conn, "b.jpg")
    for kind, fid, status in (
        ("rapikan", a["fid"], "done"),
        ("undo", b["fid"], "done"),
        ("apply", b["fid"], "undone"),
    ):
        bid = new_id()
        conn.execute(
            "insert into batches(id, kind, label, created_at) values(?,?,?,?)",
            (bid, kind, kind, "2024-01-01"),
        )
        conn.execute(
            "insert into moves(id, batch_id, file_id, status) values(?,?,?,?)",
            (new_id(), bid, fid, status),
        )
    conn.commit()
    got = {x["kind"]: x for x in queries.list_batches(conn)}
    assert got["rapikan"]["undoable"] and got["rapikan"]["done"] == 1
    assert not got["undo"]["undoable"]
    assert not got["apply"]["undoable"] and got["apply"]["undone"] == 1
    conn.execute("update files set purged=1 where id=?", (a["fid"],))
    conn.commit()
    assert not {x["kind"]: x for x in queries.list_batches(conn)}["rapikan"]["undoable"]


def test_counts_report_label_help_and_seen_progress(conn_with):
    conn = conn_with([
        {"name": "a.jpg", "loc": "review", "category": "burem"},
        {"name": "b.jpg", "loc": "review", "category": "burem"},
        {"name": "c.jpg", "loc": "review", "category": "burem"},
    ])
    conn.execute("update units set seen=1 where id=?", (_unit(conn, "a.jpg")["uid"],))
    decisions.record(conn, _unit(conn, "b.jpg")["uid"], "keep")
    row = next(r for r in queries.category_counts(conn) if r["category"] == "burem")
    assert (row["count"], row["done"]) == (3, 2)
    assert row["label"] == "Blur" and row["help"]


def test_group_progress_counts_only_fully_decided_groups(conn_with):
    conn = conn_with([{"name": n, "loc": "review"} for n in ("a.jpg", "b.jpg", "c.jpg", "d.jpg")])
    a, b, c, d = (_unit(conn, n)["uid"] for n in ("a.jpg", "b.jpg", "c.jpg", "d.jpg"))
    _group(conn, "duplikat-persis", a, [a, b])
    _group(conn, "duplikat-persis", c, [c, d])
    decisions.record(conn, a, "keep"); decisions.record(conn, b, "discard")
    decisions.record(conn, c, "keep")
    row = next(r for r in queries.category_counts(conn) if r["category"] == "duplikat-persis")
    assert (row["count"], row["done"]) == (2, 1)


def test_group_with_archived_keeper_is_done_when_review_copy_decided(conn_with):
    conn = conn_with([
        {"name": "a.jpg", "loc": "archive"},
        {"name": "b.jpg", "loc": "review"},
    ])
    a, b = _unit(conn, "a.jpg")["uid"], _unit(conn, "b.jpg")["uid"]
    _group(conn, "duplikat-persis", a, [a, b])
    decisions.record(conn, b, "discard")
    row = next(r for r in queries.category_counts(conn) if r["category"] == "duplikat-persis")
    assert (row["count"], row["done"]) == (1, 1)


def test_every_category_has_label_and_help():
    from arsipbersih.classify import CATEGORY_PRIORITY
    from arsipbersih.web.labels import CATEGORY_HELP, CATEGORY_LABELS

    for cat in CATEGORY_PRIORITY:
        assert CATEGORY_LABELS.get(cat) and CATEGORY_HELP.get(cat), cat


def test_grid_order_matches_grid_items_order(conn_with):
    conn = conn_with([
        {"name": n, "loc": "review", "category": "burem"} for n in ("a.jpg", "b.jpg", "c.jpg", "d.jpg")
    ])
    for name, score in (("a.jpg", 40.0), ("b.jpg", 10.0), ("c.jpg", None), ("d.jpg", 10.0)):
        conn.execute("update flags set score=? where unit_id=?", (score, _unit(conn, name)["uid"]))
    conn.commit()
    for sort in ("score", "date"):
        expected = [i["unit_id"] for i in queries.grid_items(conn, "burem", sort, 0, 10_000)["items"]]
        assert queries.grid_order(conn, "burem", sort, "all") == expected


def test_group_status_none_partial_all(conn_with):
    conn = conn_with([{"name": n, "loc": "review"} for n in ("a.jpg", "b.jpg")])
    a, b = _unit(conn, "a.jpg")["uid"], _unit(conn, "b.jpg")["uid"]
    gid = _group(conn, "duplikat-persis", a, [a, b])
    assert queries.group_detail(conn, gid)["status"] == "none"
    decisions.record(conn, a, "keep")
    assert queries.group_detail(conn, gid)["status"] == "partial"
    decisions.record(conn, b, "discard")
    assert queries.group_detail(conn, gid)["status"] == "all"


def test_group_status_ignores_archived_keeper(conn_with):
    conn = conn_with([
        {"name": "a.jpg", "loc": "archive"},
        {"name": "b.jpg", "loc": "review"},
    ])
    a, b = _unit(conn, "a.jpg")["uid"], _unit(conn, "b.jpg")["uid"]
    gid = _group(conn, "duplikat-persis", a, [a, b])
    assert queries.group_detail(conn, gid)["status"] == "none"
    decisions.record(conn, b, "discard")
    assert queries.group_detail(conn, gid)["status"] == "all"


def test_version_reason_names_resolution(conn_with):
    conn = conn_with([
        {"name": "IMG_1.jpg", "loc": "review", "w": 4032, "h": 3024},
        {"name": "IMG-20230114-WA0001.jpg", "loc": "review", "w": 1600, "h": 1200},
    ])
    big, small = _unit(conn, "IMG_1.jpg")["uid"], _unit(conn, "IMG-20230114-WA0001.jpg")["uid"]
    gid = _group(conn, "duplikat-versi", big, [big, small])
    g = queries.group_detail(conn, gid)
    assert g["reason"] == "Resolusi paling besar"
    origins = {m["unit_id"]: m["origin"] for m in g["members"]}
    assert origins[small] == "versi kiriman WA" and origins[big] is None


def test_burst_reason_is_sharpest(conn_with):
    conn = conn_with([{"name": n, "loc": "review"} for n in ("a.jpg", "b.jpg")])
    a, b = _unit(conn, "a.jpg")["uid"], _unit(conn, "b.jpg")["uid"]
    gid = _group(conn, "mirip-beruntun", a, [a, b])
    assert queries.group_detail(conn, gid)["reason"] == "Foto paling tajam di rangkaian ini"


def test_batch_titles_are_readable(conn_with):
    conn = conn_with([])
    ids = {}
    for kind, label in [("apply", "apply kiriman-chat"), ("rapikan", "rapikan 2026-11-16"),
                        ("purge", "purge trash")]:
        ids[label] = new_id()
        conn.execute("insert into batches(id, kind, label, created_at) values(?,?,?,?)",
                     (ids[label], kind, label, "2026-11-16T10:00:00"))
    conn.execute("insert into batches(id, kind, label, created_at) values(?,?,?,?)",
                 (new_id(), "undo", f"undo {ids['apply kiriman-chat']}", "2026-11-16T11:00:00"))
    conn.commit()
    titles = [b["title"] for b in queries.list_batches(conn)]
    assert titles == ["Undo · Apply · Chat (WA/Telegram)", "Kosongkan sampah",
                      "Rapikan ke Arsip", "Apply · Chat (WA/Telegram)"]


def test_category_has_label_and_help():
    from arsipbersih.web.labels import CATEGORY_HELP, CATEGORY_LABELS

    assert CATEGORY_LABELS["rekaman-tidak-sengaja"] == "Rekaman tidak sengaja"
    assert "Cek dulu" in CATEGORY_HELP["rekaman-tidak-sengaja"]
