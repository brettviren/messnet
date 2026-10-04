"""A messnet node: composes config, spool and store into emit / ingest / follow."""

import asyncio
import logging
import time
from collections.abc import Iterator
from pathlib import Path

from messnet import keys
from messnet.config import Config
from messnet.event import EventError, make_event
from messnet.spool import Spool, read_from
from messnet.store import Store

log = logging.getLogger(__name__)

# Envelope attributes a producer may supply when emitting from JSON input.
_SETTABLE = ("data", "subject", "scope", "source", "time", "id")


class Node:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.name = cfg.node
        # Only "all" events go in the (possibly Syncthing shared) spool.
        self.spools = {"all": Spool(cfg.spool),
                       "lan": Spool(cfg.local_spool / "lan"),
                       "host": Spool(cfg.local_spool / "host")}
        self.store = Store(cfg.db)
        self.sessions: dict[str, int] = {}   # peer name -> number of live sessions
        self.profile_scope = "lan"            # widest link scope allowed; set by the link manager
        self.iroh = None                      # iroh transport, opened on first use
        self.iroh_lock = asyncio.Lock()

    def linked(self, peer: str) -> bool:
        return self.sessions.get(peer, 0) > 0

    def close(self) -> None:
        self.store.close()

    def emit(self, type: str, data=None, scope: str = "all", **attrs) -> dict:
        """Create a new event originating at this node, spool and store it."""
        if scope not in self.spools:
            raise EventError(f"scope must be one of {tuple(self.spools)}: {scope!r}")
        spool = self.spools[scope]
        with self.store.transaction():
            seq = max(self.store.max_seq(self.name, scope), spool.last_seq(self.name)) + 1
            ev = make_event(type, data, origin=self.name, seq=seq, scope=scope, **attrs)
            spool.append(ev)
            self.store.insert(ev)
        return ev

    def emit_object(self, obj: dict) -> dict:
        """Emit from a partial event object holding at least "type"."""
        if not isinstance(obj, dict) or not obj.get("type"):
            raise EventError("input object needs a non-empty \"type\"")
        attrs = {k: obj[k] for k in _SETTABLE if k in obj}
        return self.emit(obj["type"], **attrs)

    def ingest(self) -> int:
        """Scan spool files for new complete lines; store new events. Return count new."""
        count = 0
        for scope, path in ((sc, p) for sc, sp in self.spools.items() for p in sp.files()):
            offset, size = self.store.file_offset(path), path.stat().st_size
            if size == offset:
                continue
            if size < offset:
                log.warning("spool file shrank, rescanning: %s", path)
                offset = 0
            with self.store.transaction():
                for ev, offset in read_from(path, offset):
                    if ev["origin"] != path.parent.name or ev.get("scope", "all") != scope:
                        log.warning("event %s/%s misplaced in %s", ev["origin"], ev.get("scope"), path)
                        continue
                    count += self.store.insert(ev)
                self.store.set_file_offset(path, offset)
        return count

    @property
    def seed(self) -> bytes:
        """The node's secret key seed (created on first use)."""
        if getattr(self, "_seed", None) is None:
            self._seed = keys.load_seed(self.cfg.key)
        return self._seed

    def prune(self, before: float, spool: bool = False) -> tuple[int, int]:
        """Prune stored events older than BEFORE; with SPOOL also delete this node's
        spool files whose events are all pruned.  Return (events, files) deleted."""
        with self.store.transaction():
            events = self.store.prune(before)
        files = 0
        if spool:
            for scope, sp in self.spools.items():
                floor = self.store.floor(self.name, scope)
                for path in sp.origin_files(self.name):
                    if floor and all(ev["seq"] <= floor for ev, _ in read_from(path, 0)):
                        path.unlink()
                        self.store.forget_file(path)
                        files += 1
        return events, files

    def rebuild(self) -> int:
        """Move the store aside and rebuild it from the spools. Return events ingested.

        Events this node only received over live links are dropped; peers
        resend them on the next link since the version vector shrinks.
        """
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self.cfg.db}{suffix}")
            if path.exists():
                path.replace(f"{path}.bak")
        self.store = Store(self.cfg.db)
        return self.ingest()

    def receive(self, ev: dict) -> bool:
        """Store an event received from a peer; True if it was new."""
        return self.store.insert(ev)

    def follow(self, types=None, origin=None, since=None, after: int = 0,
               forever: bool = False) -> Iterator[tuple[int, dict]]:
        """Yield matching (rowid, event); with FOREVER keep polling for new ones."""
        self.ingest()
        while True:
            for rowid, ev in self.store.query(types, origin, since, after):
                after = rowid
                yield rowid, ev
            if not forever:
                return
            time.sleep(self.cfg.poll)
            self.ingest()
