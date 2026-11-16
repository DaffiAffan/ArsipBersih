from __future__ import annotations

from arsipbersih.db import new_id, open_db


def _make_group(root, kind="duplikat-persis"):
    conn = open_db(root)
    try:
        uids = []
        for i in range(2):
            uid, fid = new_id(), new_id()
            rel = f"_review/{kind}/HP/IMG_{i}.jpg"
            conn.execute(
                "insert into units(id, primary_file_id, location, primary_category, planned_archive_dir)"
                " values(?,?,?,?,?)",
                (uid, fid, "review", kind, "Arsip/2024/2024-01"),
            )
            conn.execute(
                "insert into files(id, unit_id, rel_path, orig_rel_path, size, kind, ext, source_label)"
                " values(?,?,?,?,?,?,?,?)",
                (fid, uid, rel, rel, 10, "photo", "jpg", "HP"),
            )
            uids.append(uid)
        gid = new_id()
        conn.execute(
            "insert into groups(id, kind, suggested_keep_unit_id) values(?,?,?)", (gid, kind, uids[0])
        )
        conn.executemany(
            "insert into group_members(group_id, unit_id) values(?,?)", [(gid, u) for u in uids]
        )
        conn.commit()
    finally:
        conn.close()


def test_review_page_lists_nonempty_categories(web, root):
    _make_group(root)
    r = web.get("/review/duplikat-persis")
    assert r.status_code == 200
    assert "duplikat-persis" in r.text
    assert 'data-mode="group"' in r.text
    assert "Duplikat persis" in r.text
    idx = web.get("/review", follow_redirects=False)
    assert idx.status_code == 307
    assert idx.headers["location"] == "/review/duplikat-persis"


def test_review_page_empty_and_unknown(web):
    r = web.get("/review")
    assert r.status_code == 200
    assert "Tidak ada yang perlu direview" in r.text
    assert web.get("/review/bukan-kategori").status_code == 404


def test_review_grid_category_renders_grid_root(web):
    r = web.get("/review/burem")
    assert r.status_code == 200
    assert 'data-mode="grid"' in r.text
    assert 'id="grid-root"' in r.text
    assert "/static/review-grid.js" in r.text
    assert "/static/review-groups.js" not in r.text
