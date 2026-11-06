"""Telling the web pages that something changed, so they can fetch again.

``Changes`` keeps a version number that moves whenever a *token* (a cheap fingerprint of what the
pages show) differs from the last one seen.  Streams ``wait`` for the version to move.  The token is
checked on demand and, while anything is waiting, by a thread every ``interval`` seconds, so
nothing runs when no page is open.
"""

import threading
import time
from collections.abc import Callable, Hashable


class Changes:
    def __init__(self, token: Callable[[], Hashable], interval: float = 1.0):
        self._token, self.interval = token, interval
        self._cond = threading.Condition()
        self._version = 0
        self._last: Hashable = None
        self._seen = False
        self._waiting = 0
        self._thread: threading.Thread | None = None

    def check(self) -> int:
        """Compare the token with the last one, bump the version if it moved; return the version."""
        token = self._token()
        with self._cond:
            if self._seen and token != self._last:
                self._version += 1
                self._cond.notify_all()
            self._last, self._seen = token, True
            return self._version

    def wait(self, version: int, timeout: float) -> int:
        """The version once it differs from VERSION, or VERSION if TIMEOUT seconds pass first.
        Call within ``watching()`` so the changes are looked for."""
        with self._cond:
            self._cond.wait_for(lambda: self._version != version, timeout)
            return self._version

    def watching(self) -> "Watching":
        return Watching(self)

    def _enter(self) -> None:
        with self._cond:
            self._waiting += 1
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="changes", daemon=True)
                self._thread.start()

    def _leave(self) -> None:
        with self._cond:
            self._waiting -= 1

    def _run(self) -> None:
        while True:
            with self._cond:
                if self._waiting == 0:
                    self._thread = None
                    return
            self.check()
            time.sleep(self.interval)


class Watching:
    """``with changes.watching():`` while some client waits."""

    def __init__(self, changes: Changes):
        self.changes = changes

    def __enter__(self) -> Changes:
        self.changes._enter()
        return self.changes

    def __exit__(self, *exc) -> None:
        self.changes._leave()
