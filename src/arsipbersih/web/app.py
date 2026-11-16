"""FastAPI app factory: local-only guards, templates, static files, routers."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .deps import render
from .messages import UserError, explain

DEFAULT_PORT = 8765
WEB_DIR = Path(__file__).resolve().parent
CSRF_HEADER = "x-arsipbersih"

ROUTERS: list[APIRouter] = []  # later tasks append their routers here

_pages = APIRouter()


@_pages.get("/")
def home(request: Request):
    return render(request, "placeholder.html", active="beranda", title="Beranda")


ROUTERS.append(_pages)

from .routes import media as _media_routes  # noqa: E402

ROUTERS.append(_media_routes.router)


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
    app.state.jobs = None  # installed by the job runner task
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
    return app
