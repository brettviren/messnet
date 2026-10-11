"""The concentrator: local delivery of every event the node receives, to pure consumers.

A client that wants to follow the messnet network should not have to speak the replication
protocol, authenticate to peers or find links.  It connects to its own node, which has already
done all that, and is sent every event that reaches the node's store: its own, those read from
the spools and those received over links.  Two mechanisms:

* ``publish`` entries in the configuration make ``messnet serve`` listen for consumers on a
  unix socket (by default) or a TCP address.  The traffic is one way: the consumer says what it
  wants once, then only the node talks.  Frames are JSON lines, like the replication protocol::

      consumer -> node   {"op":"subscribe","types":["backup.*"],"origin":"hokum.bv","replay":20}
      node -> consumer   {"op":"hello","proto":1,"node":"hokum.bv","scope":"host"}
                         {"op":"event","event":{...}}     (replayed events first, then live)
                         {"op":"ping"}                     (every 30 s, so a dead consumer is noticed)

  Every field of ``subscribe`` is optional, and the line itself may be left out.

* ``latest`` names a file that is overwritten, atomically, with the newest event to arrive.

Each consumer is served from the store by its own cursor, as a replication session is, so a
slow consumer costs nothing but its own place in line: nothing queues in memory.  A consumer's
*scope* limits what it is sent, as a link's scope does: ``all`` consumers see only ``all`` events,
``lan`` ones also ``lan`` events, ``host`` ones everything.  A unix socket (mode 0600, so only
you) defaults to ``host``; a TCP consumer, who may be anyone who can reach the port, to ``all``.
"""

import asyncio
import contextlib
import fnmatch
import json
import logging
import os
import tempfile
from pathlib import Path

from messnet.links import parse_addr
from messnet.node import Node

log = logging.getLogger(__name__)

PROTO = 1
SCOPES = ("all", "lan", "host")          # narrowest first
VISIBLE = {"all": ("all",), "lan": ("lan", "all"), "host": ("host", "lan", "all")}
PING = 30.0
SUBSCRIBE_WAIT = 2.0


def default_socket() -> Path:
    """Where the unix socket goes unless a ``publish`` entry says otherwise.

    Deliberately not under $XDG_RUNTIME_DIR: cron does not set it, so a daemon started by cron
    and a client run from a shell would look in different places.  The cache directory is the
    same for both (and is where the log and the ``latest`` file live too).
    """
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "messnet" / "events.sock"


def check_publish(spec: dict) -> dict:
    """Validate and complete a ``publish`` entry (raises ValueError)."""
    kind = spec.get("kind")
    if kind == "unix":
        out = {"kind": "unix", "path": str(Path(spec.get("path") or default_socket()).expanduser())}
    elif kind == "tcp":
        if "addr" not in spec:
            raise ValueError(f"a tcp publish entry needs 'addr': {spec}")
        parse_addr(spec["addr"])
        out = {"kind": "tcp", "addr": spec["addr"]}
    else:
        raise ValueError(f"publish kind must be 'unix' or 'tcp': {spec}")
    out["scope"] = spec.get("scope", "host" if kind == "unix" else "all")
    if out["scope"] not in SCOPES:
        raise ValueError(f"publish scope must be one of {SCOPES}: {spec}")
    return out


def label(spec: dict) -> str:
    return f"{spec['kind']}:{spec.get('path') or spec.get('addr')} ({spec['scope']} scope)"


async def _frame(writer: asyncio.StreamWriter, msg: dict) -> None:
    writer.write((json.dumps(msg, separators=(",", ":"), ensure_ascii=False) + "\n").encode())
    await writer.drain()


async def _subscription(reader: asyncio.StreamReader) -> dict:
    """The consumer's subscribe line, or {} if it sends none in time."""
    try:
        line = await asyncio.wait_for(reader.readline(), SUBSCRIBE_WAIT)
    except asyncio.TimeoutError:
        return {}
    if not line.strip():
        return {}
    msg = json.loads(line)
    if not isinstance(msg, dict) or msg.get("op") != "subscribe":
        raise ValueError(f"expected a subscribe frame, got {line[:60]!r}")
    return msg


async def serve_consumer(node: Node, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                         scope: str, name: str = "") -> None:
    """Send one consumer the events it asked for, replayed and then live, until it goes away."""
    store, scopes = node.store, VISIBLE[scope]
    try:
        try:
            sub = await _subscription(reader)
            types = [str(t) for t in sub.get("types") or []]
            origin, replay = sub.get("origin"), int(sub.get("replay") or 0)
        except (ValueError, TypeError) as err:
            with contextlib.suppress(Exception):
                await _frame(writer, {"op": "error", "reason": str(err)})
            return
        cursor = store.max_rowid()
        await _frame(writer, {"op": "hello", "proto": PROTO, "node": node.name, "scope": scope})
        if replay > 0:
            for _, ev in store.tail(replay, types, origin, upto=cursor, scopes=scopes):
                await _frame(writer, {"op": "event", "event": ev})
        loop = asyncio.get_running_loop()
        pinged = loop.time()
        while not reader.at_eof():
            top = store.max_rowid()
            last = cursor
            for rowid, ev in store.query(types, origin, after=cursor, scopes=scopes):
                await _frame(writer, {"op": "event", "event": ev})
                last = rowid
            cursor = max(top, last)       # every row up to here was looked at, matching or not
            if loop.time() - pinged >= PING:
                await _frame(writer, {"op": "ping"})
                pinged = loop.time()
            await asyncio.sleep(node.cfg.poll)
    except (ConnectionError, asyncio.IncompleteReadError, BrokenPipeError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()
        log.debug("consumer %s left", name)


def matches(ev: dict, types: list[str], origin: str | None = None) -> bool:
    """Does EV match a consumer's filters (what the store does for a live subscription)?"""
    return (not types or any(fnmatch.fnmatchcase(ev["type"], t) for t in types)) and (not origin or ev["origin"] == origin)


async def _clear_stale_socket(path: Path) -> None:
    """Remove PATH if it is a socket nobody listens on; refuse if one does, or it is not a socket."""
    if not path.exists() and not path.is_symlink():
        return
    if not path.is_socket():
        raise OSError(f"{path} exists and is not a socket")
    try:
        _, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(path)), 2.0)
    except (ConnectionError, FileNotFoundError, asyncio.TimeoutError):
        path.unlink(missing_ok=True)
        return
    writer.close()
    raise OSError(f"{path} is in use by another process")


async def serve(node: Node, spec: dict) -> asyncio.Server:
    """Start listening for consumers as SPEC (from ``check_publish``) says.

    The server has ``sessions`` (the consumers' tasks) and ``cleanup()``, for whoever stops it.
    """
    sessions: set[asyncio.Task] = set()
    scope = spec["scope"]

    async def handle(reader, writer):
        task = asyncio.current_task()
        sessions.add(task)
        try:
            await serve_consumer(node, reader, writer, scope, label(spec))
        except Exception as err:
            log.warning("consumer on %s failed: %s", label(spec), err)
        finally:
            sessions.discard(task)

    if spec["kind"] == "unix":
        path = Path(spec["path"])
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        await _clear_stale_socket(path)
        old = os.umask(0o177)                     # the socket is born private
        try:
            server = await asyncio.start_unix_server(handle, path=str(path))
        finally:
            os.umask(old)
        server.cleanup = lambda: path.unlink(missing_ok=True)
    else:
        host, port = parse_addr(spec["addr"])
        if host not in ("127.0.0.1", "::1", "localhost"):
            log.warning("event publisher on %s: anyone who can reach it can read %s-scope events",
                        spec["addr"], scope)
        server = await asyncio.start_server(handle, host, port)
        server.cleanup = lambda: None
    server.sessions = sessions
    log.info("publishing events to consumers on %s", label(spec))
    return server


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as fp:
        fp.write(text)
    os.replace(tmp, path)


async def write_latest(node: Node, path: Path) -> None:
    """Keep PATH holding the newest event to reach the store, until cancelled."""
    store = node.store
    cursor = store.max_rowid()
    if not Path(path).exists() and cursor:
        _write_atomically(Path(path), json.dumps(store.tail(1)[0][1], ensure_ascii=False) + "\n")
    while True:
        top = store.max_rowid()
        if top > cursor:
            _write_atomically(Path(path), json.dumps(store.tail(1)[0][1], ensure_ascii=False) + "\n")
            cursor = top
        await asyncio.sleep(node.cfg.poll)
