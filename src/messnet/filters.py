"""Parsing of user supplied filter values."""

import re
import time
from datetime import datetime

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
_REL = re.compile(r"^(\d+(?:\.\d+)?)([smhdw])$")


def parse_since(text: str | None, now: float | None = None) -> float | None:
    """Return an epoch time from a relative age ("90s", "2h", "1d", "1w") or ISO date/time."""
    if not text:
        return None
    m = _REL.match(text.strip())
    if m:
        return (now if now is not None else time.time()) - float(m[1]) * _UNITS[m[2]]
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"cannot parse time {text!r}: use e.g. 30m, 2h, 1d or an ISO date") from None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()
