import numpy as np
import pytest
from PIL import Image

from arsipbersih.ai import model


def test_crop_rgb_shape_and_center():
    img = Image.new("RGB", (400, 300), (10, 20, 30))
    img.paste((250, 5, 5), (180, 130, 220, 170))  # solid center block: bicubic keeps it exact
    out = model.crop_rgb(img)
    assert out.shape == (256, 256, 3) and out.dtype == np.uint8
    assert tuple(out[128, 128]) == (250, 5, 5)


@pytest.mark.parametrize("size", [(20000, 10), (10, 20000)])
def test_crop_rgb_extreme_aspect_ratio(size, monkeypatch):
    img = Image.new("RGB", size, (1, 2, 3))
    real_resize = Image.Image.resize

    def small_resize(self, wh, *a, **k):
        assert wh == (256, 256) and self.size == (10, 10)  # cropped before any resize
        return real_resize(self, wh, *a, **k)

    monkeypatch.setattr(Image.Image, "resize", small_resize)
    out = model.crop_rgb(img)
    assert out.shape == (256, 256, 3) and out.dtype == np.uint8
    assert tuple(out[128, 128]) == (1, 2, 3)


def test_model_status_not_ready_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("ARSIPBERSIH_MODELS", str(tmp_path))
    st = model.model_status()
    assert st.ready is False and "setup-models" in st.detail
    assert st.path == tmp_path / model.MODEL_NAME


def test_tests_do_not_see_an_installed_model(tmp_path, monkeypatch):
    # A ready model at the default location (tools/models under the repo root) ...
    fake_repo = tmp_path / "repo"
    ready = fake_repo / "tools" / "models" / model.MODEL_NAME
    ready.mkdir(parents=True)
    for name in (*model.MODEL_FILES, model.MANIFEST):
        (ready / name).write_bytes(b"x")
    monkeypatch.setattr(model, "_REPO_ROOT", fake_repo)
    # ... stays invisible to an unmarked test (conftest's autouse fixture) ...
    assert model.model_status().ready is False
    # ... while the default location itself would be ready.
    monkeypatch.delenv("ARSIPBERSIH_MODELS")
    assert model.model_status().ready is True and model.model_status().path == ready


def test_text_embeddings_uses_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("ARSIPBERSIH_MODELS", str(tmp_path))
    prompts = ["a cat", "a dog"]

    def fake(p, path):
        return np.array([[3.0, 4.0], [0.0, 2.0]], dtype=np.float32)

    monkeypatch.setattr(model, "_encode_text", fake)
    model.models_dir().mkdir(parents=True)
    first = model.text_embeddings(prompts)
    assert np.allclose(np.linalg.norm(first, axis=1), 1.0)

    def boom(p, path):
        raise AssertionError("text model must not load when the cache exists")

    monkeypatch.setattr(model, "_encode_text", boom)
    assert np.allclose(model.text_embeddings(prompts), first)


def test_setup_models_network_failure_is_clear(tmp_path, monkeypatch):
    def fail(*a, **k):
        raise model.urllib.error.URLError("offline")

    monkeypatch.setattr(model.urllib.request, "urlopen", fail)
    with pytest.raises(model.ModelSetupError, match="Gagal mengunduh"):
        model.setup_models(tmp_path / "m")


@pytest.mark.model
def test_real_model_embeds():
    st = model.model_status()
    if not st.ready:
        pytest.skip("model belum disiapkan")
    enc = model.VisionEncoder(st.path)
    batch = np.stack([model.crop_rgb(Image.new("RGB", (320, 240), c)) for c in ((200, 30, 30), (20, 20, 200))])
    out = enc.embed(batch)
    assert out.shape[0] == 2 and out.ndim == 2
    assert np.allclose(np.linalg.norm(out, axis=1), 1.0, atol=1e-3)
