from __future__ import annotations

import os
import shutil

import pytest

from arsipbersih import decisions
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
    record_bulk_discard,
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


def test_grid_apply_moves_only_explicit_decisions(review_db):
    conn, root, (a, b, c) = review_db("burem")
    mark_seen(conn, [a, b]); record(conn, b, "keep")
    assert preview_apply(conn, "burem") == ApplyPreview(keep=1, discard=0, unseen=2)
    apply_category(conn, root, "burem")
    loc = dict(conn.execute("select id, location from units").fetchall())
    assert (loc[a], loc[b], loc[c]) == ("review", "archive", "review")


def test_record_bulk_discard_fills_only_undecided_review_units(review_db):
    conn, root, (a, b, c) = review_db("kiriman-chat")
    archived = _add_unit(conn, root, "Arsip/2024/2024-01/X.jpg", location="archive")
    record(conn, a, "keep")
    record(conn, b, "discard")
    n = record_bulk_discard(conn, [a, b, c, archived, "missing-id"])
    assert n == 1
    assert _decision_map(conn) == {a: "keep", b: "discard", c: "discard"}
    assert record_bulk_discard(conn, [a, b, c]) == 0  # second call changes nothing


def test_record_bulk_discard_skips_group_categories(review_db):
    conn, root, (a, _, _) = review_db("kiriman-chat")
    grouped = _add_unit(conn, root, "_review/duplikat-persis/HP/D.jpg", location="review",
                        category="duplikat-persis")
    assert record_bulk_discard(conn, [a, grouped]) == 1
    assert _decision_map(conn) == {a: "discard"}


def test_record_bulk_discard_refills_applied_decision(review_db):
    conn, root, (a, b, c) = review_db("kiriman-chat")
    record(conn, a, "keep")
    conn.execute("update decisions set applied=1 where unit_id=?", (a,))
    conn.commit()
    assert record_bulk_discard(conn, [a]) == 1
    row = conn.execute("select decision, applied from decisions where unit_id=?", (a,)).fetchone()
    assert tuple(row) == ("discard", 0)


def test_group_accept_and_override_archive_keeper(group_db):
    conn, root, gid, keeper, other = group_db("mirip-beruntun")
    accept_suggestions(conn, [gid]); record(conn, keeper, "discard"); record(conn, other, "keep")
    apply_category(conn, root, "mirip-beruntun")
    loc = dict(conn.execute("select id, location from units").fetchall())
    assert (loc[keeper], loc[other]) == ("trash", "archive")


def _decision_map(conn):
    return dict(conn.execute("select unit_id, decision from decisions").fetchall())


def test_accept_suggestions_keeps_explicit_unapplied_decision(group_db):
    conn, root, gid, keeper, other = group_db("mirip-beruntun")
    record(conn, other, "keep")  # explicit keep on a non-keeper
    assert accept_suggestions(conn, [gid]) == 1  # only the keeper was undecided
    assert _decision_map(conn) == {keeper: "keep", other: "keep"}


def test_accept_suggestions_fills_undecided_members(group_db):
    conn, root, gid, keeper, other = group_db("mirip-beruntun")
    assert accept_suggestions(conn, [gid]) == 2
    assert _decision_map(conn) == {keeper: "keep", other: "discard"}
    assert accept_suggestions(conn, [gid]) == 0  # second accept changes nothing


def test_accept_suggestions_keeps_explicit_discard_on_keeper(group_db):
    conn, root, gid, keeper, other = group_db("mirip-beruntun")
    record(conn, keeper, "discard")
    assert accept_suggestions(conn, [gid]) == 1
    assert _decision_map(conn) == {keeper: "discard", other: "discard"}


def test_purge_requires_matching_totals(review_db):
    conn, root, (a, b, c) = review_db("burem")
    [record(conn, u, "discard") for u in (a, b, c)]; apply_category(conn, root, "burem")
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


def test_purge_refuses_when_trash_folder_missing(root):
    conn = open_db(root)
    uid = _add_unit(conn, root, "_sampah/burem/HP/IMG_M.jpg", location="trash")
    shutil.move(root / "_sampah", root / "_sampah_away")
    files, size = trash_totals(conn)
    with pytest.raises(PurgeMismatch):
        purge_trash(conn, root, files, size)
    assert conn.execute("select purged from files where unit_id=?", (uid,)).fetchone()[0] == 0


def test_purge_reports_each_failed_file(root, monkeypatch, review_db):
    conn, root, ids = review_db("burem")
    [record(conn, u, "discard") for u in ids]; apply_category(conn, root, "burem")
    real = decisions._delete
    def flaky(p):
        if p.name == "IMG_1.jpg":
            raise PermissionError("locked")
        real(p)
    monkeypatch.setattr(decisions, "_delete", flaky)
    errors: list[str] = []
    n = purge_trash(conn, root, *trash_totals(conn), errors=errors)
    assert n == 2
    assert len(errors) == 1 and "IMG_1.jpg" in errors[0] and "PermissionError" in errors[0]


def test_purge_skips_file_whose_size_changed(root, review_db):
    conn, root, ids = review_db("burem")
    [record(conn, u, "discard") for u in ids]; apply_category(conn, root, "burem")
    totals = trash_totals(conn)
    victim = next((root / "_sampah").rglob("IMG_0.jpg"))
    victim.write_bytes(b"y" * 99)                   # bukan file yang dicatat lagi
    errors: list[str] = []
    assert purge_trash(conn, root, *totals, errors=errors) == 2
    assert victim.exists() and "differs" in errors[0]
