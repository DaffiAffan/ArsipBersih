"""Single-threaded job runner: at most one file-moving operation at a time."""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal

from ..mover import BatchResult
from ..pipeline import Progress
from .messages import explain

log = logging.getLogger(__name__)

JobFn = Callable[[Callable[[Progress], None], threading.Event], "dict | None"]


class JobBusy(Exception):
    def __init__(self, label: str):
        super().__init__(label)
        self.label = label


@dataclass
class JobState:
    id: str | None = None
    kind: str | None = None
    label: str | None = None
    status: Literal["idle", "running", "done", "failed", "stopped"] = "idle"
    progress: dict | None = None
    result: dict | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def batch_result_dict(r: BatchResult) -> dict:
    return {
        "batch_id": r.batch_id,
        "moved_units": r.moved_units,
        "failed_units": r.failed_units,
        "skipped_units": r.skipped_units,
        "errors": list(r.errors),
    }


class JobRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = JobState()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def _snapshot(self) -> JobState:
        return JobState(**asdict(self._state))

    def start(self, kind: str, label: str, fn: JobFn) -> JobState:
        with self._lock:
            if self._state.status == "running":
                raise JobBusy(self._state.label or "")
            self._stop = threading.Event()
            self._state = JobState(
                id=uuid.uuid4().hex, kind=kind, label=label, status="running", started_at=_now()
            )
            self._thread = threading.Thread(
                target=self._run,
                args=(self._state.id, fn, self._stop),
                daemon=True,
                name=f"job-{kind}",
            )
            self._thread.start()
            return self._snapshot()

    def _run(self, job_id: str, fn: JobFn, stop: threading.Event) -> None:
        def report(p: Progress) -> None:
            with self._lock:
                if self._state.id == job_id:
                    self._state.progress = {
                        "stage": p.stage, "done": p.done, "total": p.total, "eta_s": p.eta_s,
                    }

        result: dict | None = None
        error: str | None = None
        status = "done"
        try:
            result = fn(report, stop)
            if stop.is_set():
                status = "stopped"
        except Exception as exc:
            log.exception("job %s failed", job_id)
            status, error = "failed", explain(exc)
        with self._lock:
            if self._state.id == job_id:
                self._state.status = status
                self._state.result = result
                self._state.error = error
                self._state.finished_at = _now()

    def status(self) -> JobState:
        with self._lock:
            return self._snapshot()

    def stop(self) -> None:
        with self._lock:
            if self._state.status == "running":
                self._stop.set()

    def busy(self) -> bool:
        with self._lock:
            return self._state.status == "running"

    def wait(self, timeout: float = 30) -> JobState:
        t = self._thread
        if t is not None:
            t.join(timeout)
        return self.status()
