"""One ArsipBersih process per collection: an OS-level lock on <root>/.arsipbersih/lock.

The lock is held by an open file handle, so the operating system drops it when the process
dies; a stale lock file on disk never blocks anyone. Only the lock file inside the state
directory is ever opened for writing; media files are never touched here.

Both mechanisms refuse a second lock from the same process too (msvcrt locks are per handle,
flock is per open file description), so the web root switch and tests behave like two
processes would.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .layout import STATE_DIR

LOCK_NAME = "lock"

if sys.platform == "win32":
    import msvcrt

    # Lock one byte far past the PID text: Windows byte-range locks are mandatory, so
    # locking byte 0 would make the PID unreadable for anyone looking at the file.
    _LOCK_OFFSET = 1 << 30

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class RootLocked(Exception):
    """Another ArsipBersih process (or handle) holds the lock for this collection."""


class RootLock:
    def __init__(self, root: Path, fd: int) -> None:
        self.root = root
        self._fd: int | None = fd

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        except OSError:
            pass  # closing the handle releases it anyway
        finally:
            os.close(fd)

    def __enter__(self) -> RootLock:
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def lock_path(root: Path) -> Path:
    return Path(root) / STATE_DIR / LOCK_NAME


def acquire_root_lock(root: Path) -> RootLock:
    """Take the collection lock without waiting; raise RootLocked if someone else has it."""
    path = lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    if not _try_lock(fd):
        os.close(fd)
        raise RootLocked(str(root))
    try:  # PID for diagnostics only; the OS lock is what counts
        pid = f"{os.getpid()}\n".encode()
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, pid)
        os.ftruncate(fd, len(pid))
    except OSError:
        pass
    return RootLock(Path(root), fd)
