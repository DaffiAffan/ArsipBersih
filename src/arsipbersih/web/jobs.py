"""Single-threaded job runner: at most one file-moving operation at a time."""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from ..db import open_db
from ..mover import BatchResult, reconcile
from ..pipeline import Progress
from .messages import explain

log = logging.getLogger(__name__)

JobFn = Callable[[Callable[[Progress], None], threading.Event], "dict | None"]


class JobPrecheckFailed(Exception):
    """A start() precheck refused the job (e.g. the collection changed under the request)."""


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


RECONCILE_LABEL = "Merapikan jurnal"


def reconcile_job(root: Path) -> JobFn:
    """Settle interrupted moves of the move journal (app start, root switch)."""

    def run(report, stop):
        conn = open_db(root)  # sqlite connections are thread-bound
        try:
            errs: list[str] = []
            fixed = reconcile(conn, root, errs)
            pending = conn.execute("select count(*) from moves where status='pending'").fetchone()[0]
            return {"fixed": fixed, "errors": errs, "pending": pending}
        finally:
            conn.close()

    return run


class JobRunner:
    def __init__(self) -> None:
        # Re-entrant so exclusive() can start a job (e.g. reconcile after a root switch).
        self._lock = threading.RLock()
        self._state = JobState()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def _snapshot(self) -> JobState:
        return JobState(**asdict(self._state))

    def start(
        self, kind: str, label: str, fn: JobFn, precheck: Callable[[], bool] | None = None
    ) -> JobState:
        with self._lock:
            if self._state.status == "running":
                raise JobBusy(self._state.label or "")
            if precheck is not None and not precheck():
                raise JobPrecheckFailed(kind)
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
        except BaseException as exc:
            status, result = "failed", None
            try:
                log.exception("job %s failed", job_id)
                error = explain(exc)
            except Exception:
                error = "Proses gagal karena kesalahan yang tidak terduga."
        finally:
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

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """Run a block while no job runs and none can start; raise JobBusy if one runs."""
        with self._lock:
            if self._state.status == "running":
                raise JobBusy(self._state.label or "")
            yield

    def busy(self) -> bool:
        with self._lock:
            return self._state.status == "running"

    def wait(self, timeout: float = 30) -> JobState:
        with self._lock:
            t = self._thread
        if t is not None:
            t.join(timeout)
        return self.status()
