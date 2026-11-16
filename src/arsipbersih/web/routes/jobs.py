"""Job status and stop endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

router = APIRouter()


@router.get("/api/job")
def job_status(request: Request):
    return request.app.state.jobs.status().as_dict()


@router.post("/api/job/stop", status_code=204)
def job_stop(request: Request):
    request.app.state.jobs.stop()
    return Response(status_code=204)
