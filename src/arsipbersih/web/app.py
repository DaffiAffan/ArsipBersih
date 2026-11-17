"""FastAPI app factory: local-only guards, templates, static files, routers."""

from __future__ import annotations

import os
import signal
import sys
import threading
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..db import open_db
from ..lock import RootLock
from .jobs import RECONCILE_LABEL, JobRunner, reconcile_job
from .messages import UserError, explain
from .routes import home as home_routes
from .routes import jobs as jobs_routes

DEFAULT_PORT = 8765
WEB_DIR = Path(__file__).resolve().parent
CSRF_HEADER = "x-arsipbersih"

ROUTERS: list[APIRouter] = []  # later tasks append their routers here

ROUTERS.append(home_routes.router)
ROUTERS.append(jobs_routes.router)


from .routes import media as _media_routes  # noqa: E402

ROUTERS.append(_media_routes.router)

from .routes import review as _review_routes  # noqa: E402

ROUTERS.append(_review_routes.router)
ROUTERS.append(_review_routes.pages)

from .routes import plan as _plan_routes  # noqa: E402

ROUTERS.append(_plan_routes.router)

from .routes import report as _report_routes  # noqa: E402

ROUTERS.append(_report_routes.router)

from .routes import settings as _settings_routes  # noqa: E402

ROUTERS.append(_settings_routes.router)

from .routes import history as _history_routes  # noqa: E402

ROUTERS.append(_history_routes.router)
ROUTERS.append(_history_routes.pages)

from .routes import dates as _dates_routes  # noqa: E402

ROUTERS.append(_dates_routes.router)


def _drain_jobs(jobs: JobRunner) -> None:
    """Wait for a running job before shutdown. Scan is asked to stop (it resumes later);
    move jobs run to the end. A second Ctrl+C forces exit; the journal reconciles next start."""
    st = jobs.status()
    if st.status != "running":
        return
    if st.kind == "scan":
        jobs.stop()
    print(
        f"Menunggu proses '{st.label}' selesai… (tekan Ctrl+C sekali lagi untuk paksa keluar; "
        "jurnal akan merapikan saat dibuka lagi)",
        file=sys.stderr,
        flush=True,
    )
    prev = None
    installed = False
    if threading.current_thread() is threading.main_thread():
        def _force_exit(signum, frame):
            print(
                "Memaksa keluar. Jurnal akan merapikan perpindahan yang terputus "
                "saat ArsipBersih dibuka lagi.",
                file=sys.stderr,
                flush=True,
            )
            os._exit(130)  # no KeyboardInterrupt: uvicorn would swallow it and free the lock

        prev = signal.signal(signal.SIGINT, _force_exit)
        installed = True
    try:
        while jobs.status().status == "running":
            jobs.wait(0.5)  # short slices so signals are delivered on Windows
    finally:
        if installed:
            signal.signal(signal.SIGINT, prev)


def create_app(
    root: Path,
    *,
    port: int = DEFAULT_PORT,
    allowed_hosts: set[str] | None = None,
    start_jobs: bool = True,
    root_lock: RootLock | None = None,
) -> FastAPI:
    """`root_lock` is the collection lock taken by `serve`; tests pass none (the lock is
    only required where a process can move files: serve, the CLI, and the root switch)."""
    hosts = allowed_hosts if allowed_hosts is not None else {f"127.0.0.1:{port}", f"localhost:{port}"}
    app = FastAPI(title="ArsipBersih", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.root = Path(root)
    app.state.templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
    app.state.jobs = JobRunner()
    open_db(app.state.root).close()  # create/upgrade the schema once, before concurrent requests
    app.state.start_jobs = start_jobs
    app.state.root_lock = root_lock

    def _on_shutdown() -> None:
        _drain_jobs(app.state.jobs)  # before the lock goes: a move must not outlive it
        if app.state.root_lock is not None:
            app.state.root_lock.release()

    app.router.add_event_handler("shutdown", _on_shutdown)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host", "") not in hosts:
            return JSONResponse({"detail": "Host tidak diizinkan"}, status_code=400)
        if request.method not in ("GET", "HEAD") and request.headers.get(CSRF_HEADER) != "1":
            return JSONResponse({"detail": "Permintaan ditolak"}, status_code=403)
        try:
            return await call_next(request)
        except UserError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=exc.status)
        except Exception as exc:
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": explain(exc)}, status_code=500)
            raise

    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
    for router in ROUTERS:
        app.include_router(router)
    if start_jobs:
        app.state.jobs.start("reconcile", RECONCILE_LABEL, reconcile_job(app.state.root))
    return app
