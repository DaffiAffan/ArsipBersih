from __future__ import annotations

import random

from arsipbersih.config import Settings
from arsipbersih.grouping import UnitView, choose_keeper_version, run_grouping
from arsipbersih.videoframes import VideoFrameData, sample_plan, save_video_frames
from arsipbersih.videomatch import align, classify_pair

SCENE_S = 6.0  # synthetic content: one random pHash per 6-second scene


def scenes(seed: int, n: int = 200) -> list[int]:
    rng = random.Random(seed)
    return [rng.getrandbits(64) for _ in range(n)]


def video(content: list[int], start: float, duration: float, noise_bits: int = 0, seed: int = 1) -> VideoFrameData:
    """Frames of a video showing `content` from `start` for `duration` s, sampled like videoframes."""
    rng = random.Random(seed)
    times = sample_plan(duration)
    hashes = []
    for t in times:
        h = content[int((start + t) // SCENE_S)]
        for bit in rng.sample(range(64), noise_bits):
            h ^= 1 << bit
        hashes.append(f"{h:016x}")
    n = len(times)
    return VideoFrameData(times, hashes, [100.0] * n, [50.0] * n, [5.0] * n)


def test_reencoded_copy_is_duplicate():
    c = scenes(1)
    a, b = video(c, 0, 60), video(c, 0, 60, noise_bits=4)
    m = align(a, b, 8)
    assert m is not None and m.matched_fraction > 0.9 and abs(m.offset_s) < 1
    assert classify_pair(a, b, 60, 60) == "duplicate"


def test_copy_missing_one_second_at_end_is_duplicate():
    c = scenes(2)
    assert classify_pair(video(c, 0, 60), video(c, 0, 59, noise_bits=3), 60, 59) == "duplicate"


def test_trimmed_ten_seconds_is_clip_not_duplicate():
    c = scenes(3)
    assert classify_pair(video(c, 0, 60), video(c, 0, 50, noise_bits=3), 60, 50) == "clip"


def test_clip_from_middle_detected_with_offset():
    c = scenes(4)
    parent = video(c, 0, 300)  # 120 frames, 2.5 s apart
    clip = video(c, 100.3, 30, noise_bits=3)  # 30 frames, 1 s apart
    m = align(clip, parent, 8)
    assert m is not None and abs(m.offset_s - 100.3) <= 2.5
    assert classify_pair(parent, clip, 300, 30) == "clip"


def test_unrelated_videos_no_match():
    a, b = video(scenes(5), 0, 60), video(scenes(6), 0, 60)
    assert align(a, b, 8) is None
    assert classify_pair(a, b, 60, 60) == "none"


# ------------------------------------------------------------- grouping integration


def _save_frames(conn, name, data):
    fid = conn.execute("select id from files where rel_path like ?", (f"%/{name}",)).fetchone()[0]
    save_video_frames(conn, fid, data)
    conn.commit()


def test_gopro_chapters_not_matched(conn_with):
    c = scenes(7)
    conn = conn_with([
        dict(name="GX010123.MP4", kind="video", duration=60.0, w=1920, h=1080, sha="g1"),
        dict(name="GX020123.MP4", kind="video", duration=60.0, w=1920, h=1080, sha="g2"),
    ])
    _save_frames(conn, "GX010123.MP4", video(c, 0, 60))
    _save_frames(conn, "GX020123.MP4", video(c, 0, 60, noise_bits=2))
    assert run_grouping(conn, Settings()).version == 0
    assert conn.execute("select count(*) from video_clips").fetchone()[0] == 0


def test_wa_copy_slightly_longer_does_not_beat_original(conn_with):
    c = scenes(8)
    conn = conn_with([
        dict(name="VID_20230812_143022.mp4", path="Sumber/HP/DCIM/Camera", kind="video",
             duration=30.0, w=1920, h=1080, size=60_000_000, sha="o1"),
        dict(name="VID-20230812-WA0007.mp4", path="Sumber/HP/WhatsApp/Media/WhatsApp Video",
             kind="video", duration=30.3, w=1280, h=720, size=6_000_000, sha="w1"),
    ])
    _save_frames(conn, "VID_20230812_143022.mp4", video(c, 0, 30.0))
    _save_frames(conn, "VID-20230812-WA0007.mp4", video(c, 0, 30.3, noise_bits=4))
    assert run_grouping(conn, Settings()).version == 1
    keep = conn.execute(
        "select f.rel_path from groups g join units u on u.id = g.suggested_keep_unit_id"
        " join files f on f.id = u.primary_file_id"
    ).fetchone()[0]
    assert keep.endswith("VID_20230812_143022.mp4")


def _view(uid, duration, w, h, size, name="VID_1.mp4"):
    return UnitView(
        unit_id=uid, location="source", orig_rel_path=f"Sumber/HP/DCIM/{name}", filename=name,
        rel_path=f"Sumber/HP/DCIM/{name}", source_label="HP", make=None, model=None, width=w,
        height=h, size=size, sharpness=None, taken_at=None, phash=None, frame_phashes=None,
        duration_s=duration, kind="video", sha256=None,
    )


def test_clearly_longer_video_wins_keeper():
    # 20 s longer beats a sharper, bigger copy.
    assert choose_keeper_version([
        _view("a", 60.0, 1280, 720, 10_000_000), _view("b", 40.0, 1920, 1080, 90_000_000),
    ]) == "a"
    # 1.5 s longer is not "clearly longer": resolution decides.
    assert choose_keeper_version([
        _view("a", 61.5, 1280, 720, 10_000_000), _view("b", 60.0, 1920, 1080, 9_000_000),
    ]) == "b"
    # Same resolution: higher bitrate (size / duration) wins.
    assert choose_keeper_version([
        _view("a", 30.0, 1920, 1080, 30_000_000), _view("b", 30.5, 1920, 1080, 20_000_000),
    ]) == "a"


def test_clips_table_rewritten_each_run(conn_with):
    c = scenes(9)
    conn = conn_with([
        dict(name="VID_PARENT.mp4", kind="video", duration=300.0, w=1920, h=1080, sha="p1"),
        dict(name="VID_CLIP.mp4", kind="video", duration=30.0, w=1920, h=1080, sha="c1"),
        dict(name="VID_OTHER.mp4", kind="video", duration=30.0, w=1920, h=1080, sha="x1"),
    ])
    _save_frames(conn, "VID_PARENT.mp4", video(c, 0, 300))
    _save_frames(conn, "VID_CLIP.mp4", video(c, 100.3, 30, noise_bits=3))
    _save_frames(conn, "VID_OTHER.mp4", video(scenes(10), 0, 30))
    clips_sql = (
        "select fc.rel_path, fp.rel_path, k.offset_s from video_clips k"
        " join units uc on uc.id = k.clip_unit_id join files fc on fc.id = uc.primary_file_id"
        " join units up on up.id = k.parent_unit_id join files fp on fp.id = up.primary_file_id"
    )
    for _ in range(2):
        r = run_grouping(conn, Settings())
        assert r.version == 0  # a clip is not a duplicate
        rows = conn.execute(clips_sql).fetchall()
        assert len(rows) == 1
        assert rows[0][0].endswith("VID_CLIP.mp4") and rows[0][1].endswith("VID_PARENT.mp4")
        assert abs(rows[0][2] - 100.3) <= 2.5
    assert conn.execute("select count(*) from flags").fetchone()[0] == 0
    conn.execute("update files set missing=1 where rel_path like '%VID_CLIP.mp4'")
    conn.commit()
    run_grouping(conn, Settings())
    assert conn.execute("select count(*) from video_clips").fetchone()[0] == 0
