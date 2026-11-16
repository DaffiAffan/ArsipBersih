from __future__ import annotations


def _rows():
    return [
        {"name": "a.jpg", "category": "burem", "taken": "2020-01-01T00:00:00"},
        {"name": "b.jpg", "taken": None},
    ]


def test_report_endpoint(web, conn_with):
    conn = conn_with(_rows())
    conn.close()
    r = web.get("/api/report")
    assert r.status_code == 200
    d = r.json()
    assert d["total_media"] == 2
    assert d["by_category"] == {"burem": 1}
    assert d["by_location"] == {"source": 2}
    assert d["undated"] == 1
    assert d["cache_bytes"] == 0
    page = web.get("/laporan")
    assert page.status_code == 200
    assert "Laporan" in page.text


def test_verify_job_reports_missing(web, root, conn_with):
    conn = conn_with(_rows())
    conn.close()
    assert web.post("/api/report/verify").status_code == 202
    state = web.app_ref.state.jobs.wait()
    assert state.status == "done", state.error
    res = state.result
    assert res["ok"] is False
    assert res["missing_total"] == 2
    assert "Sumber/HP/DCIM/a.jpg" in res["missing"]
    assert res["unexpected"] == [] and res["unexpected_total"] == 0


def test_verify_truncates_lists(web, root, conn_with):
    conn = conn_with([{"name": f"f{i:03}.jpg"} for i in range(205)])
    conn.close()
    web.post("/api/report/verify")
    res = web.app_ref.state.jobs.wait().result
    assert len(res["missing"]) == 200
    assert res["missing_total"] == 205


def test_cache_clear_only_touches_cache(web, root):
    thumb = root / ".arsipbersih/cache/thumb"
    thumb.mkdir(parents=True)
    (thumb / "x.webp").write_bytes(b"x" * 10)
    (root / ".arsipbersih/arsip.db").write_bytes(b"db")
    (root / "Sumber/HP").mkdir()
    keep = root / "Sumber/HP/a.jpg"
    keep.write_bytes(b"jpg")
    assert web.post("/api/cache/clear").status_code == 202
    state = web.app_ref.state.jobs.wait()
    assert state.status == "done", state.error
    assert state.result == {"removed_bytes": 10, "skipped": 0}
    assert not (thumb / "x.webp").exists()
    assert not list((root / ".arsipbersih/cache").iterdir())
    assert keep.exists()
    assert (root / ".arsipbersih/arsip.db").exists()


def test_cache_clear_without_cache_dir(web, root):
    web.post("/api/cache/clear")
    state = web.app_ref.state.jobs.wait()
    assert state.status == "done", state.error
    assert state.result == {"removed_bytes": 0, "skipped": 0}


def test_cache_clear_skips_locked_files(web, root, monkeypatch):
    import os
    import shutil

    cache = root / ".arsipbersih/cache/thumb"
    cache.mkdir(parents=True)
    (cache / "a.webp").write_bytes(b"a" * 10)
    locked = cache / "b.webp"
    locked.write_bytes(b"b" * 7)
    real_unlink = os.unlink

    def unlink(path, *a, **kw):  # simulate a file Windows keeps open
        if str(path).endswith("b.webp") or getattr(path, "name", "") == "b.webp":
            raise PermissionError(13, "in use", str(path))
        return real_unlink(path, *a, **kw)

    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(shutil.os, "unlink", unlink)
    web.post("/api/cache/clear")
    state = web.app_ref.state.jobs.wait()
    assert state.status == "done", state.error
    assert state.result == {"removed_bytes": 10, "skipped": 1}
    assert locked.exists() and not (cache / "a.webp").exists()


def test_cache_clear_refuses_junction(web, root, monkeypatch):
    from pathlib import Path

    cache = root / ".arsipbersih/cache"
    cache.mkdir(parents=True)
    f = cache / "x.webp"
    f.write_bytes(b"x")
    real = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda self: self.name == "cache" or real(self))
    web.post("/api/cache/clear")
    state = web.app_ref.state.jobs.wait()
    assert state.status == "failed"
    assert f.exists()
