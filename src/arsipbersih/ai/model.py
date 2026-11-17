"""MobileCLIP2-S0 setup (one-time download) and ONNX runtime wrappers.

The model files live outside git under <repo>/tools/models/mobileclip2-s0 (override with
ARSIPBERSIH_MODELS). Only ``setup_models`` touches the network; everything else is offline.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from ..layout import MODELS_DIR

MODEL_NAME = "mobileclip2-s0"
INPUT_PX = 256
HF_REPO = "plhery/mobileclip2-onnx"
CONTEXT_LEN = 77

# local file name -> path inside the Hugging Face repo
MODEL_FILES = {
    "vision_model.onnx": "onnx/s0/vision_model.onnx",
    "text_model.onnx": "onnx/s0/text_model.onnx",
    "tokenizer.json": "tokenizer.json",
}
MANIFEST = "manifest.json"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_CHUNK = 1 << 20


class ModelSetupError(RuntimeError):
    """Downloading or writing the model files failed."""


@dataclass(frozen=True)
class ModelStatus:
    ready: bool
    path: Path
    detail: str


def models_dir() -> Path:
    override = os.environ.get("ARSIPBERSIH_MODELS")
    base = Path(override) if override else _REPO_ROOT / MODELS_DIR
    return base / MODEL_NAME


def model_status() -> ModelStatus:
    path = models_dir()
    missing = [n for n in (*MODEL_FILES, MANIFEST) if not (path / n).is_file()]
    if missing:
        return ModelStatus(False, path, "belum disiapkan, jalankan `arsipbersih setup-models`")
    return ModelStatus(True, path, "siap")


def _download(url: str, target: Path, progress: Callable[[str, int, int], None] | None) -> tuple[str, int]:
    part = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    done = 0
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, open(part, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            while chunk := resp.read(_CHUNK):
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress:
                    progress(target.name, done, total)
        part.replace(target)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        part.unlink(missing_ok=True)
        raise ModelSetupError(f"Gagal mengunduh {url}: {e}") from e
    return digest.hexdigest(), done


def setup_models(
    dest: Path | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> Path:
    """Download vision, text and tokenizer files and write manifest.json. Returns the folder."""
    dest = Path(dest) if dest else models_dir()
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ModelSetupError(f"Tidak bisa membuat folder {dest}: {e}") from e
    files: dict[str, dict] = {}
    for name, remote in MODEL_FILES.items():
        url = f"https://huggingface.co/{HF_REPO}/resolve/main/{remote}"
        sha, size = _download(url, dest / name, progress)
        files[name] = {"sha256": sha, "bytes": size}
    manifest = {"name": MODEL_NAME, "repo": HF_REPO, "files": files}
    (dest / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return dest


def crop_rgb(img: Image.Image) -> np.ndarray:
    """Resize the short side to 256 (BICUBIC) and center-crop: uint8 (256, 256, 3)."""
    img = img.convert("RGB")
    w, h = img.size
    s = INPUT_PX / min(w, h)
    img = img.resize((max(INPUT_PX, round(w * s)), max(INPUT_PX, round(h * s))), Image.BICUBIC)
    w, h = img.size
    left, top = (w - INPUT_PX) // 2, (h - INPUT_PX) // 2
    return np.asarray(img.crop((left, top, left + INPUT_PX, top + INPUT_PX)), dtype=np.uint8)


def _normalize(x: np.ndarray) -> np.ndarray:
    return (x / np.linalg.norm(x, axis=1, keepdims=True)).astype(np.float32)


def _session(path: Path, threads: int):
    import onnxruntime as ort

    so = ort.SessionOptions()
    so.intra_op_num_threads = threads
    so.inter_op_num_threads = 1
    return ort.InferenceSession(str(path), so, providers=["CPUExecutionProvider"])


class VisionEncoder:
    """Image embeddings: uint8 (N,256,256,3) batch in, L2-normalized float32 (N,D) out."""

    def __init__(self, path: Path, threads: int = 4) -> None:
        self._sess = _session(Path(path) / "vision_model.onnx", threads)
        self._input = self._sess.get_inputs()[0].name

    def embed(self, batch: np.ndarray) -> np.ndarray:
        x = batch.astype(np.float32).transpose(0, 3, 1, 2) / 255.0  # mean 0 / std 1
        out = self._sess.run(None, {self._input: np.ascontiguousarray(x)})[0]
        return _normalize(out)


def _encode_text(prompts: list[str], path: Path) -> np.ndarray:
    """Raw (unnormalized) text-encoder outputs; loads the text model."""
    from tokenizers import Tokenizer

    tok = Tokenizer.from_file(str(path / "tokenizer.json"))
    sess = _session(path / "text_model.onnx", 4)
    inp = sess.get_inputs()[0]
    ids = np.zeros((len(prompts), CONTEXT_LEN), dtype=np.int64)
    for i, p in enumerate(prompts):
        enc = tok.encode(p).ids[:CONTEXT_LEN]
        ids[i, : len(enc)] = enc
    if "int32" in inp.type:
        ids = ids.astype(np.int32)
    return sess.run(None, {inp.name: ids})[0]


def text_embeddings(prompts: list[str]) -> np.ndarray:
    """L2-normalized float32 (P,D); cached per prompt list so the text model loads only once."""
    path = models_dir()
    key = hashlib.sha1("\n".join(prompts).encode("utf-8")).hexdigest()
    cache = path / f"text-cache-{key}.npy"
    if cache.is_file():
        return np.load(cache)
    emb = _normalize(_encode_text(prompts, path))
    tmp = path / f"text-cache-{key}.tmp.npy"
    np.save(tmp, emb)
    tmp.replace(cache)
    return emb
