"""The long running node: spool ingest, state materialization, links."""

import asyncio
import logging

from messnet import keys, manager, peers, state
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


def publish_self(node: Node) -> bool:
    """Write this node's peer file from its ``advertise`` config. True if changed."""
    if not node.cfg.advertise:
        return False
    advertise = dict(node.cfg.advertise)
    if "iroh" in advertise:
        from messnet.iroh_link import advertise_ids
        eps = advertise["iroh"]
        advertise["iroh"] = advertise_ids(node, eps if isinstance(eps, list) else [eps])
    extra = {"id": keys.public_id(keys.load_seed(node.cfg.key))}
    return peers.publish_peer(node.cfg.etc, peers.peer_record(node.name, advertise, extra))


def wants_iroh_listener(node: Node) -> bool:
    return node.cfg.iroh.get("listen", "iroh" in node.cfg.advertise)


async def run(node: Node) -> None:
    """Run maintenance, listeners, explicit links and the link manager until cancelled."""
    specs = [check_spec(dict(s)) for s in node.cfg.links]
    publish_self(node)
    servers = [await serve_tcp(node, ls["addr"], ls.get("scope", "all"))
               for ls in listen_specs(node.cfg.listen)]
    tasks = [asyncio.create_task(maintain(node))]
    tasks += [asyncio.create_task(keep_link(node, spec)) for spec in specs]
    if node.cfg.peers:
        tasks.append(asyncio.create_task(manager.manage(node)))
    if node.cfg.gonc_serve:
        from messnet import gonc
        tasks += [asyncio.create_task(gonc.serve(node.cfg, gonc.check(dict(ep))))
                  for ep in node.cfg.gonc_serve]
    if wants_iroh_listener(node):
        from messnet import iroh_link
        tasks.append(asyncio.create_task((await iroh_link.transport(node)).serve()))
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for server in servers:
            server.close()
        if node.iroh:
            await node.iroh.close()


async def run_stdio(node: Node, scope: str = "all", maintained: bool = True) -> None:
    """Serve one session on stdin/stdout (the remote end of an ssh link)."""
    reader, writer = await stdio_streams()
    keeper = asyncio.create_task(maintain(node)) if maintained else None
    try:
        await Session(node, reader, writer, scope).run()
    finally:
        if keeper:
            keeper.cancel()
