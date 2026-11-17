"""Heads turn stored image embeddings into a coarse group label.

The zero-shot head scores embeddings against CLIP text prompts (see prompts_zeroshot_v1.json),
then applies deterministic rule hints (camera EXIF, screen ratio, download-like name) to the
group probabilities. Factors are applied before the thresholds and the result is NOT
renormalized, so a hint can push a probability up to 1.0 or down without touching the others.
"""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from ..layout import STATE_DIR
from .model import text_embeddings

GROUPS = ("identitas", "keuangan", "kertas", "grafis", "layar", "lainnya")
GROUP_TO_CATEGORY = {
    "identitas": "dokumen-identitas",
    "keuangan": "dokumen-keuangan",
    "kertas": "dokumen-kertas",
    "grafis": "download-meme",
    "layar": "screenshot",
}
DEFAULT_PROMPTS = Path(__file__).with_name("prompts_zeroshot_v1.json")


@dataclass(frozen=True)
class UnitFacts:
    has_camera: bool
    is_png: bool
    screen_ratio: bool
    download_like: bool


class Head(Protocol):
    name: str
    version: str

    def predict(self, emb: np.ndarray, facts: list[UnitFacts]) -> list[tuple[str, str | None, float]]:
        """Return (group, sub label or None, probability) per row of ``emb``."""
        ...


def _expand(prompt: str, cfg: dict) -> list[str]:
    if prompt.startswith(tuple(cfg["as_is_prefixes"])) or any(w in prompt for w in cfg["as_is_contains"]):
        return [prompt + "."]
    return [t.format(prompt) for t in cfg["templates"]]


class ZeroShotHead:
    name = "zeroshot"
    version = "v1"

    def __init__(self, text_emb_fn: Callable[[list[str]], np.ndarray] = text_embeddings,
                 config_path: Path | None = None):
        self._cfg = json.loads(Path(config_path or DEFAULT_PROMPTS).read_text(encoding="utf-8"))
        self._text_emb_fn = text_emb_fn
        self._matrix: np.ndarray | None = None
        self._sub_group: list[str] = []
        self._sub_label: list[str] = []

    def _load(self) -> np.ndarray:
        if self._matrix is not None:
            return self._matrix
        flat: list[str] = []
        spans: list[tuple[int, int]] = []
        sub_group: list[str] = []
        sub_label: list[str] = []
        for group, items in self._cfg["groups"].items():
            for item in items:
                texts = _expand(item["prompt"], self._cfg)
                spans.append((len(flat), len(flat) + len(texts)))
                flat.extend(texts)
                sub_group.append(group)
                sub_label.append(item["sub"])
        emb = np.asarray(self._text_emb_fn(flat), dtype=np.float32)  # one call keeps the disk cache effective
        rows = []
        for a, b in spans:
            m = emb[a:b].mean(0)
            rows.append(m / np.linalg.norm(m))
        self._matrix = np.stack(rows).astype(np.float32)
        self._sub_group, self._sub_label = sub_group, sub_label
        return self._matrix

    def _factors(self, f: UnitFacts) -> dict[str, float]:
        rf = self._cfg["rule_factors"]
        out: dict[str, float] = {}

        def mul(table: dict[str, float]) -> None:
            for g, v in table.items():
                out[g] = out.get(g, 1.0) * v

        if f.has_camera:
            mul(rf.get("has_camera", {}))
        if f.screen_ratio and not f.has_camera:
            mul(rf.get("screen_ratio_no_camera", {}))
        if f.download_like:
            mul(rf.get("download_like", {}))
        if f.is_png:
            mul(rf.get("is_png", {}))
        return out

    def group_scores(self, emb: np.ndarray, facts: list[UnitFacts]) -> list[tuple[dict[str, float], dict[str, str]]]:
        """Per row: group probabilities after rule factors (before thresholds) and best sub label per group."""
        emb = np.asarray(emb, dtype=np.float32)
        if len(emb) == 0:
            return []
        if len(emb) != len(facts):
            raise ValueError("emb and facts must have the same length")
        matrix = self._load()
        logits = self._cfg["logit_scale"] * emb @ matrix.T
        p = np.exp(logits - logits.max(1, keepdims=True))
        p /= p.sum(1, keepdims=True)
        sub_group = np.array(self._sub_group)
        out: list[tuple[dict[str, float], dict[str, str]]] = []
        for i, f in enumerate(facts):
            gp = {g: float(p[i, sub_group == g].sum()) for g in GROUPS}
            for g, k in self._factors(f).items():
                gp[g] = min(1.0, gp[g] * k)
            subs = {}
            for g in GROUPS:
                idx = np.flatnonzero(sub_group == g)
                if len(idx):
                    subs[g] = self._sub_label[int(idx[np.argmax(p[i, idx])])]
            out.append((gp, subs))
        return out

    def decide(self, gp: dict[str, float], subs: dict[str, str]) -> tuple[str, str | None, float]:
        """Pick the winning group with the shared thresholds."""
        return decide(gp, subs, self._cfg["thresholds"])

    def predict(self, emb: np.ndarray, facts: list[UnitFacts]) -> list[tuple[str, str | None, float]]:
        return [self.decide(gp, subs) for gp, subs in self.group_scores(emb, facts)]


def decide(gp: dict[str, float], subs: dict[str, str], thr: dict) -> tuple[str, str | None, float]:
    """Winner by probability; a non-lainnya group below the thresholds falls back to lainnya."""
    winner = max(GROUPS, key=lambda g: gp.get(g, 0.0))
    prob = gp.get(winner, 0.0)
    if winner != "lainnya" and (prob < thr["group_min"] or (winner == "identitas" and prob < thr["identitas_min"])):
        winner, prob = "lainnya", gp.get("lainnya", 0.0)
    return winner, subs.get(winner), prob


@functools.cache
def group_thresholds(config_path: Path | None = None) -> dict:
    """The decision thresholds every head shares (from the zero-shot prompt file)."""
    cfg = json.loads(Path(config_path or DEFAULT_PROMPTS).read_text(encoding="utf-8"))
    return dict(cfg["thresholds"])


def threshold_for(grp: str, thr: dict | None = None) -> float:
    """The probability a label of ``grp`` had to reach (lainnya uses the plain group minimum)."""
    thr = thr or group_thresholds()
    return thr["identitas_min"] if grp == "identitas" else thr["group_min"]


HEAD_DIR = Path(STATE_DIR) / "ai" / "heads"
_TRAINED_RE = re.compile(r"^trained-v(\d+)$")


class TrainedHead:
    """Softmax weights for the trained groups, zero-shot for the rest (see ai/train.py).

    Final probability: fallback group = its zero-shot probability (after rule factors);
    trained group = trained softmax prob * (1 - sum of fallback zero-shot probs), then the
    same rule factors as zero-shot (clipped at 1.0). Thresholds are shared with zero-shot.
    """

    def __init__(self, path: Path, text_emb_fn: Callable[[list[str]], np.ndarray] = text_embeddings,
                 config_path: Path | None = None):
        path = Path(path)
        data = np.load(path, allow_pickle=False)
        self.weights = data["weights"].astype(np.float32)
        self.bias = data["bias"].astype(np.float32)
        self.groups = [str(g) for g in data["groups"]]
        self.name = path.stem
        self.version = self.name.removeprefix("trained-")
        self._zs = ZeroShotHead(text_emb_fn=text_emb_fn, config_path=config_path)

    def predict(self, emb: np.ndarray, facts: list[UnitFacts]) -> list[tuple[str, str | None, float]]:
        emb = np.asarray(emb, dtype=np.float32)
        base = self._zs.group_scores(emb, facts)
        if not base:
            return []
        z = emb @ self.weights.T + self.bias
        z = np.exp(z - z.max(1, keepdims=True))
        z /= z.sum(1, keepdims=True)
        thr = self._zs._cfg["thresholds"]
        results = []
        for i, (gp, subs) in enumerate(base):
            fallback = sum(v for g, v in gp.items() if g not in self.groups)
            scale = max(0.0, 1.0 - fallback)
            final = {g: v for g, v in gp.items() if g not in self.groups}
            factors = self._zs._factors(facts[i])
            for j, g in enumerate(self.groups):
                final[g] = min(1.0, float(z[i, j]) * scale * factors.get(g, 1.0))
            results.append(decide(final, subs, thr))
        return results


def trained_head_dir(root: Path) -> Path:
    return Path(root) / HEAD_DIR


# Accepted head names that store their labels under another name.
HEAD_ALIASES = {"zeroshot-v1": "zeroshot"}


def canonical_head_name(name: str | None) -> str:
    """The name a head's labels are stored under (``zeroshot-v1`` -> ``zeroshot``)."""
    name = name or "zeroshot"
    return HEAD_ALIASES.get(name, name)


def get_head(name: str, root: Path | None = None,
             text_emb_fn: Callable[[list[str]], np.ndarray] | None = None) -> Head:
    """Resolve a head by setting name: ``zeroshot`` or ``trained-vN`` (loaded from ``root``)."""
    name = canonical_head_name(name)
    if name == "zeroshot":
        return ZeroShotHead(**({"text_emb_fn": text_emb_fn} if text_emb_fn else {}))
    if _TRAINED_RE.match(name):
        if root is None:
            raise ValueError(f"Head {name!r} butuh folder koleksi.")
        path = trained_head_dir(root) / f"{name}.npz"
        if not path.is_file():
            raise ValueError(f"Head {name!r} tidak ditemukan di {path.parent}.")
        return TrainedHead(path, **({"text_emb_fn": text_emb_fn} if text_emb_fn else {}))
    raise ValueError(f"Head tidak dikenal: {name!r} (pilihan: zeroshot, trained-vN)")
