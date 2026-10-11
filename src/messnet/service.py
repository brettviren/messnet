"""The node service behind ``messnet serve`` / ``run``: it follows the configuration.

The service owns a set of *components*: spool/state upkeep, one listener per ``listen`` entry,
one task per explicit ``[[links]]`` entry and per ``[[gonc_serve]]`` responder, the link manager
(when ``peers`` is set) and the iroh listener.  Every component has a key made from the setting
that asks for it, so *reconciling* is a set difference: stop what is no longer wanted, start what
is new, leave the rest alone (and their connections with them).

What triggers it, and how it stays safe:

* The config file is read every ``interval`` seconds (and on SIGHUP).  A change is acted on only
  after the same content has been seen twice, so an editor mid-save is not applied; a file that
  does not parse, or names a bad link, is reported once and the running configuration is kept; a
  file that has vanished is ignored.  Nothing is applied unless all of it is valid.
* Only ``config.RELOADABLE`` settings change.  Command line options still win, as at startup.
  Node name, store paths and iroh's ``bind`` / ``relays`` are opened once; changing them is
  reported and needs a restart.
* The peer directory (``etc/peers``, ``etc/networks``) is watched too: a change makes the link
  manager re-plan at once, and sessions that the directory authorised but no longer does (a
  peer file removed, its id changed) are ended, once the lack has been seen twice.
* A component that fails to start (a port in use) is retried with backoff; one that crashes is
  restarted.  Neither takes the service down.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import signal
import time
import tomllib
from collections.abc import Awaitable, Callable
from pathlib import Path

from messnet import config as cfgmod
from messnet import daemon, logsetup, manager
from messnet.links import check_spec, keep_link, link_label, serve_tcp
from messnet.node import Node

log = logging.getLogger(__name__)

RELOAD_INTERVAL = 2.0


def canon(obj) -> str:
    """A stable text form of a settings dict, to compare and key by."""
    return json.dumps(obj, sort_keys=True, default=str)


def listen_specs(listen: list) -> list[dict]:
    """Normalize ``listen`` entries: "HOST:PORT" or {addr=..., scope=...}."""
    return [{"addr": x} if isinstance(x, str) else dict(x) for x in listen]


def wants_iroh_listener(cfg) -> bool:
    return cfg.iroh.get("listen", "iroh" in cfg.advertise)


async def supervised(name: str, factory: Callable[[], Awaitable], max_backoff: float = 60.0,
                     first_delay: float = 1.0) -> None:
    """Run FACTORY() to completion; if it crashes, say so and run it again with backoff."""
    delay = first_delay
    while True:
        started = time.monotonic()
        try:
            await factory()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s crashed; restarting in %.0fs", name, delay)
        delay = first_delay if time.monotonic() - started > max_backoff else min(max_backoff, delay * 2)
        await asyncio.sleep(delay)


class Component:
    """One running piece of the service."""

    def __init__(self, label: str):
        self.label = label
        self.started = False
        self.delay, self.next_try = 1.0, 0.0

    async def start(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError


class TaskComponent(Component):
    def __init__(self, label: str, factory: Callable[[], Awaitable]):
        super().__init__(label)
        self.factory = factory
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(supervised(self.label, self.factory))

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


class ListenComponent(Component):
    """A TCP listener.  Stopping it also ends the sessions it accepted."""

    def __init__(self, node: Node, addr: str, scope: str):
        super().__init__(f"listener {addr} ({scope} scope)")
        self.node, self.addr, self.scope = node, addr, scope
        self.server: asyncio.Server | None = None

    async def start(self) -> None:
        self.server = await serve_tcp(self.node, self.addr, self.scope)

    async def stop(self) -> None:
        if not self.server:
            return
        self.server.close()
        sessions = list(getattr(self.server, "sessions", ()))
        for task in sessions:
            task.cancel()
        await asyncio.gather(*sessions, return_exceptions=True)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.server.wait_closed(), 2.0)


class Service:
    def __init__(self, node: Node, interval: float = RELOAD_INTERVAL, configure_log: bool = False,
                 settle: float = 2.0):
        self.node, self.interval, self.configure_log, self.settle = node, interval, configure_log, settle
        self.components: dict[tuple, Component] = {}
        self._wake = asyncio.Event()
        self.force = False                       # SIGHUP: apply a changed file without waiting
        self.applied: str | None = None          # hash of the config file content last applied
        self.pending: str | None = None          # content seen once, applied if seen again
        self.failed: str | None = None           # content that could not be applied (said once)
        self.missing = False
        self.peer_sig: tuple | None = None
        self.suspects: set = set()               # sessions the peer directory stopped authorising
        self.audit_due = False

    # ---- what should be running ------------------------------------------------------------

    def desired(self, cfg) -> dict[tuple, Callable[[], Component]]:
        """Component constructors by key for CFG.  Raises ValueError for a bad configuration."""
        node, want = self.node, {}
        want[("maintain",)] = lambda: TaskComponent("spool and state upkeep", lambda: daemon.maintain(node))
        for spec in cfg.links:
            spec = check_spec(dict(spec))
            want[("link", canon(spec))] = lambda s=spec: TaskComponent(
                f"link {link_label(s)}", lambda: keep_link(node, s))
        for ls in listen_specs(cfg.listen):
            addr, scope = ls["addr"], ls.get("scope", "all")
            want[("listen", addr, scope)] = lambda a=addr, sc=scope: ListenComponent(node, a, sc)
        if cfg.gonc_serve:
            from messnet import gonc
            for ep in cfg.gonc_serve:
                ep = gonc.check(dict(ep))
                want[("gonc", canon(ep))] = lambda e=ep: TaskComponent(
                    "gonc responder", lambda: gonc.serve(cfg, e))
        if cfg.peers:
            want[("manager",)] = lambda: TaskComponent("link manager", lambda: manager.manage(node, settle=self.settle))
        if wants_iroh_listener(cfg):
            async def iroh_serve():
                from messnet import iroh_link
                await (await iroh_link.transport(node)).serve()
            want[("iroh",)] = lambda: TaskComponent("iroh listener", iroh_serve)
        return want

    async def _start(self, comp: Component) -> None:
        try:
            await comp.start()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            comp.next_try = time.monotonic() + comp.delay
            log.warning("cannot start %s: %s (retrying in %.0fs)", comp.label, err, comp.delay)
            comp.delay = min(300.0, comp.delay * 2)
        else:
            comp.started = True
            log.info("started %s", comp.label)

    async def reconcile(self) -> None:
        """Make the running components match the configuration."""
        want = self.desired(self.node.cfg)
        for key in [k for k in self.components if k not in want]:
            comp = self.components.pop(key)
            log.info("stopping %s", comp.label)
            await comp.stop()
        for key, make in want.items():
            if key not in self.components:
                self.components[key] = make()
                await self._start(self.components[key])

    async def _retry_failed(self) -> None:
        for comp in list(self.components.values()):
            if not comp.started and time.monotonic() >= comp.next_try:
                await self._start(comp)

    # ---- following the configuration file ----------------------------------------------------

    async def check_config(self) -> None:
        cfg = self.node.cfg
        path = cfgmod.source_file(cfg)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            if not self.missing:
                log.warning("config file %s is missing; keeping the current settings", path)
            self.missing = True
            return
        except OSError as err:
            log.warning("cannot read %s: %s; keeping the current settings", path, err)
            return
        self.missing = False
        digest = hashlib.sha256(data).hexdigest()
        if digest == self.applied or (digest == self.failed and not self.force):
            self.pending, self.force = None, False
            return
        if digest != self.pending and not self.force:
            self.pending = digest                # new content: wait to see it again, unchanged
            return
        self.pending, self.force = None, False
        try:
            new = cfgmod.reload_config(cfg)
            self.desired(new)                    # validate everything before changing anything
        except (OSError, ValueError, tomllib.TOMLDecodeError) as err:
            self.failed = digest
            log.warning("config %s not applied: %s; keeping the current settings", path, err)
            return
        changed, ignored = cfgmod.merge_reloadable(cfg, new)
        self.applied, self.failed = digest, None
        if ignored:
            log.warning("config: %s changed but only takes effect after a restart; ignored",
                        ", ".join(ignored))
        if changed:
            log.info("config reloaded from %s: changed %s", path, ", ".join(changed))
        else:
            log.debug("config %s read: no changes", path)
        if self.configure_log:
            logsetup.configure(cfg.log, cfg.log_level)
        if not changed:
            return
        await self.reconcile()
        if "advertise" in changed or "iroh" in changed:       # what this node tells its peers
            daemon.publish_self(self.node)
        self.node.replan.set()

    # ---- following the peer directory ----------------------------------------------------------

    def signature(self) -> tuple:
        etc, sig = Path(self.node.cfg.etc), []
        for sub in ("peers", "networks"):
            for file in sorted((etc / sub).glob("*.toml")):
                with contextlib.suppress(OSError):
                    st = file.stat()
                    sig.append((sub, file.name, st.st_mtime_ns, st.st_size))
        return tuple(sig)

    def check_peers(self) -> None:
        sig = self.signature()
        if sig != self.peer_sig:
            if self.peer_sig is not None:
                log.info("peer directory changed")
                self.node.replan.set()
                self.audit_due = True
            self.peer_sig = sig
        if self.audit_due or self.suspects:
            self.audit()

    def audit(self) -> None:
        """End sessions that the peer directory authorised but no longer does."""
        failing = set()
        for session in list(self.node.live):
            if session.recheck is None:
                continue
            try:
                ok = session.recheck()
            except Exception:                    # cannot tell: do not cut a working link
                ok = True
            if not ok:
                failing.add(session)
        for session in failing & self.suspects:  # failed twice in a row
            log.warning("ending the link with %s: the peer directory no longer authorises it", session.peer)
            session.revoke("the peer directory no longer authorises this peer")
        self.suspects = failing - self.suspects
        self.audit_due = bool(self.suspects)

    # ---- the loop -------------------------------------------------------------------------

    def poke(self) -> None:
        """Look at the configuration now, and apply a change without waiting for it to settle."""
        self.force = True
        self._wake.set()

    async def _nap(self) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._wake.wait(), self.interval)
        self._wake.clear()

    async def run(self, ready: Callable[[], None] | None = None) -> None:
        """Run until cancelled.  READY is called once the components have been started."""
        node, cfg = self.node, self.node.cfg
        log.info("service starting: node %s, pid %d, config %s", cfg.node, os.getpid(), cfgmod.source_file(cfg))
        self.desired(cfg)                        # a bad configuration fails here, before anything starts
        daemon.publish_self(node)
        loop = asyncio.get_running_loop()
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(signal.SIGHUP, self.poke)
        try:
            await self.reconcile()
            self.peer_sig = self.signature()
            if ready:
                ready()
            while True:
                await self._nap()
                await self.check_config()
                self.check_peers()
                await self._retry_failed()
        finally:
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.remove_signal_handler(signal.SIGHUP)
            for comp in reversed(list(self.components.values())):
                await comp.stop()
            self.components.clear()
            log.info("service stopped")
