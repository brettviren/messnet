"""Replication protocol v1: newline-delimited JSON frames over any byte stream.

1. both sides send ``{"op":"hello","proto":1,"node":N,"vv":{stream: seq}}``
2. each sends the events the peer lacks per the peer's version vector
3. then each live-pushes newly stored events (found by polling the store)
4. ``{"op":"ping"}`` keeps idle links alive and detects dead ones

A stream is an (origin, scope) pair.  A link has a scope: ``lan`` links
carry ``lan`` and ``all`` events, ``all`` (wide area) links carry only
``all`` events.  ``host`` events never leave a node.
"""

import asyncio
import contextlib
import json
import logging

from messnet.event import EventError, validate
from messnet.node import Node
from messnet.store import stream_key

log = logging.getLogger(__name__)

PROTO = 1
LINK_SCOPES = ("lan", "all")
LINK_EVENT = "messnet.link.v1"


class ProtocolError(Exception):
    pass


def allowed(scope: str, link_scope: str) -> bool:
    """May an event of SCOPE travel over a link of LINK_SCOPE?"""
    return scope == "all" or (scope == "lan" and link_scope == "lan")


class Session:
    """One replication session with a peer over a (reader, writer) stream pair."""

    def __init__(self, node: Node, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 link_scope: str = "all", keepalive: float = 30.0, timeout: float = 90.0,
                 expect: str | None = None, label: str = "", report: bool = True):
        if link_scope not in LINK_SCOPES:
            raise ValueError(f"link scope must be one of {LINK_SCOPES}: {link_scope!r}")
        self.node, self.reader, self.writer = node, reader, writer
        self.link_scope, self.keepalive, self.timeout = link_scope, keepalive, timeout
        self.expect, self.label, self.report = expect, label, report
        self.peer: str | None = None
        self.peer_vv: dict[str, int] = {}
        self.seen: set[tuple[str, int]] = set()   # (stream, seq) the peer has via this session
        self.sent = self.received = 0

    def _peer_has(self, ev: dict) -> bool:
        key = stream_key(ev["origin"], ev["scope"])
        return ev["seq"] <= self.peer_vv.get(key, 0) or (key, ev["seq"]) in self.seen

    def _mark(self, ev: dict) -> None:
        self.seen.add((stream_key(ev["origin"], ev["scope"]), ev["seq"]))

    async def _send(self, msg: dict) -> None:
        self.writer.write((json.dumps(msg, separators=(",", ":"), ensure_ascii=False) + "\n").encode())
        await self.writer.drain()

    async def _recv(self) -> dict:
        line = await asyncio.wait_for(self.reader.readline(), self.timeout)
        if not line:
            raise EOFError("peer closed the link")
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as err:
            raise ProtocolError(f"bad frame: {err}") from err
        if not isinstance(msg, dict) or "op" not in msg:
            raise ProtocolError(f"bad frame: {line[:80]!r}")
        return msg

    async def _handshake(self) -> None:
        await self._send({"op": "hello", "proto": PROTO, "node": self.node.name,
                          "vv": self.node.store.version_vector()})
        msg = await self._recv()
        if msg["op"] != "hello" or msg.get("proto") != PROTO:
            raise ProtocolError(f"expected hello proto {PROTO}, got {msg}")
        self.peer = str(msg["node"])
        if self.expect and self.peer != self.expect:
            raise ProtocolError(f"expected peer {self.expect}, reached {self.peer}")
        if self.peer == self.node.name:
            raise ProtocolError("linked to self")
        self.peer_vv = {str(k): int(v) for k, v in msg.get("vv", {}).items()}
        log.info("linked with %s (%s scope)", self.peer, self.link_scope)

    async def _push(self, ev: dict) -> None:
        if allowed(ev["scope"], self.link_scope) and not self._peer_has(ev):
            await self._send({"op": "event", "event": ev})
            self._mark(ev)
            self.sent += 1

    async def _sender(self) -> None:
        store = self.node.store
        cursor = store.max_rowid()
        for origin, scope in store.streams():
            if allowed(scope, self.link_scope):
                start = self.peer_vv.get(stream_key(origin, scope), 0)
                for ev in store.stream_after(origin, scope, start, cursor):
                    await self._push(ev)
        while True:
            for rowid, ev in store.query(after=cursor):
                cursor = rowid
                await self._push(ev)
            await asyncio.sleep(self.node.cfg.poll)

    async def _receiver(self) -> None:
        while True:
            msg = await self._recv()
            if msg["op"] == "event":
                self._accept(msg.get("event"))
            elif msg["op"] == "bye":
                return
            elif msg["op"] != "ping":
                log.debug("ignoring unknown op from %s: %s", self.peer, msg["op"])

    def _accept(self, ev) -> None:
        try:
            validate(ev)
        except EventError as err:
            log.warning("dropping invalid event from %s: %s", self.peer, err)
            return
        ev.setdefault("scope", "all")
        if not allowed(ev["scope"], self.link_scope):
            log.warning("dropping %s scope event from %s on %s link",
                        ev["scope"], self.peer, self.link_scope)
            return
        self._mark(ev)
        self.received += self.node.receive(ev)

    async def _pinger(self) -> None:
        while True:
            await asyncio.sleep(self.keepalive)
            await self._send({"op": "ping"})

    def _status(self, state: str, error: str | None = None) -> None:
        if not self.report or not self.peer:
            return
        data = {"state": state, "link": self.label, "scope": self.link_scope}
        if state == "down":
            data.update(sent=self.sent, received=self.received)
        if error:
            data["error"] = error
        try:
            self.node.emit(LINK_EVENT, data, subject=self.peer, scope="host",
                           source=f"messnet://{self.node.name}/link")
        except Exception as err:      # status reporting must never break a link
            log.debug("link status event failed: %s", err)

    async def run(self) -> None:
        """Run until the link fails or the peer leaves; always closes the writer."""
        registered, error = False, None
        try:
            await self._handshake()
            self.node.sessions[self.peer] = self.node.sessions.get(self.peer, 0) + 1
            registered = True
            self._status("up")
            tasks = [asyncio.create_task(c()) for c in (self._sender, self._receiver, self._pinger)]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            for task in done:
                err = task.exception()
                if err and not isinstance(err, EOFError):
                    raise err
        except Exception as err:
            error = str(err) or type(err).__name__
            raise
        finally:
            if registered:
                self.node.sessions[self.peer] -= 1
                self._status("down", error)
            log.info("unlinked from %s: sent %d, received %d new",
                     self.peer, self.sent, self.received)
            with contextlib.suppress(Exception):
                self.writer.close()
                await self.writer.wait_closed()
