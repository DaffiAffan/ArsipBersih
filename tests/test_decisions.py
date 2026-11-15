from __future__ import annotations

import os

import pytest

from arsipbersih.db import new_id, open_db
from arsipbersih.decisions import (
    ApplyPreview,
    PurgeMismatch,
    accept_suggestions,
    apply_category,
    mark_seen,
    preview_apply,
    purge_trash,
    record,
    trash_totals,
)


def _add_unit(conn, root, rel, *, location, category=None, archive_dir="Arsip/2024/2024-01"):
    uid, fid = new_id(), new_id()
    p = root / rel
    os.makedirs(p.parent, exist_ok=True)
    p.write_bytes(b"x" * 10)
    conn.execute(
        "insert into units(id, primary_file_id, location, primary_category, planned_archive_dir)"
        " values(?,?,?,?,?)",
        (uid, fid, location, category, archive_dir),
    )
    conn.execute(
        "insert into files(id, unit_id, rel_path, orig_rel_path, size, kind, ext, source_label)"
        " values(?,?,?,?,?,?,?,?)",
        (fid, uid, rel, rel, 10, "photo", "jpg", "HP"),
    )
    conn.commit()
    return uid


@pytest.fixture
def review_db(root):
    def make(category):
        conn = open_db(root)
        ids = tuple(
            _add_unit(conn, root, f"_review/{category}/HP/IMG_{i}.jpg",
                      location="review", category=category)
            for i in range(3)
        )
        return conn, root, ids

    return make


@pytest.fixture
def group_db(root):
    def make(kind):
        conn = open_db(root)
        keeper = _add_unit(conn, root, "Arsip/2024/2024-01/IMG_K.jpg", location="archive")
        other = _add_unit(conn, root, f"_review/{kind}/HP/IMG_O.jpg", location="review")
        gid = new_id()
        conn.execute("insert into groups(id, kind, suggested_keep_unit_id) values(?,?,?)",
                     (gid, kind, keeper))
        conn.executemany("insert into group_members(group_id, unit_id) values(?,?)",
                         [(gid, keeper), (gid, other)])
        conn.commit()
        return conn, root, gid, keeper, other

    return make


def test_grid_default_discard_only_for_seen(review_db):
    conn, root, (a, b, c) = review_db("burem")
    mark_seen(conn, [a, b]); record(conn, b, "keep")
    assert preview_apply(conn, "burem") == ApplyPreview(keep=1, discard=1, unseen=1)
    apply_category(conn, root, "burem")
    loc = dict(conn.execute("select id, location from units").fetchall())
    assert (loc[a], loc[b], loc[c]) == ("trash", "archive", "review")


def test_group_accept_and_override_archive_keeper(group_db):
    conn, root, gid, keeper, other = group_db("mirip-beruntun")
    accept_suggestions(conn, [gid]); record(conn, keeper, "discard"); record(conn, other, "keep")
    apply_category(conn, root, "mirip-beruntun")
    loc = dict(conn.execute("select id, location from units").fetchall())
    assert (loc[keeper], loc[other]) == ("trash", "archive")


def test_purge_requires_matching_totals(review_db):
    conn, root, (a, b, c) = review_db("burem")
    mark_seen(conn, [a, b, c]); apply_category(conn, root, "burem")
    files, size = trash_totals(conn)
    with pytest.raises(PurgeMismatch):
        purge_trash(conn, root, files + 1, size)
    assert purge_trash(conn, root, files, size) == 3
    assert not any((root / "_sampah").rglob("*.jpg"))


def test_purge_settles_file_already_gone(root):
    conn = open_db(root)
    uid = _add_unit(conn, root, "_sampah/burem/HP/IMG_G.jpg", location="trash")
    os.unlink(root / "_sampah/burem/HP/IMG_G.jpg")
    files, size = trash_totals(conn)
    assert (files, size) == (1, 10)
    assert purge_trash(conn, root, files, size) == 1
    assert conn.execute("select purged from files where unit_id=?", (uid,)).fetchone()[0] == 1
    assert trash_totals(conn) == (0, 0)
