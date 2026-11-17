"""Plain-language reason why a keeper was suggested.

Compares the keeper with the best other member using the engine's own key functions
(called, never copied), so the sentence always names the first component that differs.
"""
from __future__ import annotations

from ..grouping import UnitView, _completeness, _exact_key, _version_key

_FALLBACK = "Saran otomatis"
_COMPLETE = "Salinan paling lengkap (ikut file pasangannya)"
# Labels for the components of _exact_key, in order (the trailing path/id parts are ties).
_EXACT_LABELS = (
    "Bukan kiriman chat/download",
    "Ada info kamera",
    "Nama file asli kamera",
    "Path paling pendek",
)


def _first_diff(a: tuple, b: tuple) -> int | None:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return None


def _exact_reason(keeper: UnitView, other: UnitView) -> str:
    if _completeness(keeper) != _completeness(other):
        return _COMPLETE
    if (keeper.location == "archive") != (other.location == "archive"):
        return "Salinan yang sudah ada di Arsip"
    i = _first_diff(_exact_key(keeper), _exact_key(other))
    # index 3 is len(orig_rel_path); 4+ (path text, unit id) are arbitrary tie-breaks
    if i is None or i > 3:
        return _FALLBACK
    return _EXACT_LABELS[i]


def _version_reason(keeper: UnitView, other: UnitView) -> str:
    # _version_key = (*completeness(2), -pixels, -size, no_camera, *exact_key)
    i = _first_diff(_version_key(keeper), _version_key(other))
    if i is None:
        return _FALLBACK
    if i < len(_completeness(keeper)):
        return _COMPLETE
    return {
        len(_completeness(keeper)): "Resolusi paling besar",
        len(_completeness(keeper)) + 1: "File paling besar",
        len(_completeness(keeper)) + 2: "Ada info kamera",
    }.get(i, _FALLBACK)


def suggestion_reason(kind: str, views: list[UnitView], keeper_id: str) -> str:
    if kind == "mirip-beruntun":
        return "Foto paling tajam di rangkaian ini"
    keeper = next((v for v in views if v.unit_id == keeper_id), None)
    others = [v for v in views if v.unit_id != keeper_id]
    if keeper is None or not others:
        return _FALLBACK
    if kind == "duplikat-persis":
        best = min(others, key=lambda v: (_completeness(v), v.location != "archive", _exact_key(v)))
        return _exact_reason(keeper, best)
    if kind == "duplikat-versi":
        best = min(others, key=_version_key)
        return _version_reason(keeper, best)
    return _FALLBACK
