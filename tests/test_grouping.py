from __future__ import annotations

import time

import numpy as np
import pytest

from arsipbersih.config import Settings
from arsipbersih.grouping import find_close_pairs, run_grouping
from arsipbersih.pathrules import is_chat, is_trash, segments_of


def unit_location(conn, unit_id):
    return conn.execute("select location from units where id=?", (unit_id,)).fetchone()[0]


def keeper_name(conn, kind):
    return conn.execute(
        "select f.rel_path from groups g join units u on u.id = g.suggested_keep_unit_id "
        "join files f on f.id = u.primary_file_id where g.kind=?",
        (kind,),
    ).fetchone()[0].rsplit("/", 1)[-1]


def member_count(conn, kind):
    return conn.execute(
        "select count(*) from group_members m join groups g on g.id = m.group_id where g.kind=?",
        (kind,),
    ).fetchone()[0]


def test_exact_keeper_prefers_archive_then_camera_original(conn_with):
    conn = conn_with([
        dict(name="IMG-20230812-WA0001.jpg", path="Sumber/HP/WhatsApp/Media/x", sha="aa", loc="source"),
        dict(name="IMG_20230812_143022.jpg", path="Sumber/HP/DCIM/Camera", sha="aa", loc="source", make="samsung"),
        dict(name="IMG_20230812_143022.jpg", path="Arsip/2023/2023-08", sha="aa", loc="archive"),
    ])
    run_grouping(conn, Settings())
    g = conn.execute("select suggested_keep_unit_id from groups where kind='duplikat-persis'").fetchone()[0]
    assert unit_location(conn, g) == "archive"


def test_version_group_keeps_highest_resolution(conn_with):
    conn = conn_with([
        dict(name="IMG_1.jpg", phash="f0f0f0f0f0f0f0f0", w=4000, h=3000, sha="a1"),
        dict(name="IMG-20230812-WA0002.jpg", phash="f0f0f0f0f0f0f0f1", w=1600, h=1200, sha="a2"),
        dict(name="IMG_9.jpg", phash="0f0f0f0f0f0f0f0f", w=4000, h=3000, sha="a3"),
    ])
    r = run_grouping(conn, Settings())
    assert r.version == 1
    assert keeper_name(conn, "duplikat-versi") == "IMG_1.jpg"


def test_gopro_chapters_never_grouped(conn_with):
    conn = conn_with([
        dict(name="GX010123.MP4", kind="video", duration=60.0, frames=["00" * 8] * 3, sha="v1"),
        dict(name="GX020123.MP4", kind="video", duration=60.0, frames=["00" * 8] * 3, sha="v2"),
    ])
    assert run_grouping(conn, Settings()).version == 0


def test_burst_chain_keeps_sharpest(conn_with):
    conn = conn_with([
        dict(name="IMG_A.jpg", src="HP", taken="2023-08-12T05:41:03", phash="ff00ff00ff00ff00", sharp=94, sha="b1"),
        dict(name="IMG_B.jpg", src="HP", taken="2023-08-12T05:41:05", phash="ff00ff00ff00ff0f", sharp=48, sha="b2"),
        dict(name="IMG_C.jpg", src="HP", taken="2023-08-12T05:41:30", phash="ff00ff00ff00ff00", sharp=90, sha="b3"),
    ])
    assert run_grouping(conn, Settings()).burst == 1
    assert keeper_name(conn, "mirip-beruntun") == "IMG_A.jpg"
    assert member_count(conn, "mirip-beruntun") == 2


def test_same_camera_sequential_shots_are_burst_not_version(conn_with):
    conn = conn_with([
        dict(name="IMG_A.jpg", src="HP", taken="2023-08-12T05:41:03", phash="ff00ff00ff00ff00", sha="s1"),
        dict(name="IMG_B.jpg", src="HP", taken="2023-08-12T05:41:06", phash="ff00ff00ff00ff03", sha="s2"),
    ])
    r = run_grouping(conn, Settings())
    assert (r.version, r.burst) == (0, 1)
    assert member_count(conn, "mirip-beruntun") == 2


def test_pathrules():
    assert is_chat(["WhatsApp", "Media", "WhatsApp Images"], "IMG_1.jpg")
    assert is_chat([], "VID-20230812-WA0007.mp4")
    assert is_trash(["DCIM"], ".trashed-1690000000-IMG_1.jpg")
    assert segments_of("Sumber/HP-Lama/Rinjani/Hari-1/IMG.jpg") == ["Rinjani", "Hari-1"]


@pytest.mark.slow
def test_version_search_scales():
    hashes = np.random.default_rng(0).integers(0, 2**64, size=80_000, dtype=np.uint64)
    start = time.perf_counter()
    find_close_pairs(hashes, 6)
    assert time.perf_counter() - start < 300
