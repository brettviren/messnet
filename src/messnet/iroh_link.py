"""iroh transport (optional: install ``messnet[iroh]``).

Each node process binds one iroh endpoint using the node key, so the iroh
endpoint id equals the node id.  No n0 infrastructure is used: the
endpoint uses the minimal preset, relaying is disabled unless the config
lists self-hosted relays, and peers are dialed by explicit address
(id + direct addresses + optional relay URL) taken from the peer directory.

Inbound connections are authorized by endpoint id: only ids found in
the peer directory are accepted (unless ``iroh.allow = "any"``), and the
peer's hello must name the node that id belongs to.
"""

import asyncio
import contextlib
import ipaddress
import logging

from messnet import keys, peers
from messnet.node import Node
from messnet.replicate import Session

log = logging.getLogger(__name__)

ALPN = b"messnet/1"
READ_CHUNK = 65536

try:
    import iroh
except ImportError:          # optional dependency
    iroh = None


def require() -> None:
    if iroh is None:
        raise RuntimeError("iroh links need the optional iroh package: uv sync --extra iroh")


def dialable(addrs: list[str]) -> list[str]:
    """Drop wildcard addresses (dialing them hangs)."""
    out = []
    for addr in addrs:
        host = addr.rpartition(":")[0].strip("[]")
        try:
            if ipaddress.ip_address(host).is_unspecified:
                continue
        except ValueError:
            pass
        out.append(addr)
    return out


class IrohWriter:
    """asyncio.StreamWriter look-alike over an iroh send stream."""

    def __init__(self, send, conn, pump: asyncio.Task):
        self.send, self.conn, self.pump = send, conn, pump
        self.buf = bytearray()

    def write(self, data: bytes) -> None:
        self.buf += data

    async def drain(self) -> None:
        if self.buf:
            data = bytes(self.buf)
            self.buf.clear()
            await self.send.write_all(data)

    def close(self) -> None:
        self.pump.cancel()

    async def wait_closed(self) -> None:
        with contextlib.suppress(Exception):
            await self.send.finish()
        with contextlib.suppress(Exception):
            self.conn.close(0, b"bye")

    def get_extra_info(self, name, default=None):
        return default


def stream_pair(conn, bi) -> tuple[asyncio.StreamReader, IrohWriter]:
    reader = asyncio.StreamReader(limit=2**24)
    recv = bi.recv()

    async def pump():
        try:
            while data := await recv.read(READ_CHUNK):
                reader.feed_data(data)
        except Exception as err:
            log.debug("iroh stream ended: %s", err)
        finally:
            reader.feed_eof()

    return reader, IrohWriter(bi.send(), conn, asyncio.create_task(pump()))


class IrohTransport:
    def __init__(self, node: Node, endpoint):
        self.node, self.endpoint = node, endpoint

    @classmethod
    async def open(cls, node: Node) -> "IrohTransport":
        require()
        icfg = node.cfg.iroh
        relays = icfg.get("relays") or []
        mode = iroh.RelayMode.custom_from_urls(relays) if relays else iroh.RelayMode.disabled()
        opts = iroh.EndpointOptions(preset=iroh.preset_minimal(), bind_addr=icfg.get("bind", "0.0.0.0:0"),
                                    secret_key=keys.load_seed(node.cfg.key), alpns=[ALPN],
                                    relay_mode=mode)
        endpoint = await iroh.Endpoint.bind(opts)
        log.info("iroh endpoint %s on %s", endpoint.id(), endpoint.bound_sockets())
        return cls(node, endpoint)

    def id(self) -> str:
        return str(self.endpoint.id())

    def addrs(self) -> list[str]:
        return dialable(self.endpoint.addr().direct_addresses())

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.endpoint.close()

    async def dial(self, spec: dict) -> tuple[asyncio.StreamReader, IrohWriter]:
        addr = iroh.EndpointAddr(iroh.EndpointId.from_string(spec["id"]), spec.get("relay"),
                                 dialable(list(spec.get("addrs", []))))
        conn = await self.endpoint.connect(addr, ALPN)
        return stream_pair(conn, await conn.open_bi())

    def _authorize(self, remote_id: str) -> str | None:
        """Node name for REMOTE_ID from the peer directory (or "" if any is allowed)."""
        for name, rec in peers.load_peers(self.node.cfg.etc).items():
            if rec.get("id") == remote_id or any(ep.get("id") == remote_id for ep in rec.get("iroh", [])):
                return name
        return "" if self.node.cfg.iroh.get("allow") == "any" else None

    async def _handle(self, incoming) -> None:
        conn = await (await incoming.accept()).connect()
        remote = str(conn.remote_id())
        name = self._authorize(remote)
        if name is None:
            log.warning("refusing iroh link from unknown endpoint %s", remote)
            conn.close(1, b"unknown endpoint")
            return
        reader, writer = stream_pair(conn, await conn.accept_bi())
        scope = self.node.cfg.iroh.get("scope", "all")
        link_scope = "all" if "all" in (scope, self.node.profile_scope) else "lan"
        await Session(self.node, reader, writer, link_scope, expect=name or None,
                      label=f"iroh-in:{remote[:10]}").run()

    async def serve(self) -> None:
        """Accept iroh links until cancelled or the endpoint closes."""
        handlers = set()
        try:
            while (incoming := await self.endpoint.accept_next()) is not None:
                task = asyncio.create_task(self._safe(incoming))
                handlers.add(task)
                task.add_done_callback(handlers.discard)
        finally:
            for task in handlers:
                task.cancel()

    async def _safe(self, incoming) -> None:
        try:
            await self._handle(incoming)
        except Exception as err:
            log.warning("iroh inbound link failed: %s", err)


async def transport(node: Node) -> IrohTransport:
    """The node's iroh transport, opened on first use."""
    async with node.iroh_lock:
        if node.iroh is None:
            node.iroh = await IrohTransport.open(node)
        return node.iroh


def advertise_ids(node: Node, endpoints: list[dict]) -> list[dict]:
    """Fill each advertised iroh endpoint's ``id`` with this node's id."""
    nid = keys.public_id(keys.load_seed(node.cfg.key))
    return [{"id": nid, **{k: v for k, v in ep.items() if k != "id"}} for ep in endpoints]
