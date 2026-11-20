from __future__ import annotations


def _users_unit(conn_with):
    return conn_with([dict(name="IMG_1.jpg", path="Sumber/Laptop/Users/Bali", src="Laptop", taken="2023-06-01T10:00:00")])


def test_not_event_override_recomputes(web, conn_with):
    _users_unit(conn_with)
    names = [e["name"] for e in web.get("/api/plan").json()["events"]]
    assert "Users" in names
    r = web.post("/api/events/override", json={"kind": "not_event", "name": "Users"})
    assert r.status_code == 200
    names = [e["name"] for e in r.json()["events"]]
    assert "Users" not in names and "Bali" in names


def test_rename_requires_value(web):
    r = web.post("/api/events/override", json={"kind": "rename", "name": "A"})
    assert r.status_code == 400


def test_tidy_refused_before_scan_done(web, conn_with):
    _users_unit(conn_with)
    r = web.post("/api/tidy")
    assert r.status_code == 409
    assert r.json()["detail"].startswith("Scan terakhir belum selesai")


def test_plan_page_renders(web):
    assert web.get("/rencana").status_code == 200


def test_get_plan_does_not_write(web, conn_with):
    conn = _users_unit(conn_with)
    conn.execute("update units set event_key=null, event_name=null, event_subpath=null")
    conn.commit()
    assert web.get("/api/plan").status_code == 200
    row = conn.execute("select event_key, event_name from units").fetchone()
    assert tuple(row) == (None, None)  # nothing persisted by the GET


def test_plan_js_shares_modal_and_restores_focus_by_name(web):
    js = web.get("/static/pages/plan.js").text
    assert "modalDialog" in js and 'dlg.showModal' not in js
    assert "restoreFocus(ev.key, " in js


def test_plan_rows_use_row_menu(web):
    page = web.get("/rencana").text
    assert 'class="card"' not in page
    js = web.get("/static/pages/plan.js").text
    assert "Ganti nama" in js and "Gabungkan" in js and "Bukan acara" in js
