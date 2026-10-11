"""Run something as a background daemon, and hear back whether it came up.

``start(body)`` forks twice (the daemon is then no session leader and has no controlling
terminal), points the daemon's standard streams at /dev/null, and runs ``body(report)`` in it.
The launching process waits for ``body`` to call ``report(...)`` -- once it is really running, or
has found it need not, or failed -- and returns what it said, so a command that starts a daemon
can tell its user (or cron) truthfully how that went.

Call it before the process has threads, sockets, a database or an event loop: nothing of that
survives a fork cleanly.  The working directory is kept: configuration paths may be relative.
"""

import contextlib
import json
import logging
import os
import select
import time
from collections.abc import Callable
from typing import NamedTuple

TIMEOUT = 30.0


class Report(NamedTuple):
    ok: bool
    state: str        # "started" | "running" (already) | "failed"
    message: str
    pid: int | None


def _hear(fd: int, timeout: float) -> Report:
    data, deadline = b"", time.monotonic() + timeout
    try:
        while b"\n" not in data:
            left = deadline - time.monotonic()
            if left <= 0:
                return Report(False, "failed", f"no word from the daemon after {timeout:.0f}s "
                                               "(is it still starting? see the log)", None)
            if select.select([fd], [], [], left)[0]:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                data += chunk
    finally:
        os.close(fd)
    if not data.strip():
        return Report(False, "failed", "the daemon exited before it reported; see the log", None)
    msg = json.loads(data.splitlines()[0])
    return Report(msg["ok"], msg["state"], msg["message"], msg["pid"])


def _detach_stdio() -> None:
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    if null > 2:
        os.close(null)


def _daemon(body: Callable[[Callable], None], wfd: int) -> int:
    told = False

    def report(ok: bool, state: str, message: str = "") -> None:
        nonlocal told
        if told:
            return
        told = True
        with contextlib.suppress(OSError):
            line = json.dumps({"ok": ok, "state": state, "message": message, "pid": os.getpid()})
            os.write(wfd, (line + "\n").encode())
            os.close(wfd)

    try:
        body(report)
        code = 0
    except SystemExit as err:
        code = err.code if isinstance(err.code, int) else 1
    except BaseException as err:
        report(False, "failed", getattr(err, "message", None) or f"{type(err).__name__}: {err}")
        logging.getLogger(__name__).exception("daemon crashed")
        code = 1
    report(False, "failed", "the daemon stopped before it finished starting")      # no-op if it reported
    return code


def start(body: Callable[[Callable], None], timeout: float = TIMEOUT) -> Report:
    """Run BODY(report) in a new daemon and return its first report (or why there is none)."""
    rfd, wfd = os.pipe()
    pid = os.fork()
    if pid:
        os.close(wfd)
        os.waitpid(pid, 0)                     # the first child leaves at once
        return _hear(rfd, timeout)
    code = 1
    try:
        os.close(rfd)
        os.setsid()
        if os.fork():
            os._exit(0)
        _detach_stdio()
        code = _daemon(body, wfd)
    finally:
        os._exit(code)
