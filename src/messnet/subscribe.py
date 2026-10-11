"""A pure consumer of the node's event stream (the client side of ``publish``)."""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from messnet import publish
from messnet.links import parse_addr


class SubscribeError(Exception):
    pass


def parse_target(text: str) -> tuple[str, str]:
    """('unix', PATH) for ``unix:PATH`` or anything with a slash; else ('tcp', 'HOST:PORT')."""
    if text.startswith("unix:"):
        return "unix", text[5:]
    if "/" in text:
        return "unix", text
    parse_addr(text)
    return "tcp", text


def default_target(cfg) -> str:
    """Where this node's concentrator listens, from its ``publish`` setting."""
    specs = [publish.check_publish(dict(s)) for s in cfg.publish]
    for spec in sorted(specs, key=lambda s: s["kind"] != "unix"):      # prefer the unix socket
        return f"unix:{spec['path']}" if spec["kind"] == "unix" else spec["addr"]
    raise SubscribeError("this node publishes nowhere (the 'publish' setting is empty)")


async def open_target(target: str, timeout: float = 5.0):
    kind, where = parse_target(target)
    try:
        if kind == "unix":
            return await asyncio.wait_for(asyncio.open_unix_connection(where, limit=2**24), timeout)
        host, port = parse_addr(where)
        return await asyncio.wait_for(asyncio.open_connection(host, port, limit=2**24), timeout)
    except (OSError, asyncio.TimeoutError) as err:
        raise SubscribeError(f"cannot connect to {target}: {err or 'timed out'}"
                             " (is 'messnet serve' running, with this publish setting?)") from err


async def subscribe(target: str, types=(), origin: str | None = None, replay: int = 0,
                    timeout: float = 5.0, on_hello: Callable[[dict], None] | None = None) -> AsyncIterator[dict]:
    """Yield events from the node's concentrator at TARGET, replayed first (the last REPLAY
    matching) and then live, until it goes away.  ON_HELLO is called with the node's greeting
    once the connection is established."""
    reader, writer = await open_target(target, timeout)
    try:
        want = {"op": "subscribe", "types": list(types), "replay": replay}
        if origin:
            want["origin"] = origin
        writer.write((json.dumps(want) + "\n").encode())
        await writer.drain()
        while line := await reader.readline():
            msg = json.loads(line)
            if msg.get("op") == "hello" and on_hello:
                on_hello(msg)
            elif msg.get("op") == "event":
                yield msg["event"]
            elif msg.get("op") == "error":
                raise SubscribeError(msg.get("reason", "refused"))
    finally:
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()


def read_latest(path: Path) -> dict | None:
    """The newest event in the ``latest`` file, or None if there is none (yet)."""
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
