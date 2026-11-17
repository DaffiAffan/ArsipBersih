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
    jr.start("scan", "Scan", lambda r, s: (s.wait(5), {})[1])
    assert web.post("/api/job/stop").status_code == 204
    assert jr.wait().status == "stopped"


def test_base_exception_does_not_wedge_runner():
    jr = JobRunner()

    def die(r, s):
        raise SystemExit(1)

    jr.start("t", "uji", die)
    st = jr.wait()
    assert st.status == "failed" and st.error
    jr.start("t2", "uji 2", lambda r, s: {"ok": 1})
    assert jr.wait().status == "done"


def test_get_conn_survives_parallel_requests(web):
    from concurrent.futures import ThreadPoolExecutor

    from fastapi import Depends

    from arsipbersih.web.deps import get_conn

    @web.app_ref.router.get("/api/_conn_probe")
    def probe(conn=Depends(get_conn)):
        return {"n": conn.execute("select 1").fetchone()[0]}

    with ThreadPoolExecutor(8) as ex:
        rs = list(ex.map(lambda _: web.get("/api/_conn_probe"), range(20)))
        codes = [r.status_code for r in rs]

    assert codes == [200] * 20


def _run_shutdown(app):
    done = threading.Event()

    def go():
        for h in app.router.on_shutdown:
            h()
        done.set()

    threading.Thread(target=go, daemon=True).start()
    return done


class _Lock:
    def __init__(self):
        self.released = threading.Event()

    def release(self):
        self.released.set()


def test_shutdown_waits_for_move_job_before_releasing_lock(root, capsys):
    from arsipbersih.web.app import create_app

    lock = _Lock()
    app = create_app(root, start_jobs=False, root_lock=lock)
    gate = threading.Event()
    app.state.jobs.start("apply", "Apply burem", lambda r, s: (gate.wait(10), {})[1])
    done = _run_shutdown(app)
    assert not done.wait(0.5)
    assert not lock.released.is_set()
    gate.set()
    assert done.wait(5)
    assert lock.released.is_set()
    assert "Apply burem" in capsys.readouterr().err


def test_shutdown_stops_scan_then_waits(root):
    from arsipbersih.web.app import create_app

    lock = _Lock()
    app = create_app(root, start_jobs=False, root_lock=lock)
    seen = threading.Event()

    def scan(r, s):
        s.wait(10)
        seen.set()
        return {}

    app.state.jobs.start("scan", "Scan", scan)
    done = _run_shutdown(app)
    assert done.wait(5)
    assert seen.is_set() and lock.released.is_set()
    assert app.state.jobs.status().status == "stopped"
