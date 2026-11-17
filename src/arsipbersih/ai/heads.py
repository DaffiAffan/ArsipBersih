"""Heads turn stored image embeddings into a coarse group label.

The zero-shot head scores embeddings against CLIP text prompts (see prompts_zeroshot_v1.json),
then applies deterministic rule hints (camera EXIF, screen ratio, download-like name) to the
group probabilities. Factors are applied before the thresholds and the result is NOT
renormalized, so a hint can push a probability up to 1.0 or down without touching the others.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

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

    def predict(self, emb: np.ndarray, facts: list[UnitFacts]) -> list[tuple[str, str | None, float]]:
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
        thr = self._cfg["thresholds"]
        results: list[tuple[str, str | None, float]] = []
        for i, f in enumerate(facts):
            gp = {g: float(p[i, sub_group == g].sum()) for g in GROUPS}
            for g, k in self._factors(f).items():
                gp[g] = min(1.0, gp[g] * k)
            winner = max(GROUPS, key=lambda g: gp[g])
            prob = gp[winner]
            if winner != "lainnya" and (prob < thr["group_min"] or (winner == "identitas" and prob < thr["identitas_min"])):
                winner, prob = "lainnya", gp["lainnya"]
            idx = np.flatnonzero(sub_group == winner)
            sub = self._sub_label[int(idx[np.argmax(p[i, idx])])]
            results.append((winner, sub, prob))
        return results


def get_head(name: str, root: Path | None = None) -> Head:
    """Resolve a head by setting name. Task 8 adds ``trained-vN`` (loaded from ``root``)."""
    if name in ("zeroshot", "zeroshot-v1"):
        return ZeroShotHead()
    raise ValueError(f"Head tidak dikenal: {name!r} (pilihan: zeroshot)")
