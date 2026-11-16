import threading

import pytest

from arsipbersih.mover import PendingMoves
from arsipbersih.web.deps import start_job
from arsipbersih.web.jobs import JobBusy, JobRunner
from arsipbersih.web.messages import UserError


def test_second_job_is_refused_while_one_runs():
    jr, gate = JobRunner(), threading.Event()
    jr.start("t", "uji", lambda report, stop: (gate.wait(5), {"ok": 1})[1])
    with pytest.raises(JobBusy):
        jr.start("t2", "uji 2", lambda r, s: None)
    gate.set()
    st = jr.wait()
    assert st.status == "done" and st.result == {"ok": 1}


def test_failed_job_carries_indonesian_message():
    jr = JobRunner()

    def boom(r, s):
        raise PendingMoves("x")

    jr.start("tidy", "Rapikan", boom)
    st = jr.wait()
    assert st.status == "failed" and st.error.startswith("Ada perpindahan yang terputus")


def test_api_job_status_and_409(web):
    jr = web.app_ref.state.jobs
    gate = threading.Event()
    jr.start("t", "uji", lambda r, s: gate.wait(5) and None)
    assert web.get("/api/job").json()["status"] == "running"

    class Req:
        app = web.app_ref

    with pytest.raises(UserError) as ei:
        start_job(Req, "t2", "uji 2", lambda r, s: None)
    assert ei.value.status == 409 and "uji" in str(ei.value)
    gate.set()
    jr.wait()


def test_stop_endpoint_sets_stop_event(web):
    jr = web.app_ref.state.jobs
    jr.start("scan", "Pindai", lambda r, s: (s.wait(5), {})[1])
    assert web.post("/api/job/stop").status_code == 204
    assert jr.wait().status == "stopped"
