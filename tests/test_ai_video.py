from __future__ import annotations

import numpy as np
import pytest

from arsipbersih.ai import store, train
from arsipbersih.ai.heads import UnitFacts
from arsipbersih.ai.model import MODEL_NAME
from arsipbersih.ai.stage import has_camera_data, is_tall_screen_ratio, unit_facts
from arsipbersih.classify import run_classify
from arsipbersih.config import Settings
from arsipbersih.grouping import load_unit_views
from arsipbersih.metadata import extract_exif_fields
from test_ai_train import COUNTS, make_collection, prompts, run_train, use_test_prompts  # noqa: F401
from test_classify import _flags, _put_label


def test_quicktime_make_model_read():
    d = {"Keys:Make": "Apple", "Keys:Model": "iPhone 13", "Keys:GPSCoordinates": "-6.2 106.8",
         "QuickTime:CreateDate": "2023:08:12 07:30:22"}
    f = extract_exif_fields(d)
    assert (f["make"], f["model"], f["has_gps"]) == ("Apple", "iPhone 13", 1)
    assert extract_exif_fields({"IFD0:Make": "samsung", "Keys:Make": "x"})["make"] == "samsung"


def _video(conn_with, **kw):
    row = dict(name="clip.mp4", kind="video", w=1280, h=720, duration=30.0, **kw)
    conn = conn_with([row])
    return conn, conn.execute("select id from units").fetchone()[0]


def test_video_with_gps_counts_as_camera(conn_with):
    conn, uid = _video(conn_with)
    assert not has_camera_data(load_unit_views(conn)[0])
    conn.execute("update media_info set has_gps=1")
    assert has_camera_data(load_unit_views(conn)[0])
    conn.execute("update media_info set has_gps=0, qt_datetime='2023-08-12'")
    assert has_camera_data(load_unit_views(conn)[0])


def test_camera_file_name_counts_but_chat_name_does_not(conn_with):
    conn = conn_with([dict(name="PXL_20230812.mp4", kind="video"),
                      dict(name="VID-20230812-WA0001.mp4", kind="video", path="Sumber/HP/WhatsApp")])
    cam = {v.filename: has_camera_data(v) for v in load_unit_views(conn)}
    assert cam == {"PXL_20230812.mp4": True, "VID-20230812-WA0001.mp4": False}


def test_wa_169_video_without_metadata_not_pushed_to_screenshot(conn_with):
    conn, _ = _video(conn_with, path="Sumber/HP/WhatsApp")
    f = unit_facts(load_unit_views(conn)[0])
    assert not f.screen_ratio and not f.has_camera
    assert not is_tall_screen_ratio(1920, 1080)


def test_tall_screen_recording_gets_layar_hint(conn_with):
    conn = conn_with([dict(name="rec.mp4", kind="video", w=1080, h=2400)])
    f = unit_facts(load_unit_views(conn)[0])
    assert f.screen_ratio and not f.has_camera
    assert is_tall_screen_ratio(2400, 1080) and not is_tall_screen_ratio(1080, 2159)


def test_video_document_label_is_ignored(conn_with):
    conn, uid = _video(conn_with, make="samsung")
    _put_label(conn, uid, "keuangan", 0.95)
    run_classify(conn, Settings())
    assert "dokumen-keuangan" not in _flags(conn)


def test_video_grafis_label_flags_download_meme(conn_with):
    conn, uid = _video(conn_with, make="samsung")
    _put_label(conn, uid, "grafis", 0.8, sub="meme")
    run_classify(conn, Settings())
    assert "download-meme" in _flags(conn)
    _put_label(conn, uid, "layar", 0.65, sub="layar")  # below the video threshold
    run_classify(conn, Settings())
    assert "screenshot" not in _flags(conn)


def test_train_head_excludes_video_feedback(tmp_path, prompts):  # noqa: F811
    conn, _ = make_collection(tmp_path, COUNTS)
    conn.execute("insert into units (id, primary_file_id, location) values ('vid', 'f-vid', 'source')")
    conn.execute("insert into files (id, unit_id, rel_path, kind, missing) values ('f-vid', 'vid', 'v.mp4', 'video', 0)")
    store.save_embeddings(conn, MODEL_NAME, [("vid", np.ones(12, np.float32) / np.sqrt(12))])
    store.add_feedback(conn, "vid", "layar", "koreksi")
    res = run_train(conn, tmp_path, prompts, min_per_group=20)
    assert res.n_examples["layar"] == 40 - sum(1 for k in range(40) if train.is_holdout(f"layar-{k:03d}", 0.2, 42))


def test_ai_report_counts_video_examples(tmp_path, prompts):  # noqa: F811
    conn, _ = make_collection(tmp_path, {"lainnya": 2})
    conn.execute("insert into units (id, primary_file_id, location) values ('vid', 'f-vid', 'source')")
    conn.execute("insert into files (id, unit_id, rel_path, kind, missing) values ('f-vid', 'vid', 'v.mp4', 'video', 0)")
    store.add_feedback(conn, "vid", "grafis", "koreksi")
    assert "Contoh koreksi video: 1 (belum dipakai melatih)" in train.ai_report(conn, tmp_path)


def test_wa_video_with_only_creation_time_is_flagged_download_meme(conn_with):
    conn = conn_with([dict(name="VID-20230812-WA0001.mp4", kind="video", path="Sumber/HP/WhatsApp",
                           w=720, h=1280, duration=12.0)])
    conn.execute("update media_info set qt_datetime='2023-08-12T10:00:00'")
    assert not has_camera_data(load_unit_views(conn)[0])
    _put_label(conn, conn.execute("select id from units").fetchone()[0], "grafis", 0.8, sub="meme")
    run_classify(conn, Settings())
    assert "download-meme" in _flags(conn)


def test_bogus_creation_time_is_not_camera_data(conn_with):
    conn = conn_with([dict(name="clip.mp4", kind="video")])
    conn.execute("update media_info set qt_datetime='1904-01-01T00:00:00'")
    assert not has_camera_data(load_unit_views(conn)[0])
