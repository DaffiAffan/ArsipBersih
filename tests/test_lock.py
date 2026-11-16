from __future__ import annotations

import subprocess
import sys

import pytest
from typer.testing import CliRunner

from arsipbersih.cli import LOCKED, app
from arsipbersih.lock import RootLocked, acquire_root_lock, lock_path

runner = CliRunner()

_CHILD = """
import sys
from arsipbersih.lock import RootLocked, acquire_root_lock
try:
    acquire_root_lock(sys.argv[1]).release()
except RootLocked:
    sys.exit(3)
"""


def _child_exit_code(root) -> int:
    return subprocess.run([sys.executable, "-c", _CHILD, str(root)], timeout=60).returncode


def test_second_acquire_same_process_is_refused(root):
    lock = acquire_root_lock(root)
    try:
        with pytest.raises(RootLocked):
            acquire_root_lock(root)
        assert lock_path(root).read_text().strip().isdigit()  # PID for diagnostics
    finally:
        lock.release()


def test_release_then_reacquire(root):
    with acquire_root_lock(root) as lock:
        assert lock.held
    assert not lock.held
    lock.release()  # idempotent
    acquire_root_lock(root).release()


def test_other_process_is_refused_until_release(root):
    lock = acquire_root_lock(root)
    try:
        assert _child_exit_code(root) == 3
    finally:
        lock.release()
    assert _child_exit_code(root) == 0


@pytest.mark.parametrize("args", [["tidy", "--yes"], ["scan"], ["undo", "--all"]])
def test_cli_refuses_while_locked(root, args):
    with acquire_root_lock(root):
        res = runner.invoke(app, [args[0], str(root), *args[1:]])
    assert res.exit_code == 1
    assert LOCKED in res.output
    assert not (root / "Arsip").exists()


def test_cli_releases_lock_after_command(root):
    res = runner.invoke(app, ["undo", str(root), "--all"])
    assert LOCKED not in res.output
    acquire_root_lock(root).release()
