"""Where the 'latest event' comes from, shared by the terminal and web UIs.

``follow`` takes live events from the node's concentrator (``subscribe``); when that is not there
it watches the ``latest`` file; with neither it reports that only the state files are available.
``LatestFeed`` runs it in a background thread and answers "what is the latest event?" for a UI that
polls (the web one).
"""

import asyncio
import threading
from collections.abc import Callable
from pathlib import Path

from messnet import subscribe, tuimodel

PUBLISHER, FILE, STATE, STARTING = "publisher", "file", "state", "starting"

TITLES = {PUBLISHER: "Latest event · live",
          FILE: "Latest event · from the latest.json file",
          STATE: "Latest event · newest state file (no live feed: is 'messnet serve' running, and recent?)",
          STARTING: "Latest event"}


async def follow(target: str | None, latest_path: Path | None, arrived: Callable[[dict], None],
                 status: Callable[[str, str], None], seen: str | None = None) -> None:
    """Report each new event to ARRIVED, and where they come from to STATUS(text, feed), forever.

    SEEN is the id of the event the caller already shows."""
    while True:
        if target:
            try:
                async for ev in subscribe.subscribe(
                        target, replay=1, on_hello=lambda hello: status(f"connected to {target}", PUBLISHER)):
                    seen = ev.get("id")
                    arrived(ev)
            except subscribe.SubscribeError:
                pass
            except (OSError, ValueError):
                pass
        if latest_path and latest_path.exists():
            status(f"polling {latest_path.name} (no node publishing)", FILE)
            for _ in range(5):                  # then try the concentrator again
                ev = subscribe.read_latest(latest_path)
                if ev and ev.get("id") != seen:
                    seen = ev.get("id")
                    arrived(ev)
                await asyncio.sleep(1.0)
        else:
            status("none (following the state files only)", STATE)
            await asyncio.sleep(2.0)


class LatestFeed:
    """The most recent event, kept current by ``follow`` in a background thread (see ``start``)."""

    def __init__(self, state_root: Path, target: str | None = None, latest_path: Path | None = None):
        self.state_root, self.target, self.latest_path = Path(state_root), target, latest_path
        self.latest: dict | None = None
        self.feed = STARTING
        self.live = "starting"
        self._thread: threading.Thread | None = None

    def arrived(self, ev: dict) -> None:
        self.latest = ev

    def status(self, text: str, feed: str) -> None:
        self.live, self.feed = text, feed

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="latest-feed", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        asyncio.run(follow(self.target, self.latest_path, self.arrived, self.status))

    def get(self, events: list[dict]) -> dict | None:
        """The latest event; EVENTS (newest first) stand in until anything has been reported.
        With no live source the newest state file is the best guess."""
        if self.feed == STATE:
            return tuimodel.latest_arrival(self.state_root) or self.latest
        if self.latest is None and self.latest_path:
            self.latest = subscribe.read_latest(self.latest_path)
        return self.latest or tuimodel.newest(events)

    @property
    def title(self) -> str:
        return TITLES.get(self.feed, TITLES[STARTING])
