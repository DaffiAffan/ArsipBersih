def test_put_settings_validates(web):
    assert web.put("/api/settings", json={"workers": 0}).status_code == 400
    assert web.put("/api/settings", json={"timezone": "Mars/Base"}).status_code == 400
    assert web.put("/api/settings", json={"nope": 1}).status_code == 400
    assert web.put("/api/settings", json={"blur_threshold": 101}).status_code == 400
    assert web.put("/api/settings", json={"generic_folders": []}).status_code == 400


def test_put_settings_saves_and_flags_rescan(web):
    r = web.put("/api/settings", json={"burst_window_s": 5, "generic_folders": [" DCIM ", "dcim", "Kamera"]})
    assert r.status_code == 200 and r.json()["rescan_needed"] is True
    s = web.get("/api/settings").json()
    assert s["burst_window_s"] == 5 and s["generic_folders"] == ["DCIM", "Kamera"]
    assert s["blur_effective"] == s["blur_fallback"]
    r = web.put("/api/settings", json={"workers": 5})
    assert r.status_code == 200 and r.json()["rescan_needed"] is False


def test_blur_examples_split_at_threshold(web, conn_with):
    conn = conn_with(
        [{"name": f"p{i}.jpg", "sharp": float(s)} for i, s in enumerate([5, 10, 20, 40, 60, 90])]
        + [{"name": "v.mp4", "kind": "video", "sharp": 1.0}]
    )
    conn.close()
    r = web.get("/api/settings/blur-examples", params={"threshold": 30})
    assert r.status_code == 200
    d = r.json()
    assert [i["score"] for i in d["below"]] == [20.0, 10.0, 5.0]
    assert [i["score"] for i in d["above"]] == [40.0, 60.0, 90.0]
    assert d["below"][0]["thumb"] == f"/media/thumb/{d['below'][0]['file_id']}"
    assert web.get("/api/settings/blur-examples", params={"threshold": 150}).status_code == 400


def test_calibrate_suggests_without_saving(web, conn_with):
    assert web.post("/api/settings/calibrate").status_code == 400
    conn_with([{"name": f"p{i}.jpg", "make": "Canon", "sharp": 10.0 + i} for i in range(10)]).close()
    r = web.post("/api/settings/calibrate")
    assert r.status_code == 200 and r.json()["suggested"] > 0
    assert web.get("/api/settings").json()["blur_threshold"] is None


def test_settings_page_renders(web):
    r = web.get("/pengaturan")
    assert r.status_code == 200 and "Pengaturan" in r.text
