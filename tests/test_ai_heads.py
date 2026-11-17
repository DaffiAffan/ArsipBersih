import json
import math

import numpy as np
import pytest

from arsipbersih.ai.heads import DEFAULT_PROMPTS, GROUPS, UnitFacts, ZeroShotHead, get_head

PLAIN = UnitFacts(has_camera=False, is_png=False, screen_ratio=False, download_like=False)
CAM = UnitFacts(has_camera=True, is_png=False, screen_ratio=False, download_like=False)


@pytest.fixture
def head(tmp_path):
    """One prompt per group, one orthogonal text vector per group."""
    cfg = json.loads(DEFAULT_PROMPTS.read_text(encoding="utf-8"))
    cfg["groups"] = {g: [{"prompt": f"item {g}", "sub": f"sub-{g}"}] for g in GROUPS}
    path = tmp_path / "prompts.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")

    def fake(texts):
        out = np.zeros((len(texts), len(GROUPS)), np.float32)
        for i, t in enumerate(texts):
            out[i, [j for j, g in enumerate(GROUPS) if f"item {g}" in t][0]] = 1.0
        return out

    return ZeroShotHead(text_emb_fn=fake, config_path=path)


def emb_for(probs: dict[str, float]) -> np.ndarray:
    """Image embedding whose zero-shot group probabilities equal ``probs`` (must sum to 1)."""
    return np.array([[math.log(probs.get(g, 1e-9)) / 100 for g in GROUPS]], np.float32)


def test_zeroshot_picks_group_of_nearest_prompt(head):
    for g in GROUPS:
        e = np.zeros((1, len(GROUPS)), np.float32)
        e[0, GROUPS.index(g)] = 1.0
        group, sub, prob = head.predict(e, [PLAIN])[0]
        assert (group, sub) == (g, f"sub-{g}")
        assert prob > 0.99


def test_below_threshold_falls_back_to_lainnya(head):
    group, sub, prob = head.predict(emb_for({"keuangan": 0.6, "lainnya": 0.4}), [PLAIN])[0]
    assert group == "lainnya" and sub == "sub-lainnya" and prob == pytest.approx(0.4, abs=1e-3)


def test_identitas_requires_095(head):
    assert head.predict(emb_for({"identitas": 0.9, "lainnya": 0.1}), [PLAIN])[0][0] == "lainnya"
    assert head.predict(emb_for({"identitas": 0.96, "lainnya": 0.04}), [PLAIN])[0][0] == "identitas"


def test_camera_photo_damps_grafis(head):
    e = emb_for({"grafis": 0.75, "lainnya": 0.25})
    assert head.predict(e, [PLAIN])[0][0] == "grafis"
    assert head.predict(e, [CAM])[0][0] == "lainnya"


def test_screen_ratio_boosts_layar_and_download_boosts_grafis(head):
    e = emb_for({"layar": 0.6, "lainnya": 0.4})
    assert head.predict(e, [PLAIN])[0][0] == "lainnya"
    boosted = UnitFacts(has_camera=False, is_png=False, screen_ratio=True, download_like=False)
    assert head.predict(e, [boosted])[0][0] == "layar"
    e2 = emb_for({"grafis": 0.6, "lainnya": 0.4})
    dl = UnitFacts(has_camera=False, is_png=False, screen_ratio=False, download_like=True)
    group, sub, prob = head.predict(e2, [dl])[0]
    assert (group, sub) == ("grafis", "sub-grafis") and prob == pytest.approx(0.78, abs=1e-3)


def test_empty_batch_and_text_called_once():
    calls = []

    def fake(texts):
        calls.append(len(texts))
        return np.eye(len(texts), 8, dtype=np.float32) + 0.01

    h = ZeroShotHead(text_emb_fn=fake)
    assert h.predict(np.zeros((0, 8), np.float32), []) == []
    h.predict(np.ones((2, 8), np.float32), [PLAIN, PLAIN])
    h.predict(np.ones((1, 8), np.float32), [PLAIN])
    assert len(calls) == 1


def test_prompt_file_has_all_groups():
    cfg = json.loads(DEFAULT_PROMPTS.read_text(encoding="utf-8"))
    assert set(cfg["groups"]) == set(GROUPS)
    assert all(cfg["groups"][g] for g in GROUPS)
    subs = {i["sub"] for items in cfg["groups"].values() for i in items}
    assert {"KTP", "struk", "bukti transfer", "rapor", "buku tabungan"} <= subs


def test_get_head():
    assert get_head("zeroshot", None).name == "zeroshot"
    assert get_head("zeroshot-v1", None).version == "v1"
    with pytest.raises(ValueError, match="nope"):
        get_head("nope", None)
