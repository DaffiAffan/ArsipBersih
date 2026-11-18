from __future__ import annotations

import threading
from concurrent.futures import Future
from pathlib import Path

import numpy as np
import pytest

from arsipbersih import visual
from arsipbersih.ai import stage
from arsipbersih.ai.model import MODEL_NAME, ModelStatus
from arsipbersih.config import Settings
from arsipbersih.db import open_db
from arsipbersih.inventory import run_inventory
from arsipbersih.pairing import build_units
from arsipbersih.pipeline import SCAN_DONE_KEY, run_scan

NOT_READY_WARNING = "Model AI belum disiapkan — jalankan: arsipbersih setup-models"


class FakeEncoder:
    """Embedding = normalized mean colour (plus a constant, so black stays finite)."""

    made = 0

    def __init__(self, path=None, threads: int = 4) -> None:
        FakeEncoder.made += 1
        self.batches: list[int] = []

    def embed(self, batch: np.ndarray) -> np.ndarray:
        assert batch.dtype == np.uint8 and batch.shape[1:] == (256, 256, 3)
        self.batches.append(len(batch))
        v = batch.reshape(len(batch), -1, 3).mean(axis=1) + 1.0
        return (v / np.linalg.norm(v, axis=1, keepdims=True)).astype(np.float32)


class FakeHead:
    name = "fake"
    version = "v0"

    def predict(self, emb, facts):
        assert len(emb) == len(facts)
        return [("lainnya", None, 1.0) for _ in facts]


class InlinePool:
    """In-process stand-in for the ProcessPoolExecutor, so monkeypatches reach the workers."""

    def __init__(self, *a, **k):
        pass

    def submit(self, fn, *args):
        f = Future()
        try:
            f.set_result(fn(*args))
        except BaseException as e:  # noqa: BLE001 - mirror a real future
            f.set_exception(e)
        return f

    def shutdown(self, *a, **k):
        pass


@pytest.fixture
def ai_ready(monkeypatch, tmp_path):
    FakeEncoder.made = 0
    monkeypatch.setattr(stage, "model_status", lambda: ModelStatus(True, tmp_path, "siap"))
    monkeypatch.setattr(stage, "VisionEncoder", FakeEncoder)
    monkeypatch.setattr(stage, "get_head", lambda name, root=None: FakeHead())
    monkeypatch.setattr(visual, "_pool", InlinePool)


@pytest.fixture
def load_calls(monkeypatch):
    calls: list[str] = []
    real = visual._load_rgb

    def counting(path, kind, max_px):
        calls.append(Path(path).name)
        return real(path, kind, max_px)

    monkeypatch.setattr(visual, "_load_rgb", counting)
    return calls


def _units(root: Path):
    conn = open_db(root)
    run_inventory(conn, root)
    build_units(conn)
    return conn


def _count(conn, table: str) -> int:
    return conn.execute(f"select count(*) from {table} where model = ?", (MODEL_NAME,)).fetchone()[0]


def test_scan_without_model_finishes_with_warning(root, mini_collection, monkeypatch):
    monkeypatch.setattr(stage, "model_status", lambda: ModelStatus(False, root, "belum disiapkan"))
    seen: list[str] = []
    s = run_scan(root, progress=lambda p: seen.append(p.stage))
    assert not s.stopped
    assert NOT_READY_WARNING in s.warnings
    order = list(dict.fromkeys(seen))
    assert order[order.index("visual") + 1 :][:3] == ["video", "ai", "grouping"]
    conn = open_db(root)
    assert conn.execute("select 1 from settings where key = ?", (SCAN_DONE_KEY,)).fetchone()
    assert _count(conn, "ai_embeddings") == 0


def test_corrupt_model_is_a_warning_not_a_crash(root, make_image, monkeypatch):
    make_image(root / "Sumber/HP/a.jpg", size=(400, 300))

    def broken(path, threads=4):
        raise RuntimeError("protobuf parsing failed")

    monkeypatch.setattr(stage, "model_status", lambda: ModelStatus(True, root, "siap"))
    monkeypatch.setattr(stage, "VisionEncoder", broken)
    s = run_scan(root)
    assert not s.stopped
    assert any("Model AI" in w and "protobuf parsing failed" in w for w in s.warnings)
    conn = open_db(root)
    assert conn.execute("select 1 from settings where key = ?", (SCAN_DONE_KEY,)).fetchone()


def test_visual_stage_feeds_ai_without_second_decode(root, make_image, ai_ready, load_calls):
    names = ["a.jpg", "b.jpg", "c.png"]
    for i, n in enumerate(names):
        make_image(root / "Sumber/HP" / n, size=(700 + 50 * i, 500), seed=20 + i)
    at_grouping: list[int] = []

    def progress(p):
        if p.stage == "grouping" and not at_grouping:
            at_grouping.append(len(load_calls))

    s = run_scan(root, progress=progress)
    assert not s.stopped and NOT_READY_WARNING not in s.warnings
    assert at_grouping == [3]  # visual decoded each photo once; the ai stage decoded nothing
    assert sorted(load_calls[:3]) == sorted(names)
    conn = open_db(root)
    assert _count(conn, "ai_embeddings") == 3
    assert conn.execute("select count(*) from ai_labels where head = 'fake'").fetchone()[0] == 3
    assert FakeEncoder.made == 1  # one session for the visual sink and the ai stage


def test_collector_ignores_non_primary_files(root, conn_with):
    from arsipbersih.db import new_id

    conn = conn_with([{"name": "IMG_1.jpg"}])
    unit_id, primary = conn.execute("select id, primary_file_id from units").fetchone()
    other = new_id()
    conn.execute(
        "insert into files(id, unit_id, rel_path, orig_rel_path, size, mtime_ns, kind, ext)"
        " values(?,?,?,?,?,?,?,?)",
        (other, unit_id, "Sumber/HP/DCIM/IMG_1.dng", "Sumber/HP/DCIM/IMG_1.dng", 1, 0, "raw", "dng"),
    )
    conn.commit()
    enc = FakeEncoder()
    col = stage.AiCollector(enc, conn, MODEL_NAME, batch=16)
    crop = np.full((256, 256, 3), 90, np.uint8).tobytes()
    col.add(other, crop)
    col.add(primary, crop)
    col.flush()
    assert enc.batches == [1]
    assert [r[0] for r in conn.execute("select unit_id from ai_embeddings")] == [unit_id]


def test_backfill_embeds_units_visual_already_done(root, make_image, monkeypatch, load_calls):
    for i in range(3):
        make_image(root / f"Sumber/HP/old_{i}.jpg", size=(800, 600), seed=40 + i)
    monkeypatch.setattr(visual, "_pool", InlinePool)
    conn = _units(root)
    visual.run_visual(conn, root, Settings())  # an older scan: visual finished, no AI then
    assert len(load_calls) == 3 and _count(conn, "ai_embeddings") == 0
    load_calls.clear()

    monkeypatch.setattr(stage, "model_status", lambda: ModelStatus(True, root, "siap"))
    monkeypatch.setattr(stage, "get_head", lambda name, root=None: FakeHead())
    r = stage.run_ai(conn, root, Settings(), encoder_factory=FakeEncoder)
    assert (r.embedded, r.errors, r.skipped_reason) == (3, 0, None)
    assert sorted(load_calls) == ["old_0.jpg", "old_1.jpg", "old_2.jpg"]
    assert _count(conn, "ai_embeddings") == 3
    assert conn.execute("select count(*) from ai_labels").fetchone()[0] == 3


def test_ai_stage_resumes_after_stop(root, make_image, ai_ready, load_calls):
    for i in range(5):
        make_image(root / f"Sumber/HP/p{i}.jpg", size=(400, 300), seed=60 + i)
    conn = _units(root)
    stop = threading.Event()

    class StoppingEncoder(FakeEncoder):
        def embed(self, batch):
            out = super().embed(batch)
            stop.set()  # pause right after the first batch
            return out

    settings = Settings(ai_batch=2)
    r1 = stage.run_ai(conn, root, settings, stop=stop, encoder_factory=StoppingEncoder)
    assert r1.embedded == 2 and _count(conn, "ai_embeddings") == 2
    assert len(load_calls) == 2
    r2 = stage.run_ai(conn, root, settings, encoder_factory=FakeEncoder)
    assert r2.embedded == 3 and _count(conn, "ai_embeddings") == 5
    assert sorted(load_calls) == [f"p{i}.jpg" for i in range(5)]  # nothing decoded twice


def test_ai_skips_videos(root, make_image, ai_ready, load_calls):
    make_image(root / "Sumber/HP/a.jpg", size=(400, 300))
    (root / "Sumber/HP/clip.mp4").write_bytes(b"\x00" * 64)
    conn = _units(root)
    r = stage.run_ai(conn, root, Settings(), encoder_factory=FakeEncoder)
    assert (r.embedded, r.errors) == (1, 0)
    assert load_calls == ["a.jpg"]
    assert _count(conn, "ai_errors") == 0
    kinds = conn.execute(
        "select f.kind from ai_embeddings e join units u on u.id = e.unit_id"
        " join files f on f.id = u.primary_file_id"
    ).fetchall()
    assert [k[0] for k in kinds] == ["photo"]


def test_decode_failure_records_ai_error_and_continues(root, make_image, ai_ready):
    make_image(root / "Sumber/HP/good.jpg", size=(400, 300))
    (root / "Sumber/HP/bad.jpg").write_bytes(b"this is not a picture")
    conn = _units(root)
    r = stage.run_ai(conn, root, Settings(), encoder_factory=FakeEncoder)
    assert (r.embedded, r.errors) == (1, 1)
    row = conn.execute(
        "select f.rel_path, x.error from ai_errors x join units u on u.id = x.unit_id"
        " join files f on f.id = u.primary_file_id"
    ).fetchone()
    assert row[0].endswith("bad.jpg") and row[1]
    again = stage.run_ai(conn, root, Settings(), encoder_factory=FakeEncoder)
    assert (again.embedded, again.errors) == (0, 0)  # recorded failures are not retried


def test_relabel_all_uses_named_head(root, make_image, ai_ready, monkeypatch):
    make_image(root / "Sumber/HP/a.jpg", size=(400, 300))
    conn = _units(root)
    stage.run_ai(conn, root, Settings(), encoder_factory=FakeEncoder)
    conn.execute("delete from ai_labels")
    conn.commit()
    asked: list[str] = []
    monkeypatch.setattr(stage, "get_head", lambda name, root=None: asked.append(name) or FakeHead())
    assert stage.relabel_all(conn, root, Settings(), "trained-v1") == 1
    assert asked == ["trained-v1"]
    assert tuple(conn.execute("select head, grp from ai_labels").fetchone()) == ("fake", "lainnya")
