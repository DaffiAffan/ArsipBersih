from __future__ import annotations

import pytest

from arsipbersih.db import new_id
from arsipbersih.mover import PendingMoves, reconcile_or_refuse
from arsipbersih.repair import (
    EndpointOffline,
    HasPendingMove,
    StillOnDisk,
    forget_file,
    list_missing,
    list_pending,
    release_pending,
)


def _dirs(root, dst=True):
    (root / "Sumber" / "HP" / "DCIM").mkdir(parents=True, exist_ok=True)
    if dst:
        (root / "Arsip" / "2024" / "2024-01").mkdir(parents=True, exist_ok=True)


def _pending(conn, src="Sumber/HP/DCIM/IMG_1.jpg"):
    fid, uid = conn.execute("select id, unit_id from files").fetchone()
    bid = new_id()
    conn.execute(
        "insert into batches(id, kind, label, created_at) values(?, 'rapikan', 't', 'now')", (bid,)
    )
    rid = new_id()
    conn.execute(
        "insert into moves(id, batch_id, unit_id, file_id, src_rel, dst_rel, status)"
        " values(?,?,?,?,?,?, 'pending')",
        (rid, bid, uid, fid, src, "Arsip/2024/2024-01/IMG_1.jpg"),
    )
    conn.commit()
    return rid


def test_release_unblocks_a_pending_row_whose_file_is_gone(root, conn_with):
    conn = conn_with([{"name": "IMG_1.jpg"}])
    _dirs(root)
    rid = _pending(conn)
    with pytest.raises(PendingMoves):
        reconcile_or_refuse(conn, root, [])
    assert [p.row_id for p in list_pending(conn)] == [rid]
    release_pending(conn, root, rid, deleted_on_purpose=False)
    reconcile_or_refuse(conn, root, [])
    assert tuple(conn.execute("select missing, purged from files").fetchone()[:]) == (1, 0)


def test_release_marks_purged_when_deleted_on_purpose(root, conn_with):
    conn = conn_with([{"name": "IMG_1.jpg"}])
    _dirs(root)
    rid = _pending(conn)
    release_pending(conn, root, rid, deleted_on_purpose=True)
    assert tuple(conn.execute("select missing, purged from files").fetchone()[:]) == (0, 1)
    assert conn.execute("select status from moves").fetchone()[0] == "failed"


def test_release_refuses_while_file_is_on_disk(root, conn_with):
    conn = conn_with([{"name": "IMG_1.jpg"}])
    rid = _pending(conn)
    # A truncated copy: reconcile neither adopts nor removes it, so the row stays pending.
    part = root / "Sumber" / "HP" / "DCIM" / "IMG_1.jpg.partial"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"x")
    with pytest.raises(StillOnDisk):
        release_pending(conn, root, rid, deleted_on_purpose=True)
    assert conn.execute("select status from moves where id=?", (rid,)).fetchone()[0] == "pending"
    assert tuple(conn.execute("select missing, purged from files").fetchone()[:]) == (0, 0)


def test_forget_file_marks_purged_only_when_absent(root, conn_with):
    conn = conn_with([{"name": "IMG_3.jpg"}])
    fid = conn.execute("select id from files").fetchone()[0]
    conn.execute("update files set missing=1")
    conn.commit()
    _dirs(root, dst=False)
    assert [m.file_id for m in list_missing(conn)] == [fid]
    forget_file(conn, root, fid)
    assert list_missing(conn) == []
    assert conn.execute("select purged from files").fetchone()[0] == 1


def test_forget_file_refuses_when_on_disk(root, conn_with):
    conn = conn_with([{"name": "IMG_4.jpg"}])
    fid = conn.execute("select id from files").fetchone()[0]
    p = root / "Sumber" / "HP" / "DCIM" / "IMG_4.jpg"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"x")
    with pytest.raises(StillOnDisk):
        forget_file(conn, root, fid)
    assert conn.execute("select purged from files").fetchone()[0] == 0


def test_release_is_a_noop_when_reconcile_already_settled_the_row(root, conn_with):
    conn = conn_with([{"name": "IMG_1.jpg"}])
    rid = _pending(conn)
    src = root / "Sumber" / "HP" / "DCIM" / "IMG_1.jpg"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"x")
    release_pending(conn, root, rid, deleted_on_purpose=True)
    assert tuple(conn.execute("select missing, purged from files").fetchone()) == (0, 0)
    assert src.exists()


def test_release_refuses_when_an_endpoint_folder_is_unreachable(root, conn_with):
    conn = conn_with([{"name": "IMG_1.jpg"}])
    rid = _pending(conn)
    _dirs(root, dst=False)  # the Arsip folder (e.g. an unplugged drive) is absent
    with pytest.raises(EndpointOffline):
        release_pending(conn, root, rid, deleted_on_purpose=False)
    assert conn.execute("select status from moves where id=?", (rid,)).fetchone()[0] == "pending"
    assert tuple(conn.execute("select missing, purged from files").fetchone()) == (0, 0)


def test_forget_file_refuses_when_folder_is_unreachable(root, conn_with):
    conn = conn_with([{"name": "IMG_5.jpg"}])
    fid = conn.execute("select id from files").fetchone()[0]
    with pytest.raises(EndpointOffline):
        forget_file(conn, root, fid)
    assert conn.execute("select purged from files").fetchone()[0] == 0


def test_forget_file_refuses_while_a_move_is_pending(root, conn_with):
    conn = conn_with([{"name": "IMG_1.jpg"}])
    _pending(conn)
    fid = conn.execute("select id from files").fetchone()[0]
    _dirs(root)
    with pytest.raises(HasPendingMove):
        forget_file(conn, root, fid)
    assert conn.execute("select purged from files").fetchone()[0] == 0


def test_release_returns_the_done_file_of_a_split_unit(root, conn_with):
    conn = conn_with([{"name": "IMG_1.jpg"}])
    _dirs(root)
    rid = _pending(conn)
    uid = conn.execute("select unit_id from files").fetchone()[0]
    bid = conn.execute("select batch_id from moves").fetchone()[0]
    f2, rid2 = new_id(), new_id()
    src2, dst2 = "Sumber/HP/DCIM/IMG_2.jpg", "Arsip/2024/2024-01/IMG_2.jpg"
    conn.execute(
        "insert into files(id, unit_id, rel_path, orig_rel_path, size, kind, ext)"
        " values(?,?,?,?,1,'photo','jpg')",
        (f2, uid, dst2, src2),
    )
    conn.execute(
        "insert into moves(id, batch_id, unit_id, file_id, src_rel, dst_rel, status)"
        " values(?,?,?,?,?,?, 'done')",
        (rid2, bid, uid, f2, src2, dst2),
    )
    conn.commit()
    (root / "Arsip" / "2024" / "2024-01" / "IMG_2.jpg").write_bytes(b"x")
    release_pending(conn, root, rid, deleted_on_purpose=False)
    assert (root / "Sumber" / "HP" / "DCIM" / "IMG_2.jpg").exists()
    assert not (root / "Arsip" / "2024" / "2024-01" / "IMG_2.jpg").exists()
    assert conn.execute("select rel_path from files where id=?", (f2,)).fetchone()[0] == src2
    assert conn.execute("select status from moves where id=?", (rid2,)).fetchone()[0] == "failed"


def test_forget_file_uses_the_path_reconcile_settled(root, conn_with):
    conn = conn_with([{"name": "IMG_1.jpg", "size": 1}])
    _dirs(root)
    _pending(conn)
    fid = conn.execute("select id from files").fetchone()[0]
    (root / "Arsip" / "2024" / "2024-01" / "IMG_1.jpg").write_bytes(b"x")  # src absent
    with pytest.raises(StillOnDisk):
        forget_file(conn, root, fid)
    assert conn.execute("select purged from files").fetchone()[0] == 0
    assert conn.execute("select rel_path from files").fetchone()[0].startswith("Arsip/")
