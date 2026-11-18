"""Video copies and clips from per-frame pHashes (spec 5.4).

Builder's choices (spec 9; calibrate on real data):
- Frames match when their pHash hamming distance is <= `max_dist` (grouping passes
  `video_frame_phash_max`).
- Alignment works on frame *times*: videos longer than 2 minutes are sampled every
  duration/120 s, so two videos rarely share a frame grid. Every close frame pair votes for
  an offset (b_time - a_time) in bins one coarse spacing wide; the busiest bin wins and the
  median of it and its neighbours is the offset. Coverage is counted on the *sparser* video:
  each of its frames inside the overlap is matched when a frame of the denser video within
  the denser spacing is close. A match needs >= MIN_FRACTION of those frames and
  >= min(MIN_FRAMES, those frames) matched frames.
- Duplicate vs clip (owner decision, spec 7): tol = max(EDGE_S, sparser spacing), because
  sampling cannot place the ends finer than one sparse spacing (a 600 s video is sampled
  every 5 s). The shorter video's unmatched head + tail and the longer video's extra length
  both <= tol -> duplicate. Shorter covered (<= tol unmatched) but the longer has > tol
  extra -> clip. Anything else -> none.
- shortcut: two long copies whose 120-frame grids drift apart (e.g. 600 s vs 601.5 s) while
  the content changes faster than the spacing may fall under MIN_FRACTION and stay ungrouped
  (a miss = no group, the safe direction). Upgrade by sampling denser (more frames per video)
  if real collections show missed long duplicates.
- Candidates: an inverted index on 16-bit bands of every frame pHash (4 keys per frame).
  Two videos are a candidate when they share >= MIN_VOTES distinct keys. A clip shares keys
  with its parent however short it is (a duration window would miss it), and the full
  alignment never runs on all pairs. Keys held by more than MAX_POSTING videos (black or
  blank frames) carry no signal and are skipped.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Literal

import numpy as np

from .videoframes import VideoFrameData
from .visual import phash_int

EDGE_S = 2.0
MIN_FRACTION = 0.7
MIN_FRAMES = 3
DEFAULT_MAX_DIST = 8
BANDS = 4
MIN_VOTES = 2
MAX_POSTING = 64

Relation = Literal["duplicate", "clip", "none"]


@dataclass
class Match:
    offset_s: float  # b_time = a_time + offset_s
    matched_fraction: float
    a_start: float  # first / last matched frame times, in each video's own timeline
    a_end: float
    b_start: float
    b_end: float
    edge_gap_s: float = 0.0  # unmatched sparse frames at the overlap's ends x sparse spacing


def _spacing(times: list[float]) -> float:
    if len(times) > 1:
        return (times[-1] - times[0]) / (len(times) - 1)
    return 2 * times[0] if times and times[0] > 0 else 1.0  # one frame sits mid-video


def align(a: VideoFrameData, b: VideoFrameData, max_dist: int) -> Match | None:
    if not a.times or not b.times:
        return None
    ha = np.array([phash_int(h) for h in a.phashes], dtype=np.uint64)
    hb = np.array([phash_int(h) for h in b.phashes], dtype=np.uint64)
    ta, tb = np.asarray(a.times), np.asarray(b.times)
    close = np.bitwise_count(ha[:, None] ^ hb[None, :]) <= max_dist
    ii, jj = np.nonzero(close)
    if len(ii) == 0:
        return None
    step = max(_spacing(a.times), _spacing(b.times))
    offs = tb[jj] - ta[ii]
    bins = np.round(offs / step).astype(int)
    vals, counts = np.unique(bins, return_counts=True)
    best = vals[counts.argmax()]
    offset = float(np.median(offs[np.abs(bins - best) <= 1]))

    # Coverage is judged on the sparser video: each of its frames inside the overlap needs a
    # close frame in the denser one within the denser spacing. Counting the denser frames instead
    # asks a frame up to half a coarse spacing away to show the same content.
    a_sparse = _spacing(a.times) >= _spacing(b.times)
    ts, td = (ta, tb) if a_sparse else (tb, ta)
    off = offset if a_sparse else -offset  # denser time = sparser time + off
    cl = close if a_sparse else close.T
    mapped = ts + off
    half_d = (_spacing(b.times) if a_sparse else _spacing(a.times)) / 2
    inside = (mapped >= td[0] - half_d) & (mapped <= td[-1] + half_d)
    near = np.abs(td[None, :] - mapped[:, None]) <= 2 * half_d
    ok = (cl & near).any(axis=1) & inside
    n_in, n_ok = int(inside.sum()), int(ok.sum())
    if n_in == 0 or n_ok < min(MIN_FRAMES, n_in) or n_ok / n_in < MIN_FRACTION:
        return None
    idx, inner = np.nonzero(ok)[0], np.nonzero(inside)[0]
    gap = (idx[0] - inner[0] + inner[-1] - idx[-1]) * max(_spacing(a.times), _spacing(b.times))
    first, last = float(ts[idx[0]]), float(ts[idx[-1]])
    if not a_sparse:  # report in a's and b's own timelines
        first, last = first - offset, last - offset
    return Match(offset, n_ok / n_in, first, last, first + offset, last + offset, float(gap))


def pair_relation(
    a: VideoFrameData, b: VideoFrameData, dur_a: float, dur_b: float,
    max_dist: int = DEFAULT_MAX_DIST,
) -> tuple[Relation, Match | None]:
    """Relation of two videos; the Match aligns the shorter (a on ties) inside the longer."""
    if dur_a > dur_b:
        a, b, dur_a, dur_b = b, a, dur_b, dur_a
    m = align(a, b, max_dist)
    if m is None:
        return "none", None
    sparse = max(_spacing(a.times), _spacing(b.times))
    tol = max(EDGE_S, sparse)  # sampling cannot resolve the ends finer than one sparse spacing
    # The shorter video's part outside the longer one (from the offset) plus unmatched ends.
    off = m.offset_s
    uncovered = max(0.0, -off) + max(0.0, off + dur_a - dur_b) + m.edge_gap_s
    if uncovered > tol:
        return "none", m
    extra = dur_b - (dur_a - uncovered)
    return ("duplicate" if extra <= tol else "clip"), m


def classify_pair(
    a: VideoFrameData, b: VideoFrameData, dur_a: float, dur_b: float,
    max_dist: int = DEFAULT_MAX_DIST,
) -> Relation:
    """GoPro chapters are excluded by the caller (it has the file names)."""
    return pair_relation(a, b, dur_a, dur_b, max_dist)[0]


def candidate_pairs(frames: dict[str, VideoFrameData]) -> list[tuple[str, str]]:
    """Sorted (x, y) id pairs, x < y, worth a full alignment."""
    keys_of: dict[str, set[int]] = {}
    postings: dict[int, list[str]] = defaultdict(list)
    for vid, data in frames.items():
        keys = {
            (band << 16) | ((h >> (16 * band)) & 0xFFFF)
            for h in map(phash_int, data.phashes) for band in range(BANDS)
        }
        keys_of[vid] = keys
        for k in keys:
            postings[k].append(vid)
    out = []
    for vid, keys in keys_of.items():
        votes = Counter(
            o for k in keys if len(postings[k]) <= MAX_POSTING for o in postings[k] if o > vid
        )
        out += [(vid, o) for o, n in votes.items() if n >= MIN_VOTES]
    return sorted(out)
