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
    assert r.json()["detail"].startswith("Pindai terakhir belum selesai")


def test_plan_page_renders(web):
    assert web.get("/rencana").status_code == 200
