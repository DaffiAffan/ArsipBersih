from __future__ import annotations

import time

import numpy as np
import pytest

from arsipbersih.config import Settings
from arsipbersih.decisions import record
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


def test_exact_keeper_prefers_raw_jpg_unit_over_archived_bare_copy(conn_with):
    from arsipbersih.db import new_id

    conn = conn_with([
        dict(name="IMG_1.jpg", path="Sumber/HP/DCIM", sha="aa", loc="source"),
        dict(name="IMG_1.jpg", path="Arsip/2023/2023-08", sha="aa", loc="archive"),
    ])
    raw_unit = conn.execute("select unit_id from files where rel_path like 'Sumber/%'").fetchone()[0]
    conn.execute(
        "insert into files(id, unit_id, rel_path, orig_rel_path, size, mtime_ns, kind, ext, source_label)"
        " values(?,?,?,?,?,?,?,?,?)",
        (new_id(), raw_unit, "Sumber/HP/DCIM/IMG_1.dng", "Sumber/HP/DCIM/IMG_1.dng", 20_000_000, 0,
         "raw", "dng", "HP"),
    )
    conn.execute("update units set pair_kind='raw_jpg' where id=?", (raw_unit,))
    run_grouping(conn, Settings())
    g = conn.execute("select suggested_keep_unit_id from groups where kind='duplikat-persis'").fetchone()[0]
    assert g == raw_unit


def test_missing_keeper_copy_does_not_group_with_surviving_twin(conn_with):
    conn = conn_with([
        dict(name="IMG_20230812_143022.jpg", path="Sumber/HP/DCIM/Camera", sha="aa", loc="source"),
        dict(name="IMG_20230812_143022.jpg", path="Arsip/2023/2023-08", sha="aa", loc="archive"),
    ])
    conn.execute("update files set missing=1 where rel_path like 'Arsip/%'")
    assert run_grouping(conn, Settings()).exact == 0
    assert conn.execute("select count(*) from group_members").fetchone()[0] == 0


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


def test_burst_ids_chain_within_folder(conn_with):
    d = "Sumber/HP/DCIM/Camera"
    conn = conn_with([
        dict(name="00001IMG_00001_BURST20230812073015123.jpg", path=d, sha="p1"),
        dict(name="00002IMG_00002_BURST20230812073015123.jpg", path=d, sha="p2"),
        dict(name="00003IMG_00003_BURST20230812073015123_COVER.jpg", path=d, sha="p3"),
        dict(name="IMG_20230812_143022_BURST001.jpg", path=d, sha="q1"),
        dict(name="IMG_20230812_143022_BURST002.jpg", path=d, sha="q2"),
    ])
    assert run_grouping(conn, Settings()).burst == 2
    sizes = conn.execute(
        "select count(*) from group_members m join groups g on g.id = m.group_id "
        "where g.kind='mirip-beruntun' group by g.id order by 1"
    ).fetchall()
    assert [r[0] for r in sizes] == [2, 3]


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


def test_stale_decisions_cleared_when_keeper_changes(conn_with):
    conn = conn_with([
        dict(name="IMG_A.jpg", path="Sumber/HP/DCIM", sha="s1", make="Canon"),
        dict(name="IMG-20230101-WA0001.jpg", path="Sumber/HP/WhatsApp", sha="s1"),
    ])
    s = Settings()
    run_grouping(conn, s)
    keeper = conn.execute("select suggested_keep_unit_id from groups").fetchone()[0]
    other = conn.execute(
        "select unit_id from group_members where unit_id != ?", (keeper,)
    ).fetchone()[0]
    record(conn, keeper, "keep")
    record(conn, other, "discard")
    run_grouping(conn, s)  # nothing changed
    assert conn.execute("select count(*) from decisions").fetchone()[0] == 2
    conn.execute("update units set location='archive' where id=?", (other,))
    conn.commit()
    run_grouping(conn, s)  # archive wins: keeper flips to `other`
    assert conn.execute("select suggested_keep_unit_id from groups").fetchone()[0] == other
    assert conn.execute("select count(*) from decisions").fetchone()[0] == 0


def test_date_only_filename_times_do_not_form_bursts(conn_with):
    conn = conn_with([
        dict(name=f"IMG-20230114-WA00{i:02d}.jpg", path="Sumber/HP/WhatsApp/Media/Images", src="HP",
             taken="2023-01-14T00:00:00", date_source="filename", phash="ff00ff00ff00ff00",
             w=1280, h=720, sha=f"w{i}")
        for i in range(3)
    ] + [
        dict(name="IMG_20230812_143022.jpg", src="HP", taken="2023-08-12T14:30:22",
             date_source="filename", phash="00ff00ff00ff00ff", sha="t1"),
        dict(name="IMG_20230812_143024.jpg", src="HP", taken="2023-08-12T14:30:24",
             date_source="filename", phash="00ff00ff00ff00ff", sha="t2"),
    ])
    assert run_grouping(conn, Settings()).burst == 1
    names = {r[0].rsplit("/", 1)[-1] for r in conn.execute(
        "select f.rel_path from group_members m join groups g on g.id = m.group_id "
        "join units u on u.id = m.unit_id join files f on f.id = u.primary_file_id "
        "where g.kind='mirip-beruntun'")}
    assert names == {"IMG_20230812_143022.jpg", "IMG_20230812_143024.jpg"}
