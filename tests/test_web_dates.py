from __future__ import annotations

import pytest

from arsipbersih.dating import store


def _ids(conn):
    return {
        r["rel_path"].rsplit("/", 1)[-1]: r["id"]
        for r in conn.execute(
            "select u.id, f.rel_path from units u join files f on f.id = u.primary_file_id"
        )
    }


def _cam(name, taken):
    return {"name": name, "path": "Sumber/HP/Bali", "taken": taken, "date_source": "exif"}


def _wa(name, taken):
    return {"name": name, "path": "Sumber/HP/Bali", "taken": taken, "date_source": "filename"}


@pytest.fixture
def bali(web, conn_with):
    conn = conn_with([
        _cam("IMG_20240710_100000.jpg", "2024-07-10T10:00:00"),
        _cam("IMG_20240728_180000.jpg", "2024-07-28T18:00:00"),
        _wa("IMG-20241030-WA0005.jpg", "2024-10-30T00:00:00"),
        _wa("IMG-20241030-WA0002.jpg", "2024-10-30T00:00:00"),
        _wa("IMG-20241029-WA0009.jpg", "2024-10-29T00:00:00"),
    ])
    ids = _ids(conn)
    conn.executemany("update units set event_key='bali', event_name='Bali' where id=?",
                     [(u,) for u in ids.values()])
    conn.commit()
    yield conn, ids
    conn.close()


WA = ("IMG-20241030-WA0005.jpg", "IMG-20241030-WA0002.jpg", "IMG-20241029-WA0009.jpg")


def test_events_api_lists_proposals(web, bali):
    conn, ids = bali
    r = web.get("/api/dates/events")
    assert r.status_code == 200
    (ev,) = r.json()["events"]
    assert ev["event_key"] == "bali"
    assert ev["event_name"] == "Bali"
    assert ev["camera_start"].startswith("2024-07-10")
    assert ev["camera_end"].startswith("2024-07-28")
    assert ev["status"] == "new"
    assert ev["count"] == 3
    fids = {r[0] for r in conn.execute(
        "select primary_file_id from units where id in (?,?,?)", [ids[n] for n in WA])}
    assert set(ev["samples"]) == fids
    assert ev["summary"] == (
        "Foto kiriman WA di acara ini memakai tanggal kirim (Okt 2024), "
        "sedangkan foto kameranya Juli 2024."
    )


def test_approve_rejects_bad_range(web, bali):
    url = "/api/dates/events/bali/approve"
    r = web.post(url, json={"range_start": "2024-07-28", "range_end": "2024-07-10"})
    assert r.status_code == 400
    assert "Tanggal mulai" in r.json()["detail"]
    r = web.post(url, json={"range_start": "28-07-2024", "range_end": "2024-07-30"})
    assert r.status_code == 400
    assert "YYYY-MM-DD" not in r.json()["detail"]  # plain words, not a format code
    r = web.post("/api/dates/events/tidak-ada/approve",
                 json={"range_start": "2024-07-10", "range_end": "2024-07-28"})
    assert r.status_code == 404
    conn, _ = bali
    assert conn.execute("select count(*) from event_date_decisions").fetchone()[0] == 0


def test_approve_writes_event_overrides(web, bali):
    conn, ids = bali
    r = web.post("/api/dates/events/bali/approve",
                 json={"range_start": "2024-07-15", "range_end": "2024-07-20"})
    assert r.status_code == 200
    assert r.json()["dated"] == 3
    eff = store.effective_dates(conn, [ids[n] for n in WA])
    for taken, source, _ in eff.values():
        assert source == "event"
        assert "2024-07-15" <= taken[:10] <= "2024-07-20"
    dec = conn.execute("select decision, range_start, range_end from event_date_decisions").fetchone()
    assert tuple(dec) == ("approved", "2024-07-15", "2024-07-20")
    assert web.get("/api/dates/events").json()["events"] == []
    # No media moved: every unit is still where it was.
    assert {r[0] for r in conn.execute("select location from units")} == {"source"}


def test_ignore_records_decision(web, bali):
    conn, ids = bali
    r = web.post("/api/dates/events/bali/ignore")
    assert r.status_code == 200
    dec = conn.execute("select decision from event_date_decisions").fetchone()
    assert dec[0] == "ignored"
    assert conn.execute("select count(*) from date_overrides").fetchone()[0] == 0
    assert web.get("/api/dates/events").json()["events"] == []
    assert web.post("/api/dates/events/tidak-ada/ignore").status_code == 404


def test_manual_date_for_selection(web, conn_with):
    conn = conn_with([
        {"name": "scan1.jpg", "path": "Arsip/Tanpa-Tanggal", "loc": "archive"},
        {"name": "scan2.jpg", "path": "Arsip/Tanpa-Tanggal", "loc": "archive"},
        {"name": "dibuang.jpg", "path": "_review/x", "loc": "trash"},
        {"name": "ada.jpg", "taken": "2020-01-01T10:00:00", "date_source": "exif"},
    ])
    ids = _ids(conn)
    r = web.get("/api/dates/undated").json()
    assert set(r["unit_ids"]) == {ids["scan1.jpg"], ids["scan2.jpg"]}
    assert r["total"] == 2
    item = r["items"][0]
    assert item["thumb"].startswith("/media/thumb/")
    assert item["name"] in ("scan1.jpg", "scan2.jpg")

    a, b = ids["scan1.jpg"], ids["scan2.jpg"]
    assert web.post("/api/dates/manual", json={"unit_ids": [a], "year": 2019, "month": 13}).status_code == 400
    r = web.post("/api/dates/manual", json={"unit_ids": [a], "year": 2019, "month": 2, "day": 30})
    assert r.status_code == 400
    assert "tanggal" in r.json()["detail"].lower()
    assert web.post("/api/dates/manual", json={"unit_ids": [], "year": 2019, "month": 2}).status_code == 400
    assert web.post("/api/dates/manual", json={"unit_ids": ["nope"], "year": 2019, "month": 2}).status_code == 404

    r = web.post("/api/dates/manual", json={"unit_ids": [a, b], "year": 2019, "month": 5})
    assert r.status_code == 200
    assert r.json()["dated"] == 2
    eff = store.effective_dates(conn, [a, b])
    assert eff[a] == ("2019-05-01T12:00:00", "manual", "month")
    rows = conn.execute("select distinct source, decided_by from date_overrides").fetchall()
    assert [tuple(x) for x in rows] == [("manual", "owner")]

    r = web.post("/api/dates/manual", json={"unit_ids": [a], "year": 2019, "month": 5, "day": 17})
    assert r.status_code == 200
    assert store.effective_dates(conn, [a])[a] == ("2019-05-17T12:00:00", "manual", "day")
    assert conn.execute("select count(*) from date_overrides where unit_id=?", (a,)).fetchone()[0] == 1
    assert web.get("/api/dates/undated").json()["total"] == 0
    # Nothing moved on disk or in the database.
    assert conn.execute("select location from units where id=?", (a,)).fetchone()[0] == "archive"


def test_mutations_need_same_origin(web, bali):
    no = {"X-ArsipBersih": ""}
    assert web.post("/api/dates/events/bali/approve", headers=no,
                    json={"range_start": "2024-07-10", "range_end": "2024-07-28"}).status_code == 403
    assert web.post("/api/dates/events/bali/ignore", headers=no).status_code == 403
    assert web.post("/api/dates/manual", headers=no,
                    json={"unit_ids": ["x"], "year": 2019, "month": 5}).status_code == 403
    assert web.post("/api/dates/write", headers=no).status_code == 403
    conn, _ = bali
    assert conn.execute("select count(*) from event_date_decisions").fetchone()[0] == 0


def test_mutations_refuse_while_job_runs(web, bali):
    import threading

    gate = threading.Event()
    web.app_ref.state.jobs.start("scan", "Scan", lambda report, stop: gate.wait(5))
    try:
        r = web.post("/api/dates/events/bali/ignore")
        assert r.status_code == 409
        assert web.post("/api/dates/write").status_code == 409
    finally:
        gate.set()
        web.app_ref.state.jobs.wait()


def test_write_job_reports_counts(web, conn_with, root):
    conn = conn_with([
        {"name": "IMG-20190501-WA0001.jpg", "path": "Arsip/2019/2019-05", "loc": "archive",
         "taken": "2019-05-01T00:00:00", "date_source": "filename"},
    ])
    (root / "Arsip/2019/2019-05").mkdir(parents=True)
    (root / "Arsip/2019/2019-05/IMG-20190501-WA0001.jpg").write_bytes(b"x")
    r = web.post("/api/dates/write")
    assert r.status_code == 202
    assert r.json()["label"] == "Tulis tanggal"
    st = web.app_ref.state.jobs.wait()
    assert st.status == "done", st.error
    assert st.result["written"] == 1
    assert "1 file .xmp ditulis" in st.result["text"]
    assert (root / "Arsip/2019/2019-05/IMG-20190501-WA0001.jpg.xmp").exists()
    conn.close()


def test_write_rejects_bad_timezone(web, conn_with):
    conn = conn_with([])
    conn.execute("insert or replace into settings(key, value) values('timezone', '\"Mars/Base\"')")
    conn.commit()
    r = web.post("/api/dates/write")
    assert r.status_code == 400
    assert "Zona waktu" in r.json()["detail"]
    conn.close()


def test_dates_page_renders(web, bali):
    r = web.get("/tanggal")
    assert r.status_code == 200
    html = r.text
    for text in ("Tanggal acara", "Setujui", "Abaikan", "Ubah rentang", "Tanpa tanggal",
                 "Isi tanggal", "Biarkan", "Tulis tanggal ke file .xmp", "Rapikan"):
        assert text in html, text
    assert 'href="/tanggal"' in html  # sidebar entry under Review
    counts = web.get("/api/dates/counts").json()
    assert counts == {"proposals": 1, "undated": 0}
