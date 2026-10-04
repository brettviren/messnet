"""Materialized latest-event state files for fast, offline UI consumers.

Layout: ``<state>/<type>/<origin>/<subject>.json`` (subject ``_`` if none).
Each file holds the newest (highest seq) event for that key.
"""

import fnmatch
import json
import os
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

from messnet.store import Store

_CURSOR = "state_rowid"


def _component(text: str | None) -> str:
    if not text:
        return "_"
    safe = quote(text, safe="-_.,@+=")
    return "%2E" + safe[1:] if safe.startswith(".") else safe


def state_path(root: Path, ev: dict) -> Path:
    return (Path(root) / _component(ev["type"]) / _component(ev["origin"])
            / f"{_component(ev.get('subject'))}.json")


def _current_seq(path: Path) -> int:
    try:
        return json.loads(path.read_text())["seq"]
    except (OSError, ValueError, KeyError):
        return 0


def write_state(root: Path, ev: dict) -> bool:
    """Atomically write EV as the state for its key if newer. Return True if written."""
    path = state_path(root, ev)
    if _current_seq(path) >= ev["seq"]:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(ev, ensure_ascii=False) + "\n")
    tmp.replace(path)
    return True


def update(store: Store, root: Path, rebuild: bool = False) -> int:
    """Apply events stored since the last update. Return number of files written."""
    after = 0 if rebuild else int(store.get_meta(_CURSOR, "0"))
    written = 0
    for rowid, ev in store.query(after=after):
        written += write_state(root, ev)
        after = rowid
    store.set_meta(_CURSOR, str(after))
    return written


def read(root: Path, types: list[str] | None = None, origin: str | None = None) -> Iterator[dict]:
    """Yield state events whose type matches any of the TYPES globs."""
    root = Path(root)
    if not root.is_dir():
        return
    for path in sorted(root.glob("*/*/*.json")):
        ev = json.loads(path.read_text())
        if types and not any(fnmatch.fnmatchcase(ev["type"], t) for t in types):
            continue
        if origin and ev["origin"] != origin:
            continue
        yield ev
