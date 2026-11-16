from __future__ import annotations

import pytest

from arsipbersih.db import new_id
from arsipbersih.mover import PendingMoves, reconcile_or_refuse
from arsipbersih.repair import (
    StillOnDisk,
    forget_file,
    list_missing,
    list_pending,
    release_pending,
)


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
    rid = _pending(conn)
    with pytest.raises(PendingMoves):
        reconcile_or_refuse(conn, root, [])
    assert [p.row_id for p in list_pending(conn)] == [rid]
    release_pending(conn, root, rid, deleted_on_purpose=False)
    reconcile_or_refuse(conn, root, [])
    assert tuple(conn.execute("select missing, purged from files").fetchone()[:]) == (1, 0)


def test_release_marks_purged_when_deleted_on_purpose(root, conn_with):
    conn = conn_with([{"name": "IMG_1.jpg"}])
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
