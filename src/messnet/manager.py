"""Link manager: keep wanted peers linked using the current network's policy.

Every ``manage_interval`` seconds, or at once when ``node.replan`` is set (the service
does that when the peer directory or the configuration changes), the manager re-reads the
peer directory, re-detects the network profile and re-plans.  Each peer task walks its
ordered link specs, staying on the first that connects; if all fail it backs off.  A peer
that is already linked (e.g. it dialed us) is not dialed.

A changed plan is acted on only once it has come out the same on two passes in a row
(``settle`` seconds apart at the shortest), so a peer file caught half-written, or a
network that flaps, does not tear links down.  When it is acted on, a working link is
kept unless its own spec is gone from the plan; the new specs are used the next time
that peer has to be dialed.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

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


@dataclass
class PeerPlan:
    """The specs to try for one peer, and the one in use right now; the manager updates it."""
    specs: list[dict]
    active: dict | None = field(default=None)


async def keep_peer(node: Node, peer: str, specs: list[dict], max_backoff: float = 300.0,
                    linked_wait: float = 5.0, plan: PeerPlan | None = None) -> None:
    """Keep PEER linked, trying SPECS (or PLAN.specs, which may change) in order; never
    returns unless cancelled."""
    plan = plan or PeerPlan(specs)
    delay = 1.0
    while True:
        if node.linked(peer):
            await asyncio.sleep(linked_wait)
            continue
        connected = False
        for spec in list(plan.specs):
            if node.linked(peer):
                break
            loop = asyncio.get_running_loop()
            start = loop.time()
            plan.active = spec
            try:
                await run_link(node, spec)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                log.info("link %s to %s failed: %s", link_label(spec), peer, err)
            finally:
                plan.active = None
            if loop.time() - start > 10.0:   # it was a working link that dropped
                connected = True
                break
        if connected:
            delay = 1.0
            await asyncio.sleep(delay)
        else:
            await asyncio.sleep(delay)
            delay = min(max_backoff, delay * 2)


async def _nap(node: Node, seconds: float) -> None:
    """Sleep up to SECONDS, waking early when something asks for a re-plan."""
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(node.replan.wait(), seconds)
    node.replan.clear()


def _apply(node: Node, tasks: dict[str, tuple[PeerPlan, asyncio.Task]], wanted: dict[str, list[dict]],
           profile: dict) -> None:
    """Make TASKS (one link task per peer) match the plan WANTED."""
    for name in list(tasks):
        plan, task = tasks[name]
        specs = wanted.get(name)
        if not specs:                       # peer no longer wanted, or nothing usable here
            log.info("dropping links to %s", name)
            tasks.pop(name)
            task.cancel()
        elif specs != plan.specs:
            active, plan.specs = plan.active, specs
            if active is not None and active not in specs:      # the link in use is gone
                log.info("link to %s no longer planned (%s); redialing", name, link_label(active))
                tasks.pop(name)
                task.cancel()
            else:
                log.info("plan for %s changed: %s", name, ", ".join(link_label(x) for x in specs))
    for name, specs in wanted.items():
        if name in tasks:
            continue
        if not specs:
            log.debug("no usable links to %s on %s", name, profile["name"])
            continue
        log.info("linking to %s via %s", name, ", ".join(link_label(x) for x in specs))
        plan = PeerPlan(specs)
        tasks[name] = (plan, asyncio.create_task(keep_peer(node, name, specs, plan=plan)))


async def manage(node: Node, facts_fn: Callable[[], dict] = netdetect.gather_facts,
                 settle: float = 2.0) -> None:
    """Run the link manager until cancelled."""
    tasks: dict[str, tuple[PeerPlan, asyncio.Task]] = {}
    applied: dict[str, list[dict]] | None = None     # None until the first pass
    pending = None                                   # a changed plan waiting to be seen again
    last_profile = None
    try:
        while True:
            nap = node.cfg.manage_interval
            try:
                facts = await asyncio.to_thread(facts_fn)
                profile, wanted = current_plan(node, facts)
            except Exception as err:
                log.warning("link planning failed: %s", err)
                await _nap(node, nap)
                continue
            node.profile_scope = profile.get("scope", "all")      # narrowing inbound scope is never delayed
            if profile["name"] != last_profile:
                log.info("network profile: %s", profile["name"])
                last_profile = profile["name"]
            log.debug("plan pass: %s", {n: [link_label(x) for x in specs] for n, specs in wanted.items()})
            if applied is None or wanted == pending:
                _apply(node, tasks, wanted, profile)
                applied, pending = wanted, None
            elif wanted == applied:
                pending = None
            else:
                pending, nap = wanted, min(nap, settle)           # act if it comes out the same again
            await _nap(node, nap)
    finally:
        for _, task in tasks.values():
            task.cancel()
        await asyncio.gather(*(t for _, t in tasks.values()), return_exceptions=True)
