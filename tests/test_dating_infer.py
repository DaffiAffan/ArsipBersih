from __future__ import annotations

from arsipbersih.config import load_settings
from arsipbersih.dating import infer, store
from arsipbersih.db import new_id
from arsipbersih.events import add_override, run_events


def _ids(conn):
    return {
        r["rel_path"].rsplit("/", 1)[-1]: r["id"]
        for r in conn.execute(
            "select u.id, f.rel_path from units u join files f on f.id = u.primary_file_id"
        )
    }


def _group(conn, kind, *unit_ids):
    gid = new_id()
    conn.execute("insert into groups(id, kind, suggested_keep_unit_id) values(?,?,?)",
                 (gid, kind, unit_ids[0]))
    conn.executemany("insert into group_members(group_id, unit_id) values(?,?)",
                     [(gid, u) for u in unit_ids])
    conn.commit()


def _event(conn, name, *unit_ids):
    conn.executemany("update units set event_key=?, event_name=? where id=?",
                     [(name.casefold(), name, u) for u in unit_ids])
    conn.commit()


def _cam(name, taken, path="Sumber/HP/Bali"):
    return {"name": name, "path": path, "taken": taken, "date_source": "exif"}


def _wa(name, taken, path="Sumber/HP/Bali"):
    return {"name": name, "path": path, "taken": taken, "date_source": "filename"}


def _bali(conn_with, extra=()):
    rows = [
        _cam("IMG_20240710_100000.jpg", "2024-07-10T10:00:00"),
        _cam("IMG_20240728_180000.jpg", "2024-07-28T18:00:00"),
        _wa("IMG-20241030-WA0005.jpg", "2024-10-30T00:00:00"),
        _wa("IMG-20241030-WA0002.jpg", "2024-10-30T00:00:00"),
        _wa("IMG-20241029-WA0009.jpg", "2024-10-29T00:00:00"),
        *extra,
    ]
    conn = conn_with(rows)
    ids = _ids(conn)
    _event(conn, "Bali", *ids.values())
    return conn, ids


def _run(conn):
    return infer.run_dates(conn, load_settings(conn))


def test_wa_copy_borrows_date_from_camera_original(conn_with):
    conn = conn_with([
        _cam("IMG_20230601_100000.jpg", "2023-06-01T10:00:00", "Sumber/HP/DCIM"),
        _wa("IMG-20230915-WA0001.jpg", "2023-09-15T00:00:00", "Sumber/HP/WhatsApp"),
        _wa("IMG-20230916-WA0001.jpg", "2023-09-16T00:00:00", "Sumber/HP/WhatsApp"),
    ])
    ids = _ids(conn)
    _group(conn, "duplikat-versi", ids["IMG_20230601_100000.jpg"], ids["IMG-20230915-WA0001.jpg"])
    res = _run(conn)
    assert res.borrowed == 1
    eff = store.effective_dates(conn)
    assert eff[ids["IMG-20230915-WA0001.jpg"]] == ("2023-06-01T10:00:00", "borrowed", "second")
    assert eff[ids["IMG-20230916-WA0001.jpg"]][1] == "filename"
    # Recomputed every run: a second run does not stack rows.
    _run(conn)
    n = conn.execute("select count(*) from date_overrides where source='borrowed'").fetchone()[0]
    assert n == 1


def test_burst_group_does_not_lend_dates(conn_with):
    conn = conn_with([
        _cam("IMG_20230601_100000.jpg", "2023-06-01T10:00:00", "Sumber/HP/DCIM"),
        _wa("IMG-20230915-WA0001.jpg", "2023-09-15T00:00:00", "Sumber/HP/WhatsApp"),
    ])
    ids = _ids(conn)
    _group(conn, "mirip-beruntun", ids["IMG_20230601_100000.jpg"], ids["IMG-20230915-WA0001.jpg"])
    assert _run(conn).borrowed == 0
    assert store.effective_dates(conn)[ids["IMG-20230915-WA0001.jpg"]][1] == "filename"


def test_event_with_late_wa_photos_is_proposed(conn_with):
    conn, ids = _bali(conn_with)
    res = _run(conn)
    assert len(res.proposals) == 1
    p = res.proposals[0]
    assert (p.event_key, p.event_name, p.status) == ("bali", "Bali", "new")
    assert (p.camera_start, p.camera_end) == ("2024-07-10T10:00:00", "2024-07-28T18:00:00")
    assert set(p.unit_ids) == {ids["IMG-20241030-WA0005.jpg"], ids["IMG-20241030-WA0002.jpg"],
                               ids["IMG-20241029-WA0009.jpg"]}
    assert infer.event_proposals(conn) == res.proposals
    # A proposal is not active until approved.
    assert store.effective_dates(conn)[ids["IMG-20241030-WA0005.jpg"]][1] == "filename"


def test_wa_within_two_days_is_not_proposed(conn_with):
    conn = conn_with([
        _cam("IMG_20240710_100000.jpg", "2024-07-10T10:00:00"),
        _cam("IMG_20240728_180000.jpg", "2024-07-28T18:00:00"),
        _wa("IMG-20240730-WA0001.jpg", "2024-07-30T00:00:00"),
        _wa("IMG-20240708-WA0001.jpg", "2024-07-08T00:00:00"),
    ])
    _event(conn, "Bali", *_ids(conn).values())
    assert _run(conn).proposals == []


def test_event_without_camera_dates_has_no_proposal(conn_with):
    conn = conn_with([
        _wa("IMG-20240710-WA0001.jpg", "2024-07-10T00:00:00"),
        _wa("IMG-20241030-WA0001.jpg", "2024-10-30T00:00:00"),
    ])
    _event(conn, "Bali", *_ids(conn).values())
    assert _run(conn).proposals == []


def test_approved_event_spreads_inside_range_keeping_order(conn_with):
    conn, ids = _bali(conn_with)
    _run(conn)
    n = infer.approve_event(conn, "bali", "2024-07-10", "2024-07-28")
    assert n == 3
    eff = store.effective_dates(conn)
    order = ["IMG-20241029-WA0009.jpg", "IMG-20241030-WA0002.jpg", "IMG-20241030-WA0005.jpg"]
    dates = [eff[ids[name]] for name in order]
    assert all(d[1] == "event" for d in dates)
    stamps = [d[0] for d in dates]
    assert stamps == sorted(stamps) and len(set(stamps)) == 3
    assert all("2024-07-10T00:00:00" <= s <= "2024-07-28T23:59:59" for s in stamps)
    row = conn.execute("select decided_by, ref from date_overrides where unit_id=?",
                       (ids[order[0]],)).fetchone()
    assert (row["decided_by"], row["ref"]) == ("owner", "bali")
    # Deterministic and stable on rerun; no longer proposed.
    res = _run(conn)
    assert res.proposals == [] and res.applied_events == 1
    assert [store.effective_dates(conn)[ids[n]][0] for n in order] == stamps
    # Ignoring brings the send dates back.
    infer.ignore_event(conn, "bali")
    assert store.effective_dates(conn)[ids[order[0]]][1] == "filename"
    assert _run(conn).proposals == []


def test_new_wa_member_of_approved_event_gets_date(conn_with):
    conn, ids = _bali(conn_with)
    infer.approve_event(conn, "bali", "2024-07-10", "2024-07-28")
    conn_with([_wa("IMG-20241101-WA0001.jpg", "2024-11-01T00:00:00")])
    new = _ids(conn)["IMG-20241101-WA0001.jpg"]
    _event(conn, "Bali", new)
    res = _run(conn)
    assert res.proposals == []
    taken, source, _ = store.effective_dates(conn)[new]
    assert source == "event" and "2024-07-10" <= taken[:10] <= "2024-07-28"


def test_wider_camera_range_marks_event_changed(conn_with):
    conn, ids = _bali(conn_with)
    infer.approve_event(conn, "bali", "2024-07-10", "2024-07-28")
    before = store.effective_dates(conn)[ids["IMG-20241030-WA0005.jpg"]]
    conn_with([_cam("IMG_20240805_090000.jpg", "2024-08-05T09:00:00")])
    _event(conn, "Bali", _ids(conn)["IMG_20240805_090000.jpg"])
    res = _run(conn)
    assert [(p.event_key, p.status) for p in res.proposals] == [("bali", "changed")]
    assert res.proposals[0].camera_end == "2024-08-05T09:00:00"
    # The old decision stays in force until the owner decides again.
    assert store.effective_dates(conn)[ids["IMG-20241030-WA0005.jpg"]] == before


def test_manual_override_survives_rerun(conn_with):
    conn, ids = _bali(conn_with)
    wa = ids["IMG-20241030-WA0005.jpg"]
    store.set_override(conn, wa, "2024-07-15T12:00:00", "day", "manual", None, "owner")
    _run(conn)
    infer.approve_event(conn, "bali", "2024-07-10", "2024-07-28")
    _run(conn)
    infer.ignore_event(conn, "bali")
    _run(conn)
    assert store.effective_dates(conn)[wa] == ("2024-07-15T12:00:00", "manual", "day")
    assert conn.execute("select count(*) from date_overrides where unit_id=? and source='event'",
                        (wa,)).fetchone()[0] == 0


def test_decision_follows_event_rename(root, conn_with):
    conn, ids = _bali(conn_with)
    settings = load_settings(conn)
    run_events(conn, settings)  # source units re-resolve from the "Bali" folder
    _run(conn)
    infer.approve_event(conn, "bali", "2024-07-10", "2024-07-28")
    add_override(conn, "rename", "Bali", "Bali 2024")
    run_events(conn, settings)
    res = _run(conn)
    assert res.proposals == [] and res.applied_events == 1
    from arsipbersih.events import current_event_key

    assert current_event_key(conn, "bali") == "bali 2024"
    # A new late WA photo in the renamed event still gets an event date.
    conn_with([_wa("IMG-20241102-WA0001.jpg", "2024-11-02T00:00:00")])
    run_events(conn, settings)
    _run(conn)
    new = _ids(conn)["IMG-20241102-WA0001.jpg"]
    assert store.effective_dates(conn)[new][1] == "event"
