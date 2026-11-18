"""Exact, version and burst duplicate groups with keeper suggestions (spec 8.1)."""

from __future__ import annotations

import json
import posixpath
import re
import sqlite3
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

from .config import Settings
from .dates import filename_has_time
from .formats import Kind
from .db import new_id
from .pairing import gopro_clip_id
from .pathrules import has_camera_name, is_chat, is_download, segments_of
from .videoframes import load_video_frames
from .videomatch import EDGE_S, candidate_pairs, pair_relation
from .visual import _load_rgb, hamming, phash_int

BLOCK = 4096
ASPECT_TOLERANCE = 0.02
DETAIL_PX = 768
DETAIL_BLOCK = 32
DETAIL_CACHE = 128  # decoded images kept at once (<= ~1 MB each)

_PIXEL_BURST_ID = re.compile(r"_BURST(\d{8,})", re.IGNORECASE)
_BURST_MARKER = re.compile(r"_BURST\d{1,7}|_COVER", re.IGNORECASE)


@dataclass
class UnitView:
    unit_id: str
    location: str
    orig_rel_path: str
    filename: str
    rel_path: str
    source_label: str | None
    make: str | None
    model: str | None
    width: int | None
    height: int | None
    size: int | None
    sharpness: float | None
    taken_at: str | None
    phash: str | None
    frame_phashes: list[str] | None
    duration_s: float | None
    kind: str | None
    sha256: str | None
    date_source: str | None = None
    file_count: int = 1  # present files of the unit (RAW+JPG, Live Photo, sidecars)
    unit_bytes: int = 0  # their total size


@dataclass
class GroupingResult:
    exact: int
    version: int
    burst: int


def load_unit_views(conn: sqlite3.Connection, unit_ids: list[str] | None = None) -> list[UnitView]:
    base = (
        "select u.id as unit_id, u.location, u.taken_at, u.date_source, f.rel_path, f.orig_rel_path, "
        "f.source_label, f.size, f.kind, f.sha256, "
        "m.make, m.model, m.width, m.height, m.duration_s, m.phash, m.frame_phashes, m.sharpness, "
        "c.n as file_count, c.bytes as unit_bytes "
        "from units u join files f on f.id = u.primary_file_id "
        "left join media_info m on m.file_id = f.id "
        "left join (select unit_id, count(*) as n, coalesce(sum(size), 0) as bytes from files "
        "  where missing = 0 and purged = 0"
        # Our date sidecars never make a unit look more complete (spec 11.6).
        "  and id not in (select file_id from xmp_writes) group by unit_id) c on c.unit_id = u.id "
        "where u.location != 'trash' and f.missing = 0 and f.purged = 0"
    )
    if unit_ids is None:
        rows = conn.execute(base + " order by u.id").fetchall()
    else:
        rows = []
        for i in range(0, len(unit_ids), 500):
            part = unit_ids[i : i + 500]
            marks = ",".join("?" * len(part))
            rows += conn.execute(f"{base} and u.id in ({marks})", part).fetchall()
        rows.sort(key=lambda r: r["unit_id"])
    views = []
    for r in rows:
        frames = json.loads(r["frame_phashes"]) if r["frame_phashes"] else None
        views.append(UnitView(
            unit_id=r["unit_id"], location=r["location"],
            orig_rel_path=r["orig_rel_path"] or r["rel_path"], rel_path=r["rel_path"],
            filename=posixpath.basename(r["rel_path"]),
            source_label=r["source_label"], make=r["make"], model=r["model"],
            width=r["width"], height=r["height"], size=r["size"], sharpness=r["sharpness"],
            taken_at=r["taken_at"], phash=r["phash"], frame_phashes=frames,
            duration_s=r["duration_s"], kind=r["kind"], sha256=r["sha256"],
            date_source=r["date_source"],
            file_count=r["file_count"] or 1, unit_bytes=r["unit_bytes"] or 0,
        ))
    return views


# ---------------------------------------------------------------- keepers


def _exact_key(u: UnitView) -> tuple:
    segs = segments_of(u.orig_rel_path)
    return (
        is_chat(segs, u.filename) or is_download(segs),
        not (u.make or u.model),
        not has_camera_name(u.filename),
        len(u.orig_rel_path),
        u.orig_rel_path,
        u.unit_id,
    )


def _completeness(u: UnitView) -> tuple[int, int]:
    """Most files, then most bytes: a RAW+JPG or Live Photo unit outranks a bare copy, so
    accepting the suggestion never trashes the RAW or the MOV."""
    return (-u.file_count, -u.unit_bytes)


def choose_keeper_exact(units: list[UnitView]) -> str:
    best = min(_completeness(u) for u in units)
    complete = [u for u in units if _completeness(u) == best]
    pool = [u for u in complete if u.location == "archive"] or complete
    return min(pool, key=_exact_key).unit_id


def _video_has_camera_data(u: UnitView) -> bool:
    # shortcut: no GPS / QuickTime make-model on UnitView yet; Task 4's has_camera_data replaces this.
    return (bool(u.make or u.model) or u.date_source == "quicktime"
            or has_camera_name(u.filename) or u.filename.upper().startswith("DJI_"))


def _version_key(u: UnitView) -> tuple:
    if u.kind == "video":
        # Same slots as the photo key (web/reasons.py reads them); bytes never make a video
        # "more complete" and size becomes bitrate. Duration is filtered first (choose_keeper_version).
        segs = segments_of(u.orig_rel_path)
        bitrate = (u.size or 0) / u.duration_s if u.duration_s else 0.0
        return (-u.file_count, 0, -((u.width or 0) * (u.height or 0)), -bitrate,
                not _video_has_camera_data(u), is_chat(segs, u.filename) or is_download(segs),
                not has_camera_name(u.filename), u.location != "archive",
                len(u.orig_rel_path), u.orig_rel_path, u.unit_id)
    return (*_completeness(u), -((u.width or 0) * (u.height or 0)), -(u.size or 0),
            not (u.make or u.model), *_exact_key(u))


def _clearly_shorter(d: float, longest: float) -> bool:
    return longest - d > EDGE_S and longest - d > 0.05 * longest


def choose_keeper_version(units: list[UnitView]) -> str:
    if all(u.kind == "video" for u in units):
        most = max(u.file_count for u in units)
        units = [u for u in units if u.file_count == most]
        longest = max(u.duration_s or 0.0 for u in units)
        units = [u for u in units if not _clearly_shorter(u.duration_s or 0.0, longest)]
    return min(units, key=_version_key).unit_id


# ---------------------------------------------------------------- helpers


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def groups(self) -> list[list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for x in list(self.parent):
            out[self.find(x)].append(x)
        return [g for g in out.values() if len(g) > 1]


def find_close_pairs(hashes: np.ndarray, max_dist: int) -> list[tuple[int, int]]:
    """All index pairs (i < j) with hamming(hashes[i], hashes[j]) <= max_dist."""
    h = np.ascontiguousarray(hashes, dtype=np.uint64)
    n = len(h)
    pairs: list[tuple[int, int]] = []
    side = min(BLOCK, max(n, 1))
    xor = np.empty((side, side), dtype=np.uint64)
    for i0 in range(0, n, BLOCK):
        rows = h[i0:i0 + BLOCK]
        for j0 in range(i0, n, BLOCK):
            cols = h[j0:j0 + BLOCK]
            buf = xor[:len(rows), :len(cols)]
            np.bitwise_xor(rows[:, None], cols[None, :], out=buf)
            close = np.bitwise_count(buf) <= max_dist
            ii, jj = np.nonzero(close)
            ii = ii + i0
            jj = jj + j0
            keep = jj > ii
            pairs.extend(zip(ii[keep].tolist(), jj[keep].tolist()))
    return pairs


def _complete_linkage(units: list[UnitView], edges: dict[tuple[str, str], int]) -> list[list[UnitView]]:
    """Split linked units into groups where every pair is itself a close match.

    Plain union-find chains A~B~C into one group even when A and C differ, so one keeper
    would push many distinct photos to the trash. Seeds are taken in keeper order; a unit
    joins a seed's group (closest first) only if it is close to every unit already in it.
    """
    def dist(a: str, b: str) -> int | None:
        return edges.get(_edge(a, b))

    uf = _UnionFind()
    for a, b in edges:
        uf.union(a, b)
    by_id = {u.unit_id: u for u in units}
    out: list[list[UnitView]] = []
    for comp in uf.groups():
        left = sorted((by_id[x] for x in comp), key=_version_key)
        while len(left) > 1:
            seed, rest = left[0], left[1:]
            near = sorted(
                (u for u in rest if dist(seed.unit_id, u.unit_id) is not None),
                key=lambda u: (dist(seed.unit_id, u.unit_id), _version_key(u)),
            )
            group = [seed]
            for u in near:
                if all(dist(g.unit_id, u.unit_id) is not None for g in group[1:]):
                    group.append(u)
            if len(group) > 1:
                out.append(group)
            taken = {u.unit_id for u in group}
            left = [u for u in rest if u.unit_id not in taken]
    return out


def _edge(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


class _DetailCheck:
    """Second look at pHash-close photos.

    pHash only sees the overall layout, so screenshots of one app (quiz pages, transfer
    receipts) with different text look like versions of each other. A real version
    (resized, recompressed) stays close in every 32-px block; different text does not.
    """

    def __init__(self, root: Path, max_diff: float):
        self.root = Path(root)
        self.max_diff = max_diff
        self._cache: OrderedDict[str, Image.Image | None] = OrderedDict()

    def _gray(self, u: UnitView) -> Image.Image | None:
        if u.unit_id in self._cache:
            self._cache.move_to_end(u.unit_id)
            return self._cache[u.unit_id]
        try:
            kind = Kind(u.kind) if u.kind else Kind.PHOTO
            img = _load_rgb(self.root / u.rel_path, kind, DETAIL_PX).convert("L")
            img.thumbnail((DETAIL_PX, DETAIL_PX), Image.Resampling.LANCZOS)
        except Exception:  # noqa: BLE001 - unreadable now: cannot confirm a version
            img = None
        self._cache[u.unit_id] = img
        if len(self._cache) > DETAIL_CACHE:
            self._cache.popitem(last=False)
        return img

    def distance(self, a: UnitView, b: UnitView) -> float | None:
        ia, ib = self._gray(a), self._gray(b)
        if ia is None or ib is None:
            return None
        bl = DETAIL_BLOCK
        w = max(bl, min(ia.width, ib.width) // bl * bl)
        h = max(bl, min(ia.height, ib.height) // bl * bl)
        arrs = []
        for im in (ia, ib):
            x = np.asarray(im.resize((w, h), Image.Resampling.LANCZOS), dtype=np.float32)
            arrs.append((x - x.mean()) / (x.std() + 1e-6))
        d = np.abs(arrs[0] - arrs[1]).reshape(h // bl, bl, w // bl, bl).mean(axis=(1, 3))
        return float(d.max())

    def close(self, a: UnitView, b: UnitView) -> bool:
        d = self.distance(a, b)
        return d is not None and d <= self.max_diff  # unverifiable: never suggest a discard


def _aspect(u: UnitView) -> float | None:
    if not u.width or not u.height:
        return None
    return max(u.width, u.height) / min(u.width, u.height)


# ---------------------------------------------------------------- groups


def _exact_groups(views: list[UnitView]) -> list[tuple[list[UnitView], str]]:
    by_sha: dict[str, list[UnitView]] = defaultdict(list)
    for u in views:
        if u.sha256:
            by_sha[u.sha256].append(u)
    return [(m, choose_keeper_exact(m)) for m in by_sha.values() if len(m) > 1]


def _photo_version_groups(
    photos: list[UnitView], max_dist: int, detail: _DetailCheck | None = None
) -> list[list[UnitView]]:
    cand = [u for u in photos if u.phash and _aspect(u) is not None]
    if len(cand) < 2:
        return []
    hashes = np.array([phash_int(u.phash) for u in cand], dtype=np.uint64)
    edges: dict[tuple[str, str], int] = {}
    for i, j in find_close_pairs(hashes, max_dist):
        a, b = cand[i], cand[j]
        if abs(_aspect(a) - _aspect(b)) > ASPECT_TOLERANCE:
            continue
        if a.sha256 and a.sha256 == b.sha256:
            continue
        # Controller ruling: separate shots, same camera -> burst, not version.
        if (a.source_label == b.source_label and (a.width, a.height) == (b.width, b.height)
                and a.taken_at and b.taken_at and a.taken_at != b.taken_at):
            continue
        if detail is not None and not detail.close(a, b):
            continue  # same layout, different content (e.g. two transfer receipts)
        edges[_edge(a.unit_id, b.unit_id)] = hamming(int(hashes[i]), int(hashes[j]))
    return _complete_linkage(cand, edges)


def _video_version_groups(
    conn: sqlite3.Connection, videos: list[UnitView], max_dist: int
) -> tuple[list[list[UnitView]], list[tuple[str, str, float]]]:
    """Duplicate groups and (clip unit, parent unit, offset s) relations (videomatch)."""
    by_id = {u.unit_id: u for u in videos if u.duration_s}
    primary = {r[0]: r[1] for r in conn.execute("select id, primary_file_id from units")}
    loaded = load_video_frames(conn, [primary[uid] for uid in by_id])
    frames = {uid: loaded[primary[uid]] for uid in by_id if primary[uid] in loaded}
    edges: dict[tuple[str, str], int] = {}
    clips: list[tuple[str, str, float]] = []
    for x, y in candidate_pairs(frames):
        a, b = by_id[x], by_id[y]
        if a.sha256 and a.sha256 == b.sha256:
            continue
        ca = gopro_clip_id(posixpath.splitext(a.filename)[0])
        cb = gopro_clip_id(posixpath.splitext(b.filename)[0])
        if ca is not None and ca == cb:
            continue
        rel, m = pair_relation(frames[x], frames[y], a.duration_s, b.duration_s, max_dist)
        if rel == "duplicate":
            edges[_edge(x, y)] = round((1 - m.matched_fraction) * 100)
        elif rel == "clip":
            short, long = (a, b) if a.duration_s <= b.duration_s else (b, a)
            clips.append((short.unit_id, long.unit_id, round(max(0.0, m.offset_s), 1)))
    return _complete_linkage([by_id[uid] for uid in frames], edges), clips


def _burst_id(filename: str) -> str | None:
    """Shared id of burst frames within a folder, or None when the name has no burst marker."""
    stem = posixpath.splitext(filename)[0]
    m = _PIXEL_BURST_ID.search(stem)
    if m:
        return m.group(1)
    if _BURST_MARKER.search(stem):
        return _BURST_MARKER.sub("", stem).casefold()
    return None


def _time_known(u: UnitView) -> bool:
    """A date-only filename (WhatsApp) stores 00:00:00; such units have no capture time."""
    return u.date_source != "filename" or filename_has_time(u.filename)


def _burst_groups(photos: list[UnitView], settings: Settings) -> list[list[UnitView]]:
    uf = _UnionFind()
    by_id = {u.unit_id: u for u in photos}

    timed = []
    for u in photos:
        if u.taken_at and u.phash and _time_known(u):
            try:
                timed.append((u.source_label or "", datetime.fromisoformat(u.taken_at), u))
            except ValueError:
                continue
    timed.sort(key=lambda t: (t[0], t[1]))
    for (la, ta, a), (lb, tb, b) in zip(timed, timed[1:]):
        if la != lb:
            continue
        if (tb - ta).total_seconds() <= settings.burst_window_s and \
                hamming(phash_int(a.phash), phash_int(b.phash)) <= settings.phash_burst_max:
            uf.union(a.unit_id, b.unit_id)

    by_burst: dict[tuple[str, str], list[str]] = defaultdict(list)
    for u in photos:
        ident = _burst_id(u.filename)
        if ident:
            by_burst[(posixpath.dirname(u.orig_rel_path), ident)].append(u.unit_id)
    for ids in by_burst.values():
        for other in ids[1:]:
            uf.union(ids[0], other)

    return [[by_id[x] for x in g] for g in uf.groups()]


def _group_signatures(conn: sqlite3.Connection) -> dict[str, set[tuple]]:
    """Per unit: {(kind, keeper id, frozenset of member ids)} over the groups it belongs to."""
    members: dict[str, list[str]] = defaultdict(list)
    for r in conn.execute("select group_id, unit_id from group_members"):
        members[r["group_id"]].append(r["unit_id"])
    sigs: dict[str, set[tuple]] = defaultdict(set)
    for g in conn.execute("select id, kind, suggested_keep_unit_id from groups"):
        ids = members.get(g["id"], [])
        sig = (g["kind"], g["suggested_keep_unit_id"], frozenset(ids))
        for uid in ids:
            sigs[uid].add(sig)
    return sigs


def run_grouping(
    conn: sqlite3.Connection, settings: Settings, root: Path | None = None
) -> GroupingResult:
    """Rebuild all groups. With `root`, pHash-close photos also pass a pixel-detail check."""
    views = load_unit_views(conn)
    planned: list[tuple[str, list[UnitView], str]] = []  # (kind, members, keeper id)

    exact = _exact_groups(views)
    excluded: set[str] = set()
    for members, keeper in exact:
        planned.append(("duplikat-persis", members, keeper))
        excluded.update(u.unit_id for u in members if u.unit_id != keeper)

    pool = [u for u in views if u.unit_id not in excluded]
    photos = [u for u in pool if u.kind == "photo"]
    videos = [u for u in pool if u.kind == "video"]

    detail = _DetailCheck(root, settings.version_detail_max) if root is not None else None
    version = _photo_version_groups(photos, settings.phash_version_max, detail)
    video_groups, clips = _video_version_groups(conn, videos, settings.video_frame_phash_max)
    version += video_groups
    for members in version:
        keeper = choose_keeper_version(members)
        planned.append(("duplikat-versi", members, keeper))
        excluded.update(u.unit_id for u in members if u.unit_id != keeper)

    # A clip of a non-keeper copy points at that group's keeper, the copy that stays.
    keeper_of = {u.unit_id: k for kind, members, k in planned if kind == "duplikat-versi"
                 for u in members}
    remapped = {(c, keeper_of.get(p, p)): off for c, p, off in clips}
    clips = [(c, p, off) for (c, p), off in remapped.items() if c != p]

    burst_pool = [u for u in photos if u.unit_id not in excluded]
    burst = _burst_groups(burst_pool, settings)
    for members in burst:
        keeper = max(members, key=lambda u: (u.sharpness if u.sharpness is not None else -1.0,
                                             -len(u.orig_rel_path), u.unit_id))
        planned.append(("mirip-beruntun", members, keeper.unit_id))

    with conn:
        before = _group_signatures(conn)
        conn.execute("delete from group_members")
        conn.execute("delete from groups")
        for kind, members, keeper in planned:
            gid = new_id()
            conn.execute(
                "insert into groups(id, kind, suggested_keep_unit_id) values(?,?,?)",
                (gid, kind, keeper),
            )
            conn.executemany(
                "insert into group_members(group_id, unit_id) values(?,?)",
                [(gid, u.unit_id) for u in members],
            )
        conn.execute("delete from video_clips")
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            "insert into video_clips(id, clip_unit_id, parent_unit_id, offset_s, created_at)"
            " values(?,?,?,?,?)",
            [(new_id(), c, p, off, now) for c, p, off in clips],
        )
        # A re-scan can change keepers or membership; an unapplied keep/discard decided against
        # the old group must not follow a unit that is now the keeper (or left its group).
        after = _group_signatures(conn)
        stale = [(u,) for u, sig in before.items() if after.get(u) != sig]
        conn.executemany("delete from decisions where unit_id=? and applied=0", stale)
    return GroupingResult(exact=len(exact), version=len(version), burst=len(burst))
