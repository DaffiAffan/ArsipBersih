"""FastAPI app factory: local-only guards, templates, static files, routers."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..db import open_db
from ..mover import reconcile
from .jobs import JobRunner
from .messages import UserError, explain
from .routes import home as home_routes
from .routes import jobs as jobs_routes

DEFAULT_PORT = 8765
WEB_DIR = Path(__file__).resolve().parent
CSRF_HEADER = "x-arsipbersih"

ROUTERS: list[APIRouter] = []  # later tasks append their routers here

ROUTERS.append(home_routes.router)
ROUTERS.append(jobs_routes.router)


def _reconcile_job(root: Path):
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

from .routes import media as _media_routes  # noqa: E402

ROUTERS.append(_media_routes.router)

from .routes import review as _review_routes  # noqa: E402

ROUTERS.append(_review_routes.router)

from .routes import plan as _plan_routes  # noqa: E402

ROUTERS.append(_plan_routes.router)

from .routes import report as _report_routes  # noqa: E402

ROUTERS.append(_report_routes.router)


def create_app(
    root: Path,
    *,
    port: int = DEFAULT_PORT,
    allowed_hosts: set[str] | None = None,
    start_jobs: bool = True,
) -> FastAPI:
    hosts = allowed_hosts if allowed_hosts is not None else {f"127.0.0.1:{port}", f"localhost:{port}"}
    app = FastAPI(title="ArsipBersih", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.root = Path(root)
    app.state.templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
    app.state.jobs = JobRunner()
    open_db(app.state.root).close()  # create/upgrade the schema once, before concurrent requests
    app.state.start_jobs = start_jobs

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
        app.state.jobs.start("reconcile", "Merapikan jurnal", _reconcile_job(app.state.root))
    return app
