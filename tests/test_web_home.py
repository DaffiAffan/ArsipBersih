import threading

import pytest

from arsipbersih.inventory import InventoryResult
from arsipbersih.pipeline import Progress, ScanSummary
from arsipbersih.tools import ToolStatus


def _fake(called):
    def fake(root, progress=None, stop=None):
        progress(Progress("hash", 1, 2, None))
        called["root"] = root
        return ScanSummary(InventoryResult(), {"burem": 1}, [], False, ToolStatus(None, None, None), ["w1"])

    return fake


def test_scan_starts_a_job(web, monkeypatch):
    called = {}
    monkeypatch.setattr("arsipbersih.web.routes.home.run_scan", _fake(called))
    assert web.post("/api/scan").status_code == 202
    st = web.app_ref.state.jobs.wait()
    assert st.status == "done" and st.result["categories"] == {"burem": 1}
    assert called["root"] == web.app_ref.state.root
    assert web.get("/api/home").json()["warnings"] == ["w1"]


def test_root_switch_requires_sumber(web, tmp_path):
    r = web.post("/api/root", json={"path": str(tmp_path / "kosong")})
    assert r.status_code == 400 and "Sumber" in r.json()["detail"]


def test_root_switch_ok_and_refused_while_busy(web, tmp_path):
    new = tmp_path / "baru"
    (new / "Sumber").mkdir(parents=True)
    r = web.post("/api/root", json={"path": str(new)})
    assert r.status_code == 200 and web.app_ref.state.root == new
    gate = threading.Event()
    web.app_ref.state.jobs.start("x", "Tahan", lambda report, stop: gate.wait(5))
    try:
        assert web.post("/api/root", json={"path": str(new)}).status_code == 409
    finally:
        gate.set()
        web.app_ref.state.jobs.wait()


def test_home_data_and_page(web):
    d = web.get("/api/home").json()
    assert d["has_source"] and d["last_scan_at"] is None and d["warnings"] == []
    assert set(d["tools"]) == {"exiftool", "ffmpeg", "ffprobe"}
    assert web.get("/").status_code == 200


def test_root_switch_rejects_bad_paths(web, tmp_path):
    f = tmp_path / "berkas.txt"
    f.write_text("x")
    before = web.app_ref.state.root
    for p in (str(f), "foo", "", "   "):
        r = web.post("/api/root", json={"path": p})
        assert r.status_code == 400, p
    assert web.app_ref.state.root == before


def test_root_switch_db_failure_is_400(web, tmp_path, monkeypatch):
    import sqlite3

    new = tmp_path / "ok"
    (new / "Sumber").mkdir(parents=True)

    def boom(root, *a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("arsipbersih.web.routes.home.open_db", boom)
    before = web.app_ref.state.root
    r = web.post("/api/root", json={"path": str(new)})
    assert r.status_code == 400 and "Folder ini tidak bisa dipakai" in r.json()["detail"]
    assert web.app_ref.state.root == before


def test_root_switch_refused_when_new_root_is_locked(web, tmp_path):
    from arsipbersih.lock import acquire_root_lock

    new = tmp_path / "dipakai"
    (new / "Sumber").mkdir(parents=True)
    before = web.app_ref.state.root
    with acquire_root_lock(new):
        r = web.post("/api/root", json={"path": str(new)})
    assert r.status_code == 400
    assert r.json()["detail"] == "Koleksi ini sedang dipakai proses ArsipBersih lain."
    assert web.app_ref.state.root == before


def test_root_switch_moves_lock_and_reconciles(root, tmp_path):
    from fastapi.testclient import TestClient

    from arsipbersih.lock import RootLocked, acquire_root_lock
    from arsipbersih.web.app import create_app

    old_lock = acquire_root_lock(root)
    app = create_app(root, allowed_hosts={"testserver"}, start_jobs=True, root_lock=old_lock)
    new = tmp_path / "baru"
    (new / "Sumber").mkdir(parents=True)
    with TestClient(app, headers={"X-ArsipBersih": "1"}) as c:
        app.state.jobs.wait()  # startup reconcile
        r = c.post("/api/root", json={"path": str(new)})
        assert r.status_code == 200
        st = app.state.jobs.wait()
        assert st.kind == "reconcile" and st.status == "done"
        assert not old_lock.held
        acquire_root_lock(root).release()  # old collection is free again
        with pytest.raises(RootLocked):
            acquire_root_lock(new)  # the app holds the new one
        # switching to the collection it already holds is fine
        assert c.post("/api/root", json={"path": str(new)}).status_code == 200
        app.state.jobs.wait()
    acquire_root_lock(new).release()  # released on shutdown


def test_job_refused_when_root_switched_mid_request(web, tmp_path):
    from arsipbersih.web.deps import ROOT_CHANGED_TEXT, start_job

    class Req:
        pass

    req = Req()
    req.app = web.app_ref
    req.state = Req()
    req.state.root = tmp_path / "lama"  # the collection this request started on
    with pytest.raises(Exception) as ei:
        start_job(req, "x", "X", lambda report, stop: None)
    assert str(ei.value) == ROOT_CHANGED_TEXT
    assert not web.app_ref.state.jobs.busy()


def test_scan_refused_without_sumber(web, monkeypatch):
    called = {}
    monkeypatch.setattr("arsipbersih.web.routes.home.run_scan", _fake(called))
    (web.app_ref.state.root / "Sumber").rmdir()
    r = web.post("/api/scan")
    assert r.status_code == 400 and "Sumber" in r.json()["detail"]
    assert called == {}
    d = web.get("/api/home").json()
    assert d["has_source"] is False and "Sumber" in d["source_missing_text"]
