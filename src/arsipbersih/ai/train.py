"""Retrain a small head on owner feedback and report accuracy against the spec targets."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from .heads import (
    DEFAULT_PROMPTS,
    GROUP_TO_CATEGORY,
    GROUPS,
    TrainedHead,
    UnitFacts,
    ZeroShotHead,
    canonical_head_name,
    trained_head_dir,
)
from .model import MODEL_NAME, text_embeddings
from .store import effective_feedback, latest_labels, load_embeddings

NEUTRAL = UnitFacts(has_camera=False, is_png=False, screen_ratio=False, download_like=False)

# Spec section 6. "lainnya" = ordinary photos that must stay put; we read "ketepatan >= 98%"
# for them as: at least 98% of ordinary photos are left alone (recall of lainnya).
TARGETS: dict[str, dict[str, float]] = {
    "lainnya": {"recall": 0.98},
    "identitas": {"recall": 0.95},
    "keuangan": {"precision": 0.85, "recall": 0.85},
    "kertas": {"precision": 0.85, "recall": 0.85},
    "layar": {"precision": 0.85, "recall": 0.85},
    "grafis": {"precision": 0.75, "recall": 0.75},
}
METRIC_ID = {"precision": "ketepatan", "recall": "daya tangkap"}
BUCKETS = ((0.0, 0.5), (0.5, 0.7), (0.7, 0.9), (0.9, 0.95), (0.95, 1.0001))


@dataclass
class TrainResult:
    version: str  # "" when nothing was saved
    trained_groups: list[str]
    fallback_groups: list[str]
    holdout_metrics: dict[str, dict[str, float | None]] = field(default_factory=dict)
    baseline_metrics: dict[str, dict[str, float | None]] = field(default_factory=dict)
    n_examples: dict[str, int] = field(default_factory=dict)
    n_holdout: int = 0


def is_holdout(unit_id: str, fraction: float, seed: int) -> bool:
    """Stable per unit: depends only on the id and the seed."""
    h = hashlib.sha256(f"{unit_id}:{seed}".encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64 < fraction


def fit_softmax(x: np.ndarray, y: np.ndarray, k: int, *, l2: float = 1e-4, lr: float = 10.0,
                iters: int = 600) -> tuple[np.ndarray, np.ndarray]:
    """Full-batch gradient descent for multinomial logistic regression with L2 on the weights."""
    n, d = x.shape
    w = np.zeros((k, d), np.float32)
    b = np.zeros(k, np.float32)
    onehot = np.eye(k, dtype=np.float32)[y]
    for _ in range(iters):
        z = x @ w.T + b
        z = np.exp(z - z.max(1, keepdims=True))
        p = z / z.sum(1, keepdims=True)
        g = (p - onehot) / n
        w -= lr * (g.T @ x + l2 * w)
        b -= lr * g.sum(0)
    return w, b


def per_group_metrics(pred: list[str], truth: list[str]) -> dict[str, dict[str, float | None]]:
    out: dict[str, dict[str, float | None]] = {}
    for g in GROUPS:
        tp = sum(1 for p, t in zip(pred, truth) if p == g and t == g)
        npred = sum(1 for p in pred if p == g)
        ntrue = sum(1 for t in truth if t == g)
        out[g] = {
            "precision": tp / npred if npred else None,
            "recall": tp / ntrue if ntrue else None,
            "n": ntrue,
        }
    return out


def _versions(heads: Path, pattern: str) -> list[int]:
    return [int(m.group(1)) for p in heads.glob(pattern) if (m := re.fullmatch(r"trained-v(\d+)", p.stem))]


def _video_units(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute(
        "select u.id from units u join files f on f.id = u.primary_file_id where f.kind = 'video'")}


def train_head(conn: sqlite3.Connection, root: Path, *, min_per_group: int = 30, holdout: float = 0.2,
               seed: int = 42, text_emb_fn: Callable[[list[str]], np.ndarray] | None = None,
               config_path: Path | None = None) -> TrainResult:
    text_emb_fn = text_emb_fn or text_embeddings
    config_path = config_path or DEFAULT_PROMPTS
    ids, mat = load_embeddings(conn, MODEL_NAME)
    feedback = effective_feedback(conn)
    videos = _video_units(conn)  # video embeddings are frame means: they do not train the photo head
    rows = [(i, u) for i, u in enumerate(ids) if u in feedback and u not in videos]
    labels = {u: feedback[u][0] for _, u in rows}
    train_rows = [i for i, u in rows if not is_holdout(u, holdout, seed)]
    test_rows = [i for i, u in rows if is_holdout(u, holdout, seed)]
    counts = {g: sum(1 for i in train_rows if labels[ids[i]] == g) for g in GROUPS}
    trained = [g for g in GROUPS if counts[g] >= min_per_group]
    fallback = [g for g in GROUPS if g not in trained]
    if len(trained) < 2:
        return TrainResult("", [], list(GROUPS), n_examples=counts, n_holdout=len(test_rows))

    keep = [i for i in train_rows if labels[ids[i]] in trained]
    x = mat[keep]
    y = np.array([trained.index(labels[ids[i]]) for i in keep])
    w, b = fit_softmax(x, y, len(trained))

    heads = trained_head_dir(root)
    heads.mkdir(parents=True, exist_ok=True)
    version = f"trained-v{max(_versions(heads, 'trained-v*.npz'), default=0) + 1}"
    npz = heads / f"{version}.npz"
    np.savez(npz, weights=w, bias=b, groups=np.array(trained))

    holdout_metrics: dict = {}
    baseline_metrics: dict = {}
    if test_rows:
        truth = [labels[ids[i]] for i in test_rows]
        facts = [NEUTRAL] * len(test_rows)
        new = TrainedHead(npz, text_emb_fn=text_emb_fn, config_path=config_path)
        base = ZeroShotHead(text_emb_fn=text_emb_fn, config_path=config_path)
        holdout_metrics = per_group_metrics([p[0] for p in new.predict(mat[test_rows], facts)], truth)
        baseline_metrics = per_group_metrics([p[0] for p in base.predict(mat[test_rows], facts)], truth)

    meta = {
        "version": version,
        "base_model": MODEL_NAME,
        "created": datetime.now().isoformat(timespec="seconds"),
        "groups": trained,
        "fallback_groups": fallback,
        "n_examples": counts,
        "n_holdout": len(test_rows),
        "holdout_fraction": holdout,
        "seed": seed,
        "holdout_metrics": holdout_metrics,
        "baseline_metrics": baseline_metrics,
    }
    (heads / f"{version}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return TrainResult(version, trained, fallback, holdout_metrics, baseline_metrics, counts, len(test_rows))


def latest_trained_version(root: Path) -> str | None:
    nums = _versions(trained_head_dir(root), "trained-v*.json")
    return f"trained-v{max(nums)}" if nums else None


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.0f}%"


def format_metrics_table(metrics: dict, baseline: dict | None = None) -> list[str]:
    head = f"{'kelompok':<10} {'n':>4}  {'ketepatan':>9} {'daya tangkap':>12}"
    lines = [head + ("   (zero-shot: ketepatan / daya tangkap)" if baseline else "")]
    for g in GROUPS:
        m = metrics.get(g)
        if not m:
            continue
        line = f"{g:<10} {m['n']:>4}  {_pct(m['precision']):>9} {_pct(m['recall']):>12}"
        if baseline and g in baseline:
            line += f"   ({_pct(baseline[g]['precision'])} / {_pct(baseline[g]['recall'])})"
        lines.append(line)
    return lines


def _target_lines(metrics: dict) -> list[str]:
    lines = ["Target spec (tercapai / belum):"]
    for g, reqs in TARGETS.items():
        for metric, need in reqs.items():
            m = metrics.get(g) or {}
            val = m.get(metric)
            name = f"{g} {METRIC_ID[metric]} >= {need * 100:.0f}%"
            if val is None or not m.get("n"):
                lines.append(f"  [?] {name}: belum ada contoh uji")
            elif val >= need:
                lines.append(f"  [tercapai] {name}: {_pct(val)}")
            else:
                lines.append(f"  [belum tercapai] {name}: {_pct(val)}")
    return lines


def ai_report(conn: sqlite3.Connection, root: Path, head: str | None = None) -> str:
    """Report on ``head`` (default: the active head from the settings)."""
    from ..config import load_settings

    latest = latest_trained_version(root)
    head = canonical_head_name(head or load_settings(conn).ai_head)
    labels = latest_labels(conn, head)
    out = [f"Laporan AI (head: {head})"]
    if not labels:
        out.append("Belum ada label untuk head ini. Jalankan classify dulu.")
    else:
        out.append(f"Jumlah per kelompok ({len(labels)} unit):")
        for g in GROUPS:
            n = sum(1 for v in labels.values() if v[0] == g)
            out.append(f"  {g:<10} {n:>6}   kategori: {GROUP_TO_CATEGORY.get(g, '(tanpa flag)')}")
        out.append("Sebaran probabilitas:")
        for lo, hi in BUCKETS:
            n = sum(1 for v in labels.values() if lo <= v[2] < hi)
            out.append(f"  {lo:.2f} - {min(hi, 1.0):.2f}: {n}")

    # Holdout metrics belong to one trained head: never report them under another head.
    version = head if head.startswith("trained-v") else None
    meta_path = trained_head_dir(root) / f"{version}.json" if version else None
    if meta_path is not None and meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        metrics = meta.get("holdout_metrics") or {}
        out.append(f"Ketepatan dan daya tangkap pada data uji {version} "
                   f"({meta.get('n_holdout', 0)} unit, tidak dipakai belajar):")
        out.extend("  " + ln for ln in format_metrics_table(metrics, meta.get("baseline_metrics")))
    else:
        feedback = effective_feedback(conn)
        common = [u for u in labels if u in feedback]
        metrics = {}
        if not common:
            out.append("Belum ada koreksi/konfirmasi pemilik untuk mengukur ketepatan.")
        else:
            metrics = per_group_metrics([labels[u][0] for u in common], [feedback[u][0] for u in common])
            out.append(f"Ketepatan dan daya tangkap terhadap {len(common)} koreksi/konfirmasi pemilik (bukan data uji):")
            out.extend("  " + ln for ln in format_metrics_table(metrics))
    videos = _video_units(conn)
    n_video = sum(1 for u in effective_feedback(conn) if u in videos)
    out.append(f"Contoh koreksi video: {n_video} (belum dipakai melatih)")
    out.extend(_target_lines(metrics))
    if latest and latest != head:
        out.append(f"Ada head terlatih {latest}; lihat hasilnya dengan: arsipbersih ai-report --head {latest}")
    return "\n".join(out)
