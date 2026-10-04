"""The long running node: spool ingest, state materialization, links."""

import asyncio
import logging
from datetime import date, timedelta

from messnet import keys, manager, peers, state
from messnet.links import check_spec, keep_link, serve_tcp, stdio_streams
from messnet.node import Node
from messnet.replicate import Session

log = logging.getLogger(__name__)


FULL_SCAN = 60.0   # seconds between scans of all spool files (others scan recent files only)


def recent_date(days: int = 2) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def maintain_once(node: Node, full: bool = True) -> tuple[int, int]:
    """Ingest the spool and update state files. Return (new events, state files written)."""
    return node.ingest("" if full else recent_date()), state.update(node.store, node.cfg.state)


async def maintain(node: Node) -> None:
    loop = asyncio.get_running_loop()
    last_full = -FULL_SCAN
    while True:
        full = loop.time() - last_full >= FULL_SCAN
        try:
            maintain_once(node, full)
            if full:
                last_full = loop.time()
        except Exception as err:
            log.warning("maintenance failed: %s", err)
        await asyncio.sleep(node.cfg.poll)


def listen_specs(listen: list) -> list[dict]:
    """Normalize ``listen`` entries: "HOST:PORT" or {addr=..., scope=...}."""
    return [{"addr": x} if isinstance(x, str) else dict(x) for x in listen]


def publish_self(node: Node) -> bool:
    """Write this node's peer file (its id plus ``advertise`` endpoints). True if changed."""
    return publish_config(node.cfg)


def publish_config(cfg) -> bool:
    """Write the peer file for the node described by CFG. True if changed."""
    nid = keys.public_id(keys.load_seed(cfg.key))
    advertise = dict(cfg.advertise)
    if "iroh" in advertise:
        eps = advertise["iroh"]
        advertise["iroh"] = [{"id": nid, **{k: v for k, v in ep.items() if k != "id"}}
                             for ep in (eps if isinstance(eps, list) else [eps])]
    return peers.publish_peer(cfg.etc, peers.peer_record(cfg.node, advertise, {"id": nid}))


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
