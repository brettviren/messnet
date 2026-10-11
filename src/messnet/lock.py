"""One node service per store.

``messnet serve`` / ``run`` take an exclusive ``flock`` on ``<db>.serve.lock``.  A second
instance finds the lock held and does nothing, which is what lets cron or systemd start
the service as often as they like.  The kernel drops the lock when the holder dies, so
there is never a stale lock to clean up after a crash or a reboot.
"""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def lock_path(db: Path) -> Path:
    """The lock file that goes with the store DB."""
    return Path(f"{db}.serve.lock")


@contextmanager
def single_instance(path: Path) -> Iterator[bool]:
    """Try to become the one instance: yield True holding the lock, or False if it is held."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())     # informational, for running_pid()
        try:
            yield True
        finally:
            os.ftruncate(fd, 0)
    finally:
        os.close(fd)                                   # also releases the lock


def running_pid(path: Path) -> int | None:
    """Pid of the instance holding the lock at PATH, or None if none seems to be running.

    Reads the pid the holder wrote instead of probing the lock, so asking can never
    make a starting instance think another one is running.
    """
    try:
        pid = int(Path(path).read_text().split()[0])
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError, IndexError):
        return None
    return pid if b"messnet" in cmdline else None
