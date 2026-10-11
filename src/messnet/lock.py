"""One node service per store.

``messnet serve`` / ``run`` take an exclusive ``flock`` on ``<db>.serve.lock``.  A second
instance finds the lock held and does nothing, which is what lets cron or systemd start
the service as often as they like.  The kernel drops the lock when the holder dies, so
there is never a stale lock to clean up after a crash or a reboot.
"""

import fcntl
import os
import signal
import time
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


def holder_pid(path: Path) -> int | None:
    """Pid of the process holding the flock on PATH, from /proc/locks; 0 if none holds it.

    None if the kernel's lock table cannot be read or does not name the holder (another
    pid namespace), in which case callers fall back on the pid the holder wrote.
    """
    try:
        inode = os.stat(path).st_ino
        table = Path("/proc/locks").read_text().splitlines()
    except OSError:
        return None
    unknown = False
    for line in table:
        f = line.split()      # "1: FLOCK  ADVISORY  WRITE 4242 08:01:131 0 EOF"; waiters have "->"
        if len(f) < 6 or f[1] != "FLOCK" or "->" in f:
            continue
        try:
            if int(f[5].split(":")[2]) != inode:
                continue
            pid = int(f[4])
        except (ValueError, IndexError):
            continue
        if pid > 0 and Path(f"/proc/{pid}").exists():
            return pid
        unknown = True
    return None if unknown else 0


def running_pid(path: Path) -> int | None:
    """Pid of the instance holding the lock at PATH, or None if none seems to be running.

    Asks the kernel who holds the lock; if it cannot tell, trusts the pid the holder wrote
    (checking that such a process exists and is a messnet).  Never probes the lock itself,
    so asking can never make a starting instance think another one is running.
    """
    held = holder_pid(path)
    if held is not None:
        return held or None
    try:
        pid = int(Path(path).read_text().split()[0])
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError, IndexError):
        return None
    return pid if b"messnet" in cmdline else None


def _gone(pid: int) -> bool:
    """Has PID exited (a zombie, waiting to be reaped, counts)?"""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return True
    return state in ("Z", "X")


def stop(path: Path, grace: float = 15.0) -> int | None:
    """Stop the instance holding the lock at PATH: SIGTERM, and SIGKILL if it ignores that
    for GRACE seconds.  Return its pid, or None if none was running."""
    pid = running_pid(path)
    if pid is None:
        return None
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 5.0)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return pid
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if _gone(pid):
                return pid
            time.sleep(0.1)
    return pid
