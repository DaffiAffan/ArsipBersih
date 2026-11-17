from __future__ import annotations

import json

import numpy as np
import pytest
from typer.testing import CliRunner

from arsipbersih.ai import store, train
from arsipbersih.ai.heads import DEFAULT_PROMPTS, GROUPS, UnitFacts, get_head
from arsipbersih.ai.model import MODEL_NAME
from arsipbersih.db import open_db

DIM = 12
PLAIN = UnitFacts(has_camera=False, is_png=False, screen_ratio=False, download_like=False)


@pytest.fixture
def prompts(tmp_path):
    cfg = json.loads(DEFAULT_PROMPTS.read_text(encoding="utf-8"))
    cfg["groups"] = {g: [{"prompt": f"item {g}", "sub": f"sub-{g}"}] for g in GROUPS}
    path = tmp_path / "prompts.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def use_test_prompts(prompts, monkeypatch):
    """get_head() builds heads with the default prompt file; point it at the one-prompt-per-group file."""
    from arsipbersih.ai import heads

    monkeypatch.setattr(heads, "DEFAULT_PROMPTS", prompts)


def fake_text(texts):
    """Zero-shot text vectors live in dims 0..5; the synthetic images live in dims 6..11."""
    out = np.zeros((len(texts), DIM), np.float32)
    for i, t in enumerate(texts):
        out[i, [j for j, g in enumerate(GROUPS) if f"item {g}" in t][0]] = 1.0
    return out


def _add_unit(conn, uid):
    fid = "f-" + uid
    conn.execute("insert into units (id, primary_file_id, location) values (?, ?, 'source')", (uid, fid))
    conn.execute(
        "insert into files (id, unit_id, rel_path, kind, missing) values (?, ?, ?, 'photo', 0)",
        (fid, uid, uid + ".jpg"),
    )


def make_collection(root, counts: dict[str, int], seed=0):
    conn = open_db(root)
    rng = np.random.default_rng(seed)
    rows, truth = [], {}
    for g, n in counts.items():
        for k in range(n):
            uid = f"{g}-{k:03d}"
            _add_unit(conn, uid)
            v = np.zeros(DIM, np.float32)
            v[6:] = rng.normal(0, 0.15, DIM - 6)  # no energy in dims 0..5: zero-shot sees a uniform image
            v[6 + GROUPS.index(g)] += 1.0
            rows.append((uid, v / np.linalg.norm(v)))
            truth[uid] = g
    conn.commit()
    store.save_embeddings(conn, MODEL_NAME, rows)
    for uid, g in truth.items():
        store.add_feedback(conn, uid, g, "koreksi")
    return conn, truth


COUNTS = {"keuangan": 40, "kertas": 40, "layar": 40, "grafis": 40, "lainnya": 60}


def run_train(conn, root, prompts, **kw):
    return train.train_head(conn, root, text_emb_fn=fake_text, config_path=prompts, **kw)


def test_train_head_learns_separable_groups(tmp_path, prompts):
    conn, truth = make_collection(tmp_path, COUNTS)
    res = run_train(conn, tmp_path, prompts, min_per_group=20)
    assert res.version == "trained-v1"
    assert set(res.trained_groups) == set(COUNTS)
    assert res.fallback_groups == ["identitas"]
    head = get_head(res.version, tmp_path, text_emb_fn=fake_text)
    ids, mat = store.load_embeddings(conn, MODEL_NAME)
    held = [i for i, u in enumerate(ids) if train.is_holdout(u, 0.2, 42)]
    assert held
    preds = head.predict(mat[held], [PLAIN] * len(held))
    acc = np.mean([p[0] == truth[ids[i]] for p, i in zip(preds, held)])
    assert acc >= 0.9
    assert res.holdout_metrics["keuangan"]["recall"] >= 0.9


def test_groups_below_minimum_fall_back_to_zeroshot(tmp_path, prompts):
    conn, _ = make_collection(tmp_path, {**COUNTS, "identitas": 3})
    res = run_train(conn, tmp_path, prompts, min_per_group=20)
    assert "identitas" in res.fallback_groups
    assert "identitas" not in res.trained_groups


def test_zero_examples_does_not_crash_or_save(tmp_path, prompts):
    conn = open_db(tmp_path)
    res = run_train(conn, tmp_path, prompts)
    assert res.version == "" and res.trained_groups == []
    assert set(res.fallback_groups) == set(GROUPS)
    assert not (tmp_path / ".arsipbersih" / "ai" / "heads").exists()


def test_holdout_never_used_for_training(tmp_path, prompts, monkeypatch):
    conn, _ = make_collection(tmp_path, COUNTS)
    seen = []
    orig = train.fit_softmax

    def spy(x, y, k, **kw):
        seen.append(x.copy())
        return orig(x, y, k, **kw)

    monkeypatch.setattr(train, "fit_softmax", spy)
    run_train(conn, tmp_path, prompts, min_per_group=20)
    ids, mat = store.load_embeddings(conn, MODEL_NAME)
    held = {u for u in ids if train.is_holdout(u, 0.2, 42)}
    assert held and len(held) < len(ids)
    trained_rows = {tuple(r) for r in seen[0]}
    for i, u in enumerate(ids):
        assert (tuple(mat[i]) in trained_rows) == (u not in held)


def test_trained_head_loadable_by_name(tmp_path, prompts):
    conn, _ = make_collection(tmp_path, COUNTS)
    run_train(conn, tmp_path, prompts, min_per_group=20)
    r2 = run_train(conn, tmp_path, prompts, min_per_group=20)
    assert r2.version == "trained-v2"
    heads = tmp_path / ".arsipbersih" / "ai" / "heads"
    assert (heads / "trained-v2.npz").is_file()
    meta = json.loads((heads / "trained-v2.json").read_text(encoding="utf-8"))
    assert meta["base_model"] == MODEL_NAME and meta["n_examples"]["kertas"] > 0
    assert get_head("trained-v1", tmp_path, text_emb_fn=fake_text).name == "trained-v1"
    with pytest.raises(ValueError, match="trained-v9"):
        get_head("trained-v9", tmp_path)


def test_fallback_group_keeps_zeroshot_probability(tmp_path, prompts):
    conn, _ = make_collection(tmp_path, COUNTS)
    res = run_train(conn, tmp_path, prompts, min_per_group=20)
    head = get_head(res.version, tmp_path, text_emb_fn=fake_text)
    e = np.zeros((1, DIM), np.float32)
    e[0, GROUPS.index("identitas")] = 1.0  # strongly matches the identitas text prompt
    assert head.predict(e, [PLAIN])[0][0] == "identitas"


def test_ai_report_lists_targets(tmp_path, prompts):
    conn, _ = make_collection(tmp_path, COUNTS)
    res = run_train(conn, tmp_path, prompts, min_per_group=20)
    ids, mat = store.load_embeddings(conn, MODEL_NAME)
    head = get_head(res.version, tmp_path, text_emb_fn=fake_text)
    preds = head.predict(mat, [PLAIN] * len(ids))
    store.save_labels(conn, res.version, "v1", [(u, g, s, p) for u, (g, s, p) in zip(ids, preds)])
    text = train.ai_report(conn, tmp_path, res.version)
    assert "keuangan" in text and "screenshot" in text  # per group and mapped category
    assert "Target" in text and "tercapai" in text
    assert "daya tangkap" in text and "ketepatan" in text
    assert "0,95" in text or "95%" in text


def test_cli_commands(tmp_path, prompts, monkeypatch):
    from arsipbersih.cli import app

    conn, _ = make_collection(tmp_path, COUNTS)
    conn.close()
    monkeypatch.setattr(train, "text_embeddings", fake_text)
    monkeypatch.setattr(train, "DEFAULT_PROMPTS", prompts)
    r = CliRunner().invoke(app, ["train-head", str(tmp_path), "--min", "20"])
    assert r.exit_code == 0, r.output
    assert "classify --head trained-v1" in r.output
    r = CliRunner().invoke(app, ["ai-report", str(tmp_path), "--head", "trained-v1"])
    assert r.exit_code == 0, r.output
    assert "Target" in r.output


def test_ai_report_defaults_to_active_head(tmp_path, prompts):
    from dataclasses import replace

    from arsipbersih.config import load_settings, save_settings

    conn, _ = make_collection(tmp_path, COUNTS)
    res = run_train(conn, tmp_path, prompts, min_per_group=20)
    ids, _ = store.load_embeddings(conn, MODEL_NAME)
    store.save_labels(conn, "zeroshot", "v1", [(u, "lainnya", None, 0.9) for u in ids])

    text = train.ai_report(conn, tmp_path)  # active head is still zeroshot
    assert text.splitlines()[0] == "Laporan AI (head: zeroshot)"
    assert f"data uji {res.version}" not in text  # trained holdout metrics belong to that head only
    assert f"ai-report --head {res.version}" in text

    save_settings(conn, replace(load_settings(conn), ai_head="zeroshot-v1"))  # older alias
    assert train.ai_report(conn, tmp_path).splitlines()[0] == "Laporan AI (head: zeroshot)"

    save_settings(conn, replace(load_settings(conn), ai_head=res.version))
    text = train.ai_report(conn, tmp_path)
    assert text.splitlines()[0] == f"Laporan AI (head: {res.version})"
    assert f"data uji {res.version}" in text
