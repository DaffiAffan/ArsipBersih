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


def _fake_run(frames: int, stderr: bytes = b"boom"):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, b"" * (frames * 256 * 256 * 3), stderr)
    return run


def test_partial_decode_keeps_frames_and_records_error(monkeypatch, tmp_path):
    monkeypatch.setattr(videoframes.subprocess, "run", _fake_run(2))
    times = sample_plan(5.0)
    errors: list[str] = []
    grays, _ = extract("ffmpeg", tmp_path / "v.mp4", times, errors=errors)
    assert len(grays) == 2 < len(times) and errors and "2/5" in errors[0]


def test_no_frames_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(videoframes.subprocess, "run", _fake_run(0))
    with pytest.raises(RuntimeError, match="boom"):
        extract("ffmpeg", tmp_path / "v.mp4", [0.0, 1.0])


def test_warnings_on_complete_decode_are_not_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(videoframes.subprocess, "run", _fake_run(3, b"some warning"))
    errors: list[str] = []
    extract("ffmpeg", tmp_path / "v.mp4", [0.0, 1.0, 2.0], errors=errors)
    assert errors == []


@requires_ffmpeg
def test_analyze_video_records_partial_error(root, make_video, monkeypatch):
    v = make_video(root / "v.mp4", seconds=6)
    real = videoframes.subprocess.run

    def short(cmd, **kw):  # real decode, but pretend the stream died after 2 frames
        p = real(cmd, **kw)
        return subprocess.CompletedProcess(cmd, 1, p.stdout[: 2 * 256 * 256 * 3], b"died")

    monkeypatch.setattr(videoframes.subprocess, "run", short)
    r = analyze_video(v, 6.0, Settings(), find_tool("ffmpeg"), root / "v.webp")
    assert r.decode_ok and len(r.video_frames.times) == 2
    assert "partial video decode: 2/6" in r.error


@requires_ffmpeg
def test_sub_second_video_has_exactly_one_frame(root, make_video):
    v = make_video(root / "v.mp4", seconds=0.5)
    grays, crops = extract(find_tool("ffmpeg"), v, sample_plan(0.5))
    assert len(grays) == 1 and len(crops) == 1
    r = analyze_video(v, 0.5, Settings(), find_tool("ffmpeg"), root / "v.webp")
    assert r.decode_ok and len(r.video_frames.times) == 1 and len(r.frame_phashes) == 3
    assert (root / "v.webp").exists()


@requires_ffmpeg
def test_wrong_duration_falls_back_to_start(root, make_video):
    v = make_video(root / "v.mp4", seconds=3)
    r = analyze_video(v, 500.0, Settings(), find_tool("ffmpeg"), root / "v.webp")
    assert r.decode_ok and len(r.video_frames.times) >= 1 and (root / "v.webp").exists()


def test_three_hour_video_args_are_capped_and_keyframe_only(monkeypatch, tmp_path):
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"" * (256 * 256 * 3), b"")

    monkeypatch.setattr(videoframes.subprocess, "run", run)
    times = sample_plan(3 * 3600.0)
    extract("ffmpeg", tmp_path / "v.mp4", times)
    cmd = seen[0]
    assert len(times) == 120 and "-skip_frame" in cmd and "nokey" in cmd
    assert cmd[cmd.index("-frames:v") + 1] == "120"


@requires_ffmpeg
def test_analyze_video_keeps_three_frame_columns(root, make_video):
    v = make_video(root / "v.mp4", seconds=5)
    r = analyze_video(v, 5.0, Settings(), find_tool("ffmpeg"), root / "v.webp")
    assert r.decode_ok and len(r.frame_phashes) == 3 and len(r.frame_lumas) == 3
    assert (root / "v.webp").exists() and r.phash and r.ai_rgb_list is None
    assert len(r.video_frames.phashes) == len(r.video_frames.times) == 5


@requires_ffmpeg
def test_analyze_video_decodes_with_a_single_process(root, make_video, monkeypatch):
    v = make_video(root / "v.mp4", seconds=4)
    calls = []
    real = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or real(*a, **k))
    r = analyze_video(v, 4.0, Settings(), find_tool("ffmpeg"), root / "v.webp")
    assert r.decode_ok and len(calls) == 1 and (root / "v.webp").exists()


def test_decode_threads_capped_and_timeout_scales_with_duration(monkeypatch, tmp_path):
    seen = []

    def run(cmd, **kw):
        seen.append((cmd, kw["timeout"]))
        return subprocess.CompletedProcess(cmd, 0, b"\0" * (256 * 256 * 3), b"")

    monkeypatch.setattr(videoframes.subprocess, "run", run)
    for duration in (30.0, 1800.0, 10 * 3600.0):
        extract("ffmpeg", tmp_path / "v.mp4", sample_plan(duration))
    timeouts = [t for _, t in seen]
    for cmd, _ in seen:
        assert cmd[cmd.index("-threads") + 1] == "2" and cmd.index("-threads") < cmd.index("-i")
    assert timeouts[0] == videoframes.EXTRACT_TIMEOUT_S
    assert videoframes.EXTRACT_TIMEOUT_S < timeouts[1] < videoframes.EXTRACT_TIMEOUT_MAX_S
    assert timeouts[2] == videoframes.EXTRACT_TIMEOUT_MAX_S


def test_one_frame_short_is_a_complete_decode(monkeypatch, tmp_path):
    monkeypatch.setattr(videoframes.subprocess, "run", _fake_run(60, b""))
    errors: list[str] = []
    grays, _ = extract("ffmpeg", tmp_path / "v.mp4", sample_plan(61.7), errors=errors)
    assert len(grays) == 60 and errors == []
