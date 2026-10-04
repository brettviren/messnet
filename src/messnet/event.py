"""Event envelope: CloudEvents 1.0 structured JSON plus messnet extensions.

messnet extension attributes: ``origin`` (node name), ``scope`` (``host``,
``lan`` or ``all``) and ``seq``.  Events are sequenced per (origin, scope)
*stream* so a link that may not carry some scope never sees seq gaps.
"""

import getpass
import json
import socket
from datetime import datetime

from messnet.ulid import ulid

SCOPES = ("host", "lan", "all")
REQUIRED = ("specversion", "id", "source", "type", "origin", "seq")


class EventError(ValueError):
    pass


def default_source(producer: str = "cli") -> str:
    host = socket.gethostname().split(".")[0]
    return f"messnet://{host}/{getpass.getuser()}/{producer}"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def make_event(type: str, data=None, *, origin: str, seq: int, subject: str | None = None,
               scope: str = "all", source: str | None = None, time: str | None = None,
               id: str | None = None) -> dict:
    """Return a complete, validated event dictionary."""
    ev = {
        "specversion": "1.0",
        "id": id or ulid(),
        "time": time or now_iso(),
        "source": source or default_source(),
        "type": type,
        "origin": origin,
        "seq": seq,
        "scope": scope,
    }
    if subject is not None:
        ev["subject"] = subject
    if data is not None:
        ev["datacontenttype"] = "application/json"
        ev["data"] = data
    return validate(ev)


def validate(ev: dict) -> dict:
    """Raise EventError if EV is not a well formed messnet event, else return it."""
    if not isinstance(ev, dict):
        raise EventError("event must be a JSON object")
    missing = [k for k in REQUIRED if k not in ev]
    if missing:
        raise EventError(f"event missing attributes: {', '.join(missing)}")
    if not isinstance(ev["seq"], int) or ev["seq"] < 1:
        raise EventError(f"event seq must be a positive integer: {ev['seq']!r}")
    if ev.get("scope", "all") not in SCOPES:
        raise EventError(f"event scope must be one of {SCOPES}: {ev.get('scope')!r}")
    if not ev["type"] or not ev["origin"]:
        raise EventError("event type and origin must be non-empty")
    return ev


def dumps(ev: dict) -> str:
    """Serialize an event as a single compact JSON line (no newline)."""
    return json.dumps(ev, separators=(",", ":"), ensure_ascii=False)


def parse_assignments(items: list[str]) -> dict:
    """Parse ``key=value`` (string) and ``key:=json`` (raw JSON) items into a dict."""
    data = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep:
            raise EventError(f"expected key=value or key:=json, got {item!r}")
        if key.endswith(":"):
            key = key[:-1]
            try:
                value = json.loads(value)
            except json.JSONDecodeError as err:
                raise EventError(f"bad JSON value for {key!r}: {err}") from err
        if not key:
            raise EventError(f"empty key in {item!r}")
        data[key] = value
    return data
