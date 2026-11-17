import pytest

from arsipbersih.classify import run_classify
from arsipbersih.config import Settings


@pytest.mark.parametrize("row,expected", [
    (dict(name="x.jpg", decode_ok=0), "rusak"),
    (dict(name="Screenshot_20230812-143022.png"), "screenshot"),
    (dict(name="a.png", w=1080, h=2400), "screenshot"),
    (dict(name="IMG-20230812-WA0001.jpg", path="Sumber/HP/WhatsApp/Media/WhatsApp Images"), "kiriman-chat"),
    (dict(name="meme.webp"), "download-meme"),
    (dict(name="GL010123.LRV", kind="gopro_aux"), "pendamping-gopro"),
    (dict(name=".trashed-1690000000-IMG_1.jpg"), "sampah-hp"),
    (dict(name="VID_1.mp4", kind="video", duration=1.4), "video-pendek-gelap"),
    (dict(name="IMG_1.jpg", make="samsung", sharp=6, luma=12), "kepencet"),
    (dict(name="IMG_2.jpg", make="samsung", sharp=18, luma=120, std=50), "burem"),
    (dict(name="IMG_3.jpg", make="samsung", sharp=80, luma=8, std=30), "gelap-terang"),
    (dict(name="IMG_4.jpg", make="samsung", sharp=80, luma=120, std=50), None),
])
def test_primary_category(conn_with, row, expected):
    conn = conn_with([row]); run_classify(conn, Settings())
    assert conn.execute("select primary_category from units").fetchone()[0] == expected


def test_priority_and_secondary_flags(conn_with):
    conn = conn_with([dict(name="IMG-20230812-WA0001.jpg", path="Sumber/HP/WhatsApp", sharp=5, luma=120, std=50)])
    run_classify(conn, Settings())
    assert conn.execute("select primary_category from units").fetchone()[0] == "kiriman-chat"
    cats = {r[0] for r in conn.execute("select category from flags")}
    assert cats == {"kiriman-chat", "burem"}


def test_calibrated_threshold_overrides_fallback(conn_with):
    conn = conn_with([dict(name="IMG_2.jpg", make="samsung", sharp=30, luma=120, std=50)])
    run_classify(conn, Settings(blur_threshold=35.0))
    assert conn.execute("select primary_category from units").fetchone()[0] == "burem"


def test_bad_companion_file_does_not_make_unit_rusak(conn_with):
    from arsipbersih.db import new_id

    conn = conn_with([dict(name="IMG_1.HEIC", make="apple", sharp=80, luma=120, std=50)])
    uid = conn.execute("select id from units").fetchone()[0]
    fid = new_id()
    conn.execute(
        "insert into files(id, unit_id, rel_path, orig_rel_path, size, mtime_ns, kind, ext, source_label)"
        " values(?,?,?,?,?,?,?,?,?)",
        (fid, uid, "Sumber/HP/DCIM/IMG_1.MOV", "Sumber/HP/DCIM/IMG_1.MOV", 2_000_000, 0, "video", "mov", "HP"),
    )
    conn.execute("insert into media_info(file_id, decode_ok) values(?, 0)", (fid,))
    run_classify(conn, Settings())
    assert conn.execute("select primary_category from units").fetchone()[0] is None
    assert conn.execute("select count(*) from flags where category='rusak'").fetchone()[0] == 0


# --- AI flags (Fase 1.5) ---------------------------------------------------------------------

def _ai_unit_id(conn):
    return conn.execute("select id from units").fetchone()[0]


def _put_label(conn, uid, grp, prob, sub="struk", head="zeroshot"):
    from arsipbersih.ai import store

    store.save_labels(conn, head, "v1", [(uid, grp, sub, prob)])


def _flags(conn):
    return {r["category"]: r["reason"] for r in conn.execute("select category, reason from flags")}


def test_ai_label_adds_document_category_above_chat(conn_with):
    conn = conn_with([dict(name="IMG-20230812-WA0001.jpg", path="Sumber/HP/WhatsApp", make="samsung",
                           sharp=80, luma=120, std=50)])
    _put_label(conn, _ai_unit_id(conn), "keuangan", 0.9)
    run_classify(conn, Settings())
    assert conn.execute("select primary_category from units").fetchone()[0] == "dokumen-keuangan"
    flags = _flags(conn)
    assert set(flags) == {"dokumen-keuangan", "kiriman-chat"}
    assert flags["dokumen-keuangan"] == "AI: Keuangan · struk (0,90)"


def test_ai_never_removes_rule_flag(conn_with):
    conn = conn_with([dict(name="a.png", w=1080, h=2400)])
    _put_label(conn, _ai_unit_id(conn), "lainnya", 0.99, sub=None)
    run_classify(conn, Settings())
    assert conn.execute("select primary_category from units").fetchone()[0] == "screenshot"
    assert set(_flags(conn)) == {"screenshot"}


def test_ai_same_category_as_rule_keeps_rule_flag(conn_with):
    conn = conn_with([dict(name="a.png", w=1080, h=2400)])
    _put_label(conn, _ai_unit_id(conn), "layar", 0.95, sub="layar")
    run_classify(conn, Settings())
    assert conn.execute("select count(*) from flags where category='screenshot'").fetchone()[0] == 1
    assert "AI" not in _flags(conn)["screenshot"]


def test_correction_overrides_head_and_survives_reclassify(conn_with):
    from arsipbersih.ai import store

    conn = conn_with([dict(name="IMG_4.jpg", make="samsung", sharp=80, luma=120, std=50)])
    uid = _ai_unit_id(conn)
    _put_label(conn, uid, "keuangan", 0.9)
    store.add_feedback(conn, uid, "kertas", "koreksi")
    run_classify(conn, Settings())
    run_classify(conn, Settings())
    assert _flags(conn) == {"dokumen-kertas": "Dikoreksi pemilik: Kertas & catatan"}
    assert conn.execute("select primary_category from units").fetchone()[0] == "dokumen-kertas"


def test_confirmation_does_not_change_head_label(conn_with):
    from arsipbersih.ai import store

    conn = conn_with([dict(name="IMG_4.jpg", make="samsung", sharp=80, luma=120, std=50)])
    uid = _ai_unit_id(conn)
    _put_label(conn, uid, "keuangan", 0.9)
    store.add_feedback(conn, uid, "kertas", "konfirmasi")
    run_classify(conn, Settings())
    assert list(_flags(conn)) == ["dokumen-keuangan"]


def test_lainnya_correction_suppresses_ai_flag(conn_with):
    from arsipbersih.ai import store

    conn = conn_with([dict(name="IMG_4.jpg", make="samsung", sharp=80, luma=120, std=50)])
    uid = _ai_unit_id(conn)
    _put_label(conn, uid, "keuangan", 0.9)
    store.add_feedback(conn, uid, "lainnya", "koreksi")
    run_classify(conn, Settings())
    assert _flags(conn) == {}
    assert conn.execute("select primary_category from units").fetchone()[0] is None


def test_correction_without_any_label_still_flags(conn_with):
    from arsipbersih.ai import store

    conn = conn_with([dict(name="IMG_4.jpg", make="samsung", sharp=80, luma=120, std=50)])
    store.add_feedback(conn, _ai_unit_id(conn), "identitas", "koreksi")
    run_classify(conn, Settings())
    assert list(_flags(conn)) == ["dokumen-identitas"]


def test_archive_units_get_no_ai_flag(conn_with):
    conn = conn_with([dict(name="IMG_4.jpg", path="Arsip/2023/2023-08", loc="archive", make="samsung")])
    _put_label(conn, _ai_unit_id(conn), "keuangan", 0.95)
    run_classify(conn, Settings())
    assert _flags(conn) == {}


def test_ai_uses_head_from_settings(conn_with):
    conn = conn_with([dict(name="IMG_4.jpg", make="samsung", sharp=80, luma=120, std=50)])
    uid = _ai_unit_id(conn)
    _put_label(conn, uid, "keuangan", 0.9)
    _put_label(conn, uid, "layar", 0.8, sub="layar", head="trained-v1")
    run_classify(conn, Settings(ai_head="trained-v1"))
    assert list(_flags(conn)) == ["screenshot"]


def test_classify_command_relabels_without_opening_files(tmp_path, monkeypatch):
    import json

    import numpy as np
    from typer.testing import CliRunner

    from arsipbersih import visual
    from arsipbersih.ai import heads, store
    from arsipbersih.ai.heads import DEFAULT_PROMPTS, GROUPS
    from arsipbersih.ai.model import MODEL_NAME
    from arsipbersih.cli import app
    from arsipbersih.db import open_db

    cfg = json.loads(DEFAULT_PROMPTS.read_text(encoding="utf-8"))
    cfg["groups"] = {g: [{"prompt": f"item {g}", "sub": f"sub-{g}"}] for g in GROUPS}
    prompts = tmp_path / "prompts.json"
    prompts.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(heads, "DEFAULT_PROMPTS", prompts)

    def fake_text(texts):
        out = np.zeros((len(texts), len(GROUPS)), np.float32)
        for i, t in enumerate(texts):
            out[i, [j for j, g in enumerate(GROUPS) if f"item {g}" in t][0]] = 1.0
        return out

    real_get_head = heads.get_head
    monkeypatch.setattr("arsipbersih.ai.stage.get_head",
                        lambda name, root=None: real_get_head(name, root, text_emb_fn=fake_text))

    def boom(*a, **k):
        raise AssertionError("classify must not decode images")

    monkeypatch.setattr(visual, "_load_rgb", boom)

    conn = open_db(tmp_path)
    conn.execute("insert into units (id, primary_file_id, location) values ('u1', 'f1', 'source')")
    conn.execute("insert into files (id, unit_id, rel_path, orig_rel_path, kind, ext, missing)"
                 " values ('f1', 'u1', 'Sumber/HP/a.jpg', 'Sumber/HP/a.jpg', 'photo', 'jpg', 0)")
    conn.commit()
    vec = np.zeros(len(GROUPS), np.float32)
    vec[GROUPS.index("keuangan")] = 1.0
    store.save_embeddings(conn, MODEL_NAME, [("u1", vec)])
    conn.close()

    r = CliRunner().invoke(app, ["classify", str(tmp_path)])
    assert r.exit_code == 0, r.output
    assert "dokumen-keuangan: 1" in r.output
    conn = open_db(tmp_path)
    assert conn.execute("select primary_category from units").fetchone()[0] == "dokumen-keuangan"
    conn.close()

    r = CliRunner().invoke(app, ["classify", str(tmp_path), "--head", "nope"])
    assert r.exit_code == 1
    assert "nope" in r.output


def test_reclassify_units_matches_run_classify_and_leaves_others(conn_with):
    from arsipbersih.ai import store
    from arsipbersih.classify import reclassify_units

    conn = conn_with([
        dict(name="IMG-20230812-WA0001.jpg", path="Sumber/HP/WhatsApp", sharp=5, luma=120, std=50),
        dict(name="a.png", w=1080, h=2400),
    ])
    a, b = [r[0] for r in conn.execute("select u.id from units u join files f on f.id = u.primary_file_id"
                                        " order by f.rel_path desc")]
    run_classify(conn, Settings())
    before_b = sorted(tuple(r) for r in conn.execute("select id, category, reason from flags where unit_id=?", (b,)))
    store.add_feedback(conn, a, "keuangan", "koreksi")
    assert reclassify_units(conn, Settings(), [a]) == {a: "dokumen-keuangan"}
    got = sorted(tuple(r) for r in conn.execute("select category, score, reason from flags where unit_id=?", (a,)))
    # b keeps its exact rows (same ids): it was not rewritten
    assert sorted(tuple(r) for r in conn.execute("select id, category, reason from flags where unit_id=?", (b,))) == before_b
    run_classify(conn, Settings())
    assert sorted(tuple(r) for r in conn.execute("select category, score, reason from flags where unit_id=?", (a,))) == got
    assert {c for c, _, _ in got} == {"dokumen-keuangan", "kiriman-chat", "burem"}


# --- review state follows the category, never across it (spec 7.7) ------------------------

def _chat_review_units(conn_with, n):
    rows = [dict(name=f"IMG-20230812-WA000{i}.jpg", path="_review/kiriman-chat/HP/WhatsApp", loc="review",
                 make="samsung", sharp=80, luma=120, std=50) for i in range(n)]
    conn = conn_with(rows)
    run_classify(conn, Settings())
    ids = [r[0] for r in conn.execute("select u.id from units u join files f on f.id = u.primary_file_id"
                                      " order by f.rel_path")]
    assert {r[0] for r in conn.execute("select primary_category from units")} == {"kiriman-chat"}
    return conn, ids


def _state(conn, uid):
    seen = conn.execute("select seen from units where id=?", (uid,)).fetchone()[0]
    dec = conn.execute("select decision from decisions where unit_id=?", (uid,)).fetchone()
    return seen, dec[0] if dec else None


def test_run_classify_drops_seen_and_pending_discard_when_category_changes(conn_with):
    from arsipbersih import decisions

    conn, (moved, stays, kept) = _chat_review_units(conn_with, 3)
    decisions.mark_seen(conn, [moved, stays, kept])
    assert decisions.record_bulk_discard(conn, [moved, stays]) == 2
    decisions.record(conn, kept, "keep")
    _put_label(conn, moved, "identitas", 0.97, sub="ktp")
    _put_label(conn, kept, "keuangan", 0.9)
    run_classify(conn, Settings())

    assert decisions.preview_apply(conn, "dokumen-identitas").discard == 0
    assert _state(conn, moved) == (0, None)
    # an unapplied Keep is never dropped; only the implicit/explicit discard is
    assert _state(conn, kept) == (0, "keep")
    assert decisions.preview_apply(conn, "dokumen-keuangan").keep == 1
    # a unit that keeps its category keeps its review state
    assert _state(conn, stays) == (1, "discard")
    assert decisions.preview_apply(conn, "kiriman-chat").discard == 1


def test_reclassify_units_drops_seen_and_pending_discard_when_category_changes(conn_with):
    from arsipbersih import decisions
    from arsipbersih.classify import reclassify_units

    conn, (moved, stays) = _chat_review_units(conn_with, 2)
    decisions.mark_seen(conn, [moved, stays])
    decisions.record_bulk_discard(conn, [moved, stays])
    _put_label(conn, moved, "kertas", 0.9)
    _put_label(conn, stays, "lainnya", 0.9, sub=None)
    reclassify_units(conn, Settings(), [moved, stays])
    assert _state(conn, moved) == (0, None)
    assert _state(conn, stays) == (1, "discard")
    assert decisions.preview_apply(conn, "dokumen-kertas").discard == 0


def test_applied_decision_is_never_touched_by_reclassify(conn_with):
    from arsipbersih.classify import reset_review_state

    conn, (uid,) = _chat_review_units(conn_with, 1)
    conn.execute("insert into decisions (unit_id, decision, decided_at, applied)"
                 " values (?, 'discard', 'x', 1)", (uid,))
    conn.commit()
    with conn:
        reset_review_state(conn, uid)
    assert _state(conn, uid) == (0, "discard")


# --- classify --head: alias names and persistence -------------------------------------------

def _classify_root(tmp_path, monkeypatch):
    """A root with one embedded source unit the fake zero-shot head labels keuangan."""
    import json

    import numpy as np

    from arsipbersih.ai import heads, store
    from arsipbersih.ai.heads import DEFAULT_PROMPTS, GROUPS
    from arsipbersih.ai.model import MODEL_NAME
    from arsipbersih.db import open_db

    cfg = json.loads(DEFAULT_PROMPTS.read_text(encoding="utf-8"))
    cfg["groups"] = {g: [{"prompt": f"item {g}", "sub": f"sub-{g}"}] for g in GROUPS}
    prompts = tmp_path / "prompts.json"
    prompts.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(heads, "DEFAULT_PROMPTS", prompts)

    def fake_text(texts):
        out = np.zeros((len(texts), len(GROUPS)), np.float32)
        for i, t in enumerate(texts):
            out[i, [j for j, g in enumerate(GROUPS) if f"item {g}" in t][0]] = 1.0
        return out

    real_get_head = heads.get_head
    monkeypatch.setattr("arsipbersih.ai.stage.get_head",
                        lambda name, root=None: real_get_head(name, root, text_emb_fn=fake_text))
    conn = open_db(tmp_path)
    conn.execute("insert into units (id, primary_file_id, location) values ('u1', 'f1', 'source')")
    conn.execute("insert into files (id, unit_id, rel_path, orig_rel_path, kind, ext, missing)"
                 " values ('f1', 'u1', 'Sumber/HP/a.jpg', 'Sumber/HP/a.jpg', 'photo', 'jpg', 0)")
    conn.commit()
    vec = np.zeros(len(GROUPS), np.float32)
    vec[GROUPS.index("keuangan")] = 1.0
    store.save_embeddings(conn, MODEL_NAME, [("u1", vec)])
    conn.close()


def _stored_head_and_category(tmp_path):
    from arsipbersih.config import load_settings
    from arsipbersih.db import open_db

    conn = open_db(tmp_path)
    try:
        return (load_settings(conn).ai_head,
                conn.execute("select primary_category from units").fetchone()[0])
    finally:
        conn.close()


def test_classify_head_alias_persists_canonical_name_and_keeps_flags(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from arsipbersih.cli import app

    _classify_root(tmp_path, monkeypatch)
    r = CliRunner().invoke(app, ["classify", str(tmp_path), "--head", "zeroshot-v1"])
    assert r.exit_code == 0, r.output
    assert "dokumen-keuangan: 1" in r.output
    assert "Head aktif sekarang" not in r.output  # same head, just another name
    assert _stored_head_and_category(tmp_path) == ("zeroshot", "dokumen-keuangan")


def test_already_persisted_alias_still_finds_labels(tmp_path, monkeypatch):
    from dataclasses import replace

    from arsipbersih.ai import store
    from arsipbersih.ai.heads import canonical_head_name
    from arsipbersih.classify import run_classify as classify_all
    from arsipbersih.config import load_settings, save_settings
    from arsipbersih.db import open_db

    _classify_root(tmp_path, monkeypatch)
    conn = open_db(tmp_path)
    store.save_labels(conn, "zeroshot", "v1", [("u1", "keuangan", "struk", 0.9)])
    save_settings(conn, replace(load_settings(conn), ai_head="zeroshot-v1"))  # older setting
    classify_all(conn, load_settings(conn))
    assert conn.execute("select primary_category from units").fetchone()[0] == "dokumen-keuangan"
    conn.close()
    assert canonical_head_name("zeroshot-v1") == "zeroshot"
    assert canonical_head_name("trained-v2") == "trained-v2"


def test_classify_invalid_head_exits_and_keeps_setting(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from arsipbersih.cli import app

    _classify_root(tmp_path, monkeypatch)
    for bad in ("nope", "trained-v9"):
        r = CliRunner().invoke(app, ["classify", str(tmp_path), "--head", bad])
        assert r.exit_code == 1
        assert bad in r.output
    assert _stored_head_and_category(tmp_path) == ("zeroshot", None)


def _trained_head(tmp_path):
    import numpy as np

    from arsipbersih.ai.heads import GROUPS, trained_head_dir

    d = trained_head_dir(tmp_path)
    d.mkdir(parents=True)
    w = np.eye(len(GROUPS), dtype=np.float32)[[GROUPS.index("keuangan"), GROUPS.index("lainnya")]] * 10
    np.savez(d / "trained-v1.npz", weights=w, bias=np.zeros(2, np.float32),
             groups=np.array(["keuangan", "lainnya"]))


def test_classify_valid_head_is_persisted_after_relabel(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from arsipbersih.cli import app

    _classify_root(tmp_path, monkeypatch)
    _trained_head(tmp_path)
    r = CliRunner().invoke(app, ["classify", str(tmp_path), "--head", "trained-v1"])
    assert r.exit_code == 0, r.output
    assert "Head aktif sekarang: trained-v1" in r.output
    assert _stored_head_and_category(tmp_path) == ("trained-v1", "dokumen-keuangan")


def test_classify_head_not_persisted_when_relabel_fails(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from arsipbersih.cli import app

    _classify_root(tmp_path, monkeypatch)
    _trained_head(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("text model broken")

    monkeypatch.setattr("arsipbersih.ai.stage.relabel_all", boom)
    r = CliRunner().invoke(app, ["classify", str(tmp_path), "--head", "trained-v1"])
    assert r.exit_code != 0
    assert _stored_head_and_category(tmp_path) == ("zeroshot", None)
