import threading

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
