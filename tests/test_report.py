from __future__ import annotations

from arsipbersih.dating.store import set_override
from arsipbersih.report import build_report


def _uid(conn, name):
    return conn.execute(
        "select unit_id from files where rel_path like ?", (f"%/{name}",)
    ).fetchone()[0]


def test_report_counts_date_sources(conn_with, root):
    conn = conn_with([
        dict(name="CAM.jpg", taken="2024-07-10T10:00:00", date_source="exif"),
        dict(name="WA.jpg", taken="2024-06-01T00:00:00", date_source="filename"),
        dict(name="DSC.jpg"),
        dict(name="DSC2.jpg"),
    ])
    set_override(conn, _uid(conn, "DSC.jpg"), "2019-05-01T12:00:00", "month", "manual", None, "owner")
    conn.execute("update units set location='trash' where id=?", (_uid(conn, "DSC2.jpg"),))
    conn.commit()
    r = build_report(conn, root)
    assert r.date_sources == {"exif": 1, "filename": 1, "manual": 1}
    assert r.undated == 0
    assert r.date_proposals == 0


def test_report_lists_event_month_mismatch(conn_with, root):
    conn = conn_with([
        dict(name="A.jpg", path="Arsip/2024/2024-06 Bali", loc="archive",
             taken="2024-06-01T00:00:00", date_source="filename"),
        dict(name="B.jpg", path="Arsip/2024/2024-06 Bali/Hari-2", loc="archive",
             taken="2024-06-02T00:00:00", date_source="filename"),
        dict(name="C.jpg", path="Arsip/2024/2024-06 Bali", loc="archive",
             taken="2024-07-12T10:00:00", date_source="exif"),
        dict(name="D.jpg", path="Arsip/2024/2024-03 Pesta", loc="archive",
             taken="2024-03-01T10:00:00", date_source="exif"),
    ])
    conn.execute("update units set event_key='bali', event_name='Bali'"
                 " where id in (?,?,?)", (_uid(conn, "A.jpg"), _uid(conn, "B.jpg"), _uid(conn, "C.jpg")))
    conn.execute("update units set event_key='pesta', event_name='Pesta' where id=?", (_uid(conn, "D.jpg"),))
    for n in ("A.jpg", "B.jpg"):
        set_override(conn, _uid(conn, n), "2024-07-11T10:00:00", "second", "event", "bali", "owner")
    r = build_report(conn, root)
    assert r.event_month_mismatch == [("Arsip/2024/2024-06 Bali", "2024-06", "2024-07")]
