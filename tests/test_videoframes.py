from __future__ import annotations

import subprocess

import pytest

from arsipbersih import videoframes
from arsipbersih.config import Settings
from arsipbersih.db import open_db
from arsipbersih.tools import find_tool
from arsipbersih.videoframes import (
    VideoFrameData,
    extract,
    load_video_frames,
    sample_plan,
    save_video_frames,
)
from arsipbersih.visual import analyze_video
from conftest import requires_ffmpeg


def test_sample_plan_caps_at_120():
    times = sample_plan(7200.0)
    assert len(times) == 120 and times == sorted(times) and times[-1] < 7200.0


def test_sample_plan_short_video_has_one_frame():
    assert len(sample_plan(0.4)) == 1
    assert sample_plan(None) == [0.0, 1.0, 2.0]


def test_save_and_load_roundtrip(root):
    conn = open_db(root)
    conn.execute("insert into files (id, rel_path) values ('f1', 'a.mp4')")
    data = VideoFrameData([0.5, 1.5], ["00" * 8, "ff" * 8], [10.0, 20.0], [1.0, 2.0], [0.0, 3.0])
    save_video_frames(conn, "f1", data)
    save_video_frames(conn, "f1", data)  # upsert, not a second row
    assert load_video_frames(conn, ["f1", "nope"]) == {"f1": data}


@requires_ffmpeg
def test_extract_uses_one_ffmpeg_process(root, make_video, monkeypatch):
    v = make_video(root / "v.mp4", seconds=5)
    calls = []
    real = subprocess.run
    monkeypatch.setattr(videoframes.subprocess, "run", lambda *a, **k: calls.append(a) or real(*a, **k))
    grays, crops = extract(find_tool("ffmpeg"), v, sample_plan(5.0))
    assert len(calls) == 1 and len(grays) == 5 and grays[0].shape == (64, 64)
    assert 1 <= len(crops) <= 3 and crops[0].shape == (256, 256, 3)


@requires_ffmpeg
def test_dark_video_lumas_low(root, make_video):
    v = make_video(root / "v.mp4", seconds=3, dark=True)
    r = analyze_video(v, 3.0, Settings(), find_tool("ffmpeg"), root / "v.webp", want_ai=True)
    assert max(r.video_frames.lumas) < 15 and len(r.ai_rgb_list) >= 1
    assert len(r.ai_rgb_list[0]) == 256 * 256 * 3


@requires_ffmpeg
def test_static_video_motion_near_zero(root, make_video):
    v = make_video(root / "v.mp4", seconds=4, dark=True)
    r = analyze_video(v, 4.0, Settings(), find_tool("ffmpeg"), root / "v.webp")
    assert r.video_frames.motion[0] == 0 and max(r.video_frames.motion) < 1.0
    moving = make_video(root / "m.mp4", seconds=4)
    m = analyze_video(moving, 4.0, Settings(), find_tool("ffmpeg"), root / "m.webp")
    assert max(m.video_frames.motion) > 1.0


@requires_ffmpeg
def test_corrupt_tail_still_returns_frames_or_records_error(root, make_video):
    v = make_video(root / "v.mp4", seconds=6)
    data = v.read_bytes()
    v.write_bytes(data[: len(data) * 3 // 4])  # chop the tail, keep the header duration
    r = analyze_video(v, 6.0, Settings(), find_tool("ffmpeg"), root / "v.webp")
    assert r.decode_ok is not True or r.video_frames.times
    assert r.decode_ok is not False or r.error


@requires_ffmpeg
def test_analyze_video_keeps_three_frame_columns(root, make_video):
    v = make_video(root / "v.mp4", seconds=5)
    r = analyze_video(v, 5.0, Settings(), find_tool("ffmpeg"), root / "v.webp")
    assert r.decode_ok and len(r.frame_phashes) == 3 and len(r.frame_lumas) == 3
    assert (root / "v.webp").exists() and r.phash and r.ai_rgb_list is None
    assert len(r.video_frames.phashes) == len(r.video_frames.times) == 5
