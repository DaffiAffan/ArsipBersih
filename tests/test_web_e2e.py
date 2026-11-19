from __future__ import annotations

from conftest import requires_exiftool

MEDIA_EXT = {".jpg", ".jpeg", ".png", ".mp4", ".mov", ".3gp", ".heic", ".dng"}


def _media(base):
    if not base.exists():
        return set()
    return {p for p in base.rglob("*") if p.is_file() and p.suffix.lower() in MEDIA_EXT}


@requires_exiftool
def test_scan_tidy_review_apply_undo_roundtrip(web, mini_collection):
    root = mini_collection
    jobs = web.app_ref.state.jobs
    before = {p.relative_to(root).as_posix() for p in _media(root / "Sumber")}
    assert before

    assert web.post("/api/scan").status_code == 202
    assert jobs.wait(120).status == "done"
    assert web.post("/api/tidy").status_code == 202
    assert jobs.wait(60).status == "done"

    cats = {c["category"]: c for c in web.get("/api/review/categories").json()}
    assert "duplikat-persis" in cats and "burem" in cats, sorted(cats)

    def apply(category):
        pv = web.get(f"/api/review/apply/{category}").json()
        r = web.post(f"/api/review/apply/{category}", json={"keep": pv["keep"], "discard": pv["discard"]})
        assert r.status_code == 202, r.text
        assert jobs.wait(60).status == "done"

    groups = web.get("/api/review/groups/duplikat-persis").json()["groups"]
    assert groups
    web.post("/api/review/accept", json={"group_ids": [g["id"] for g in groups]})
    apply("duplikat-persis")

    items = web.get("/api/review/grid/burem").json()["items"]
    assert items
    web.post("/api/review/decisions", json={"unit_ids": [i["unit_id"] for i in items], "decision": "discard"})
    apply("burem")

    assert web.post("/api/report/verify").status_code == 202
    final = jobs.wait(60)
    assert final.status == "done" and final.result["ok"] is True

    assert web.post("/api/undo", json={"all": True}).status_code == 202
    assert jobs.wait(60).status == "done"

    for d in ("Arsip", "_review", "_sampah"):
        assert not _media(root / d), d
    after = {p.relative_to(root).as_posix() for p in _media(root / "Sumber")}
    assert after == before
