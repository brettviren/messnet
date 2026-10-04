"""The long running node: spool ingest, state materialization, links."""

import asyncio
import logging

from messnet import state
from messnet.links import check_spec, keep_link, serve_tcp, stdio_streams
from messnet.node import Node
from messnet.replicate import Session

log = logging.getLogger(__name__)


def maintain_once(node: Node) -> tuple[int, int]:
    """Ingest the spool and update state files. Return (new events, state files written)."""
    return node.ingest(), state.update(node.store, node.cfg.state)


async def maintain(node: Node) -> None:
    while True:
        try:
            maintain_once(node)
        except Exception as err:
            log.warning("maintenance failed: %s", err)
        await asyncio.sleep(node.cfg.poll)


def listen_specs(listen: list) -> list[dict]:
    """Normalize ``listen`` entries: "HOST:PORT" or {addr=..., scope=...}."""
    return [{"addr": x} if isinstance(x, str) else dict(x) for x in listen]


async def run(node: Node) -> None:
    """Run maintenance, configured listeners and links until cancelled."""
    specs = [check_spec(dict(s)) for s in node.cfg.links]
    servers = [await serve_tcp(node, ls["addr"], ls.get("scope", "all"))
               for ls in listen_specs(node.cfg.listen)]
    tasks = [asyncio.create_task(maintain(node))]
    tasks += [asyncio.create_task(keep_link(node, spec)) for spec in specs]
    try:
        await asyncio.gather(*tasks)
    finally:
        for server in servers:
            server.close()


async def run_stdio(node: Node, scope: str = "all", maintained: bool = True) -> None:
    """Serve one session on stdin/stdout (the remote end of an ssh link)."""
    reader, writer = await stdio_streams()
    keeper = asyncio.create_task(maintain(node)) if maintained else None
    try:
        await Session(node, reader, writer, scope).run()
    finally:
        if keeper:
            keeper.cancel()
