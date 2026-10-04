"""Append-only JSONL spool: ``<root>/<origin>/<YYYY-MM-DD>.jsonl``.

A node appends only to its own origin directory so the spool can be
shared by Syncthing without write conflicts.  Readers consume only
complete (newline terminated) lines so partially synced files are safe.
"""

import json
import logging
import re
from collections.abc import Iterator
from pathlib import Path

from messnet.event import EventError, dumps, validate

log = logging.getLogger(__name__)

_LOGNAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.jsonl$")


class Spool:
    def __init__(self, root: Path):
        self.root = Path(root)

    def origin_dir(self, origin: str) -> Path:
        if not origin or "/" in origin or origin.startswith("."):
            raise EventError(f"invalid origin name for spool: {origin!r}")
        return self.root / origin

    def append(self, ev: dict) -> Path:
        """Append one event to its origin's log file for the event's date."""
        odir = self.origin_dir(ev["origin"])
        odir.mkdir(parents=True, exist_ok=True)
        path = odir / f"{ev['time'][:10]}.jsonl"
        with open(path, "a", encoding="utf-8") as fp:
            fp.write(dumps(ev) + "\n")
        return path

    def origin_files(self, origin: str) -> list[Path]:
        odir = self.origin_dir(origin)
        if not odir.is_dir():
            return []
        return sorted(p for p in odir.iterdir() if _LOGNAME.match(p.name))

    def files(self) -> list[Path]:
        """All log files of all origins (Syncthing temp and conflict files excluded)."""
        if not self.root.is_dir():
            return []
        out = []
        for odir in sorted(self.root.iterdir()):
            if odir.is_dir() and not odir.name.startswith("."):
                out.extend(self.origin_files(odir.name))
        return out

    def last_seq(self, origin: str) -> int:
        """Highest seq found in the last complete line of ORIGIN's newest non-empty log."""
        for path in reversed(self.origin_files(origin)):
            line = _last_line(path)
            if line:
                try:
                    return int(json.loads(line)["seq"])
                except (ValueError, KeyError, TypeError):
                    log.warning("unparsable last line in %s", path)
                    return max((ev["seq"] for ev, _ in read_from(path, 0)), default=0)
        return 0


def _last_line(path: Path, chunk: int = 8192) -> bytes:
    """Return the last complete line of PATH (without newline) or b''."""
    with open(path, "rb") as fp:
        fp.seek(0, 2)
        size = fp.tell()
        pos, buf = size, b""
        while pos > 0:
            step = min(chunk, pos)
            pos -= step
            fp.seek(pos)
            buf = fp.read(step) + buf
            end = buf.rfind(b"\n")
            if end < 0:
                continue
            start = buf.rfind(b"\n", 0, end)
            if start >= 0 or pos == 0:
                return buf[start + 1:end]
    return b""


def read_from(path: Path, offset: int) -> Iterator[tuple[dict, int]]:
    """Yield (event, offset-after-line) for each complete valid line after OFFSET."""
    with open(path, "rb") as fp:
        fp.seek(offset)
        for line in fp:
            if not line.endswith(b"\n"):
                return
            offset += len(line)
            text = line.strip()
            if not text:
                continue
            try:
                yield validate(json.loads(text)), offset
            except (json.JSONDecodeError, UnicodeDecodeError, EventError) as err:
                log.warning("skipping bad line in %s: %s", path, err)
