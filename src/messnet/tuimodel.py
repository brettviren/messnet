"""What the terminal UI shows, computed from the state files: hosts, event types and their events.

Pure functions over a list of events (as ``state.read`` yields them), so the UI stays a thin
layer and this can be tested without a terminal.

* *Host view*: one entry per origin node; selecting it lists that host's events.
* *Event view*: one entry per event type; selecting it lists that type as received from every host.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from messnet import eventview

_SEVERITY = {eventview.FAIL: 3, eventview.WARN: 2, eventview.OK: 1, "": 0, eventview.DIM: 0}
# These describe connections, not the health of a host or a kind of event: a peer that is
# not linked right now is not a fault, and the records outlive the connection.
NEUTRAL = ("messnet.link.v1",)


@dataclass
class Group:
    """An entry of the left-hand list."""
    key: str
    count: int                       # events (host view) or hosts (event view)
    newest: datetime | None
    style: str                       # worst outcome among its events: fail > warn > ok


@dataclass
class Row:
    """A line of the events table."""
    key: str
    cells: list[str]
    style: str
    event: dict


def signature(root: Path) -> tuple:
    """A cheap fingerprint of the state directory: changes whenever a state file does."""
    sig = []
    try:
        for typ in os.scandir(root):
            if not typ.is_dir():
                continue
            for origin in os.scandir(typ.path):
                if not origin.is_dir():
                    continue
                for f in os.scandir(origin.path):
                    if f.name.endswith(".json"):
                        st = f.stat()
                        # state files are replaced by rename, so the inode changes with each write
                        sig.append((typ.name, origin.name, f.name, st.st_mtime_ns, st.st_size, st.st_ino))
    except OSError:
        pass
    return tuple(sorted(sig))


def load(root: Path) -> list[dict]:
    """Every event in the state directory (skipping files that cannot be read), newest first."""
    out = []
    for path in Path(root).glob("*/*/*.json"):
        try:
            ev = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(ev, dict) and ev.get("type") and ev.get("origin"):
            out.append(ev)
    out.sort(key=lambda e: (eventview.parse_time(e.get("time")) or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    return out


def event_key(ev: dict) -> str:
    return f"{ev['type']}\x1f{ev['origin']}\x1f{ev.get('subject') or ''}"


def _worst(events: list[dict]) -> str:
    worst = ""
    for ev in events:
        if ev["type"] in NEUTRAL:
            continue
        style = eventview.summary(ev).style
        if _SEVERITY.get(style, 0) > _SEVERITY.get(worst, 0):
            worst = style
    return worst


def _newest(events: list[dict]) -> datetime | None:
    times = [t for t in (eventview.parse_time(e.get("time")) for e in events) if t]
    return max(times) if times else None


def hosts(events: list[dict]) -> list[Group]:
    by: dict[str, list[dict]] = {}
    for ev in events:
        by.setdefault(ev["origin"], []).append(ev)
    return [Group(name, len(evs), _newest(evs), _worst(evs)) for name, evs in sorted(by.items())]


def types(events: list[dict]) -> list[Group]:
    by: dict[str, list[dict]] = {}
    for ev in events:
        by.setdefault(ev["type"], []).append(ev)
    return [Group(name, len({e["origin"] for e in evs}), _newest(evs), _worst(evs)) for name, evs in sorted(by.items())]


def rows(events: list[dict], view: str, key: str, now: datetime | None = None, width: int = 90) -> list[Row]:
    """The events of the host (VIEW 'hosts') or type (VIEW 'types') named KEY, newest first.

    A host's rows say which type each is; a type's rows say which host."""
    now = now or datetime.now(timezone.utc)
    field = "origin" if view == "hosts" else "type"
    out = []
    for ev in events:
        if ev[field] != key:
            continue
        s = eventview.summary(ev)
        who = ev["type"] if view == "hosts" else ev["origin"]
        out.append(Row(event_key(ev), [who, ev.get("subject") or "—",
                                       eventview.age(eventview.parse_time(ev.get("time")), now),
                                       eventview.clip(s.text, width)], s.style, ev))
    return out


def newest(events: list[dict]) -> dict | None:
    return events[0] if events else None        # load() sorts newest first
