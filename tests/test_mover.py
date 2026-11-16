from __future__ import annotations

import os
import shutil

import pytest

from arsipbersih.db import new_id, open_db
from arsipbersih.inventory import run_inventory
from arsipbersih.layout import long_path
from arsipbersih.mover import (
    InsufficientSpace,
    check_space,
    execute,
    reconcile,
    remove_empty_dirs,
    undo_all,
    undo_batch,
)
from arsipbersih.decisions import (
    apply_category,
    mark_seen,
    preview_apply,
    purge_trash,
    trash_totals,
)
from arsipbersih.pairing import build_units
from arsipbersih.planner import FileMove, UnitMove


def _one_unit(conn, root, rel="Sumber/HP/DCIM/IMG_1.jpg", data=b"abc"):
    # long_path: the deep-path test exceeds MAX_PATH on Windows without LongPathsEnabled.
    p = root / rel; os.makedirs(long_path(p.parent), exist_ok=True)
    with open(long_path(p), "wb") as f:
        f.write(data)
    run_inventory(conn, root); build_units(conn)
    uid, fid = conn.execute("select unit_id, id from files where rel_path=?", (rel,)).fetchone()
    return uid, fid


def insert_pending(conn, uid, fid, src, dst, bid=None):
    with conn:
        if bid is None:
            bid = new_id()
            conn.execute(
                "insert into batches (id, kind, label, created_at) values (?, 'rapikan', 't', ?)",
                (bid, "2026-11-15T10:00:00"),
            )
        conn.execute(
            "insert into moves (id, batch_id, unit_id, file_id, src_rel, dst_rel, status, created_at)"
            " values (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (new_id(), bid, uid, fid, src, dst, "2026-11-15T10:00:00"),
        )
    return bid


def test_execute_moves_journals_and_updates(root):
    conn = open_db(root); uid, fid = _one_unit(conn, root)
    r = execute(conn, root, [UnitMove(uid, "archive", [FileMove(fid, "Sumber/HP/DCIM/IMG_1.jpg", "Arsip/2023/2023-08/IMG_1.jpg")])], "rapikan", "t")
    assert r.moved_units == 1 and (root / "Arsip/2023/2023-08/IMG_1.jpg").exists()
    assert conn.execute("select rel_path from files where id=?", (fid,)).fetchone()[0] == "Arsip/2023/2023-08/IMG_1.jpg"
    assert conn.execute("select status from moves").fetchone()[0] == "done"


def test_never_overwrites(root):
    conn = open_db(root); uid, fid = _one_unit(conn, root)
    (root / "Arsip").mkdir(); (root / "Arsip/IMG_1.jpg").write_bytes(b"other")
    r = execute(conn, root, [UnitMove(uid, "archive", [FileMove(fid, "Sumber/HP/DCIM/IMG_1.jpg", "Arsip/IMG_1.jpg")])], "rapikan", "t")
    assert r.failed_units == 1 and (root / "Arsip/IMG_1.jpg").read_bytes() == b"other"
    assert (root / "Sumber/HP/DCIM/IMG_1.jpg").exists()


def test_unit_rolls_back_when_second_file_fails(root):
    conn = open_db(root)
    d = root / "Sumber/HP/DCIM"; d.mkdir(parents=True)
    heic_bytes = b"heic-original-bytes"
    (d / "IMG_1.HEIC").write_bytes(heic_bytes); (d / "IMG_1.MOV").write_bytes(b"mov-bytes")
    run_inventory(conn, root); build_units(conn)
    uid, pair_kind = conn.execute("select id, pair_kind from units").fetchone()
    assert pair_kind == "live_photo"
    ids = {rel: fid for fid, rel in conn.execute("select id, rel_path from files")}
    heic_src, mov_src = "Sumber/HP/DCIM/IMG_1.HEIC", "Sumber/HP/DCIM/IMG_1.MOV"
    heic_dst, mov_dst = "Arsip/2023/2023-08/IMG_1.HEIC", "Arsip/2023/2023-08/IMG_1.MOV"
    (root / "Arsip/2023/2023-08").mkdir(parents=True); (root / mov_dst).write_bytes(b"already here")
    r = execute(conn, root, [UnitMove(uid, "archive", [
        FileMove(ids[heic_src], heic_src, heic_dst), FileMove(ids[mov_src], mov_src, mov_dst),
    ])], "rapikan", "t")
    assert r.failed_units == 1 and r.moved_units == 0
    assert (root / heic_src).read_bytes() == heic_bytes and not (root / heic_dst).exists()
    assert (root / mov_dst).read_bytes() == b"already here" and (root / mov_src).exists()
    assert sorted(s for (s,) in conn.execute("select status from moves")) == ["failed", "failed"]
    assert {rel for (rel,) in conn.execute("select rel_path from files")} == {heic_src, mov_src}


def test_moves_long_unicode_path(root):
    deep = "Sumber/HP/" + "/".join(["Foto Liburan 🏝️ Bali panjang sekali"] * 8) + "/IMG_ü.jpg"
    conn = open_db(root); uid, fid = _one_unit(conn, root, rel=deep)
    dst = "Arsip/2023/2023-08 " + "Acara Ä" * 30 + "/IMG_ü.jpg"
    assert execute(conn, root, [UnitMove(uid, "archive", [FileMove(fid, deep, dst)])], "rapikan", "t").moved_units == 1


def test_reconcile_pending_after_completed_rename(root):
    conn = open_db(root); uid, fid = _one_unit(conn, root)
    insert_pending(conn, uid, fid, "Sumber/HP/DCIM/IMG_1.jpg", "Arsip/IMG_1.jpg")
    (root / "Arsip").mkdir(); os.rename(root / "Sumber/HP/DCIM/IMG_1.jpg", root / "Arsip/IMG_1.jpg")
    assert reconcile(conn, root) == 1
    assert conn.execute("select rel_path from files where id=?", (fid,)).fetchone()[0] == "Arsip/IMG_1.jpg"


def test_reconcile_half_copied_cross_drive(root):
    conn = open_db(root); uid, fid = _one_unit(conn, root)
    insert_pending(conn, uid, fid, "Sumber/HP/DCIM/IMG_1.jpg", "Arsip/IMG_1.jpg")
    (root / "Arsip").mkdir(); (root / "Arsip/IMG_1.jpg.partial").write_bytes(b"ab")
    reconcile(conn, root)
    assert (root / "Sumber/HP/DCIM/IMG_1.jpg").exists() and not (root / "Arsip/IMG_1.jpg.partial").exists()
    assert conn.execute("select status from moves").fetchone()[0] == "failed"


def test_reconcile_does_not_adopt_partial_when_source_dir_offline(root):
    conn = open_db(root); uid, fid = _one_unit(conn, root)
    insert_pending(conn, uid, fid, "Sumber/HP/DCIM/IMG_1.jpg", "Arsip/IMG_1.jpg")
    # A full-size .partial, and the source's directory unreachable (drive unplugged).
    (root / "Arsip").mkdir(); os.rename(root / "Sumber/HP/DCIM/IMG_1.jpg", root / "Arsip/IMG_1.jpg.partial")
    (root / "Sumber/HP/DCIM").rmdir(); (root / "Sumber/HP").rmdir()
    errors = []
    assert reconcile(conn, root, errors) == 0
    assert (root / "Arsip/IMG_1.jpg.partial").exists() and not (root / "Arsip/IMG_1.jpg").exists()
    assert conn.execute("select status from moves").fetchone()[0] == "pending"
    assert any("endpoint offline" in e for e in errors)


def test_reconcile_rejoins_unit_split_by_crash(root):
    conn = open_db(root)
    d = root / "Sumber/HP/DCIM"; d.mkdir(parents=True)
    (d / "IMG_1.HEIC").write_bytes(b"heic-bytes"); (d / "IMG_1.MOV").write_bytes(b"mov-bytes")
    run_inventory(conn, root); build_units(conn)
    uid = conn.execute("select id from units").fetchone()[0]
    ids = {rel: fid for fid, rel in conn.execute("select id, rel_path from files")}
    heic, mov = "Sumber/HP/DCIM/IMG_1.HEIC", "Sumber/HP/DCIM/IMG_1.MOV"
    # Hard crash mid-unit: both rows pending, the HEIC already renamed, the MOV not yet.
    bid = insert_pending(conn, uid, ids[heic], heic, "Arsip/2023/IMG_1.HEIC")
    insert_pending(conn, uid, ids[mov], mov, "Arsip/2023/IMG_1.MOV", bid=bid)
    (root / "Arsip/2023").mkdir(parents=True); os.rename(root / heic, root / "Arsip/2023/IMG_1.HEIC")
    reconcile(conn, root)
    assert (root / heic).read_bytes() == b"heic-bytes" and (root / mov).read_bytes() == b"mov-bytes"
    assert not (root / "Arsip/2023/IMG_1.HEIC").exists()
    assert [s for (s,) in conn.execute("select status from moves")] == ["failed", "failed"]
    assert {rel for (rel,) in conn.execute("select rel_path from files")} == {heic, mov}
    assert conn.execute("select location from units").fetchone()[0] == "source"


def test_rapikan_settles_pending_row_before_planning(root):
    import json

    from arsipbersih.pipeline import SCAN_DONE_KEY, rapikan

    conn = open_db(root); uid, fid = _one_unit(conn, root)
    with conn:
        conn.execute("insert into settings(key, value) values(?, ?)", (SCAN_DONE_KEY, json.dumps("x")))
    insert_pending(conn, uid, fid, "Sumber/HP/DCIM/IMG_1.jpg", "Arsip/IMG_1.jpg")
    (root / "Arsip").mkdir(); os.rename(root / "Sumber/HP/DCIM/IMG_1.jpg", root / "Arsip/IMG_1.jpg")
    r = rapikan(root)
    assert (r.moved_units, r.failed_units) == (0, 0)
    assert conn.execute("select status from moves").fetchone()[0] == "done"
    assert conn.execute("select rel_path from files where id=?", (fid,)).fetchone()[0] == "Arsip/IMG_1.jpg"
    assert conn.execute("select location from units where id=?", (uid,)).fetchone()[0] == "archive"


def test_undo_batch_restores_and_remove_empty_dirs(root):
    conn = open_db(root); uid, fid = _one_unit(conn, root)
    r = execute(conn, root, [UnitMove(uid, "archive", [FileMove(fid, "Sumber/HP/DCIM/IMG_1.jpg", "Arsip/IMG_1.jpg")])], "rapikan", "t")
    assert remove_empty_dirs(root / "Sumber") == 2      # DCIM, HP
    undo_batch(conn, root, r.batch_id)
    assert (root / "Sumber/HP/DCIM/IMG_1.jpg").exists()
    assert undo_batch(conn, root, r.batch_id).batch_id == ""     # nothing left: no empty batch
    assert conn.execute("select count(*) from batches").fetchone()[0] == 2


def test_cross_device_copy_verifies_hash(root, monkeypatch):
    monkeypatch.setattr("arsipbersih.mover._same_device", lambda a, b: False)
    conn = open_db(root); uid, fid = _one_unit(conn, root)
    execute(conn, root, [UnitMove(uid, "archive", [FileMove(fid, "Sumber/HP/DCIM/IMG_1.jpg", "Arsip/IMG_1.jpg")])], "rapikan", "t")
    assert not (root / "Sumber/HP/DCIM/IMG_1.jpg").exists() and (root / "Arsip/IMG_1.jpg").read_bytes() == b"abc"


def test_check_space_raises(root, monkeypatch):
    monkeypatch.setattr("arsipbersih.mover._same_device", lambda a, b: False)
    monkeypatch.setattr("shutil.disk_usage", lambda p: shutil._ntuple_diskusage(10, 10, 10))
    conn = open_db(root); uid, fid = _one_unit(conn, root)
    with pytest.raises(InsufficientSpace):
        check_space(root, [UnitMove(uid, "archive", [FileMove(fid, "Sumber/HP/DCIM/IMG_1.jpg", "Arsip/IMG_1.jpg")])])


def _review_units(root, category, n):
    ids = []
    conn = open_db(root)
    for i in range(n):
        uid, fid = new_id(), new_id()
        rel = f"_review/{category}/HP/IMG_{i}.jpg"
        p = root / rel
        os.makedirs(p.parent, exist_ok=True)
        p.write_bytes(b"x" * 10)
        conn.execute(
            "insert into units(id, primary_file_id, location, primary_category, planned_archive_dir)"
            " values(?,?,?,?,?)", (uid, fid, "review", category, "Arsip/2024/2024-01"))
        conn.execute(
            "insert into files(id, unit_id, rel_path, orig_rel_path, size, kind, ext, source_label)"
            " values(?,?,?,?,?,?,?,?)", (fid, uid, rel, rel, 10, "photo", "jpg", "HP"))
        ids.append(uid)
    conn.commit()
    return conn, tuple(ids)


def test_undo_apply_returns_grid_units_unseen_and_undecided(root):
    conn, ids = _review_units(root, "burem", n=3)
    mark_seen(conn, list(ids))
    res = apply_category(conn, root, "burem")
    undo_batch(conn, root, res.batch_id)
    p = preview_apply(conn, "burem")
    assert (p.keep, p.discard, p.unseen) == (0, 0, 3)
    assert conn.execute("select count(*) from decisions").fetchone()[0] == 0


def test_undo_all_skips_purged_files(root):
    conn, ids = _review_units(root, "burem", n=2)
    mark_seen(conn, list(ids))
    apply_category(conn, root, "burem")
    purge_trash(conn, root, *trash_totals(conn))
    res = undo_all(conn, root)
    assert res.batch_id == "" and res.failed_units == 0 and res.errors == []


def test_undo_partly_purged_unit_is_not_restored(root):
    conn, (uid,) = _review_units(root, "burem", n=1)
    mark_seen(conn, [uid])
    res = apply_category(conn, root, "burem")
    fid = conn.execute("select id from files where unit_id=?", (uid,)).fetchone()[0]
    cur = conn.execute("select rel_path from files where id=?", (fid,)).fetchone()[0]
    rel2 = cur.rsplit("/", 1)[0] + "/IMG_0.xmp"
    (root / rel2).write_bytes(b"y")
    f2 = new_id()
    conn.execute(
        "insert into files(id, unit_id, rel_path, orig_rel_path, size, kind, ext, source_label)"
        " values(?,?,?,?,?,?,?,?)", (f2, uid, rel2, rel2, 1, "sidecar", "xmp", "HP"))
    conn.execute(
        "insert into moves(id, batch_id, unit_id, file_id, src_rel, dst_rel, status, created_at)"
        " values(?,?,?,?,?,?,'done','2026-11-15T10:00:00')",
        (new_id(), res.batch_id, uid, f2, "_review/burem/HP/IMG_0.xmp", rel2))
    conn.execute("update files set purged=1 where id=?", (fid,))
    conn.commit()
    r = undo_batch(conn, root, res.batch_id)
    assert r.batch_id == "" and any("partly purged" in e for e in r.errors)
    assert (root / rel2).exists()
    assert conn.execute("select location from units where id=?", (uid,)).fetchone()[0] == "trash"


def test_undo_interrupted_still_resets_restored_units(root, monkeypatch):
    from arsipbersih import mover
    conn, ids = _review_units(root, "burem", n=2)
    mark_seen(conn, list(ids))
    res = apply_category(conn, root, "burem")
    real, calls = mover._move_file, []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real(src, dst)

    monkeypatch.setattr(mover, "_move_file", flaky)
    with pytest.raises(KeyboardInterrupt):
        undo_batch(conn, root, res.batch_id)
    restored = conn.execute(
        "select id, seen from units where location='review'").fetchall()
    assert len(restored) == 1 and restored[0][1] == 0
    assert conn.execute("select count(*) from decisions where unit_id=?", (restored[0][0],)).fetchone()[0] == 0


def test_undo_to_archive_keeps_decision(root):
    conn, (uid,) = _review_units(root, "burem", n=1)
    rel = "Arsip/2024/2024-01/IMG_0.jpg"
    fid = conn.execute("select id from files where unit_id=?", (uid,)).fetchone()[0]
    os.makedirs(root / "Arsip/2024/2024-01"); os.makedirs(root / "_sampah")
    shutil.move(root / "_review/burem/HP/IMG_0.jpg", root / rel)
    conn.execute("update files set rel_path=? where id=?", (rel, fid))
    conn.execute("update units set location='archive' where id=?", (uid,))
    conn.execute("insert into decisions(unit_id, decision, applied) values(?, 'discard', 1)", (uid,))
    conn.commit()
    r = execute(conn, root, [UnitMove(uid, "trash", [FileMove(fid, rel, "_sampah/IMG_0.jpg")])], "apply", "t")
    undo_batch(conn, root, r.batch_id)
    assert conn.execute("select location from units where id=?", (uid,)).fetchone()[0] == "archive"
    assert conn.execute("select count(*) from decisions where unit_id=?", (uid,)).fetchone()[0] == 1
