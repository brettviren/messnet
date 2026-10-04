"""Link manager: keep wanted peers linked using the current network's policy.

Every ``manage_interval`` seconds the manager re-reads the peer directory,
re-detects the network profile and re-plans.  A peer whose plan changed
has its link task restarted.  Each peer task walks its ordered link specs,
staying on the first that connects; if all fail it backs off.  A peer
that is already linked (e.g. it dialed us) is not dialed.
"""

import asyncio
import logging
from collections.abc import Callable

from messnet import netdetect, peers
from messnet.links import link_label, run_link
from messnet.node import Node

log = logging.getLogger(__name__)


def current_plan(node: Node, facts: dict) -> tuple[dict, dict[str, list[dict]]]:
    """Return (profile, {peer: [spec, ...]}) for the node's config, etc dir and FACTS."""
    cfg = node.cfg
    profile = netdetect.select_profile(peers.load_networks(cfg.etc), facts, cfg.network)
    known = peers.load_peers(cfg.etc)
    return profile, peers.plan(node.name, cfg.peers, known, profile, facts)


async def keep_peer(node: Node, peer: str, specs: list[dict], max_backoff: float = 300.0,
                    linked_wait: float = 5.0) -> None:
    """Keep PEER linked, trying SPECS in order; never returns unless cancelled."""
    delay = 1.0
    while True:
        if node.linked(peer):
            await asyncio.sleep(linked_wait)
            continue
        connected = False
        for spec in specs:
            if node.linked(peer):
                break
            loop = asyncio.get_running_loop()
            start = loop.time()
            try:
                await run_link(node, spec)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                log.info("link %s to %s failed: %s", link_label(spec), peer, err)
            if loop.time() - start > 10.0:   # it was a working link that dropped
                connected = True
                break
        if connected:
            delay = 1.0
            await asyncio.sleep(delay)
        else:
            await asyncio.sleep(delay)
            delay = min(max_backoff, delay * 2)


async def manage(node: Node, facts_fn: Callable[[], dict] = netdetect.gather_facts) -> None:
    """Run the link manager until cancelled."""
    tasks: dict[str, tuple[list[dict], asyncio.Task]] = {}
    last_profile = None
    try:
        while True:
            try:
                facts = await asyncio.to_thread(facts_fn)
                profile, wanted = current_plan(node, facts)
            except Exception as err:
                log.warning("link planning failed: %s", err)
                await asyncio.sleep(node.cfg.manage_interval)
                continue
            node.profile_scope = profile.get("scope", "all")
            if profile["name"] != last_profile:
                log.info("network profile: %s", profile["name"])
                last_profile = profile["name"]
            for name in list(tasks):
                if name not in wanted or tasks[name][0] != wanted[name]:
                    tasks.pop(name)[1].cancel()
            for name, specs in wanted.items():
                if name in tasks:
                    continue
                if not specs:
                    log.debug("no usable links to %s on %s", name, profile["name"])
                    continue
                tasks[name] = (specs, asyncio.create_task(keep_peer(node, name, specs)))
            await asyncio.sleep(node.cfg.manage_interval)
    finally:
        for _, task in tasks.values():
            task.cancel()
        await asyncio.gather(*(t for _, t in tasks.values()), return_exceptions=True)
