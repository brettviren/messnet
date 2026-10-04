"""Link transports: stdio, TCP and subprocess (ssh, gonc, any command).

A link spec is a dict, e.g. from config.toml::

    [[links]]
    kind = "ssh"            # ssh | tcp | cmd
    target = "hokum"        # ssh alias (kind=ssh)
    scope = "lan"           # link scope: lan | all (default all)

    [[links]]
    kind = "tcp"
    addr = "100.96.203.121:7701"

    [[links]]
    kind = "cmd"
    argv = ["gonc", "-p2p", "secret"]
"""

import asyncio
import contextlib
import logging
import shlex
import sys
import time

from messnet.node import Node
from messnet.replicate import LINK_SCOPES, Session

log = logging.getLogger(__name__)

REMOTE_COMMAND = ["messnet", "link", "--stdio"]


def parse_addr(addr: str, default_host: str = "127.0.0.1") -> tuple[str, int]:
    host, sep, port = addr.rpartition(":")
    if not sep or not port.isdigit():
        raise ValueError(f"expected HOST:PORT, got {addr!r}")
    return (host.strip("[]") or default_host), int(port)


def ssh_argv(target: str, command: list[str] | str | None = None) -> list[str]:
    remote = command or REMOTE_COMMAND
    if isinstance(remote, str):
        remote = shlex.split(remote)
    return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30", target,
            shlex.join(remote)]


def link_argv(spec: dict) -> list[str] | None:
    """The subprocess argv for a spec, or None for non-subprocess links."""
    kind = spec.get("kind")
    if kind == "ssh":
        return ssh_argv(spec["target"], spec.get("command"))
    if kind == "cmd":
        argv = spec["argv"]
        return shlex.split(argv) if isinstance(argv, str) else list(argv)
    return None


def link_label(spec: dict) -> str:
    return spec.get("name") or f"{spec.get('kind')}:{spec.get('target') or spec.get('addr') or spec.get('argv')}"


def check_spec(spec: dict) -> dict:
    kind = spec.get("kind")
    need = {"ssh": "target", "tcp": "addr", "cmd": "argv"}
    if kind not in need:
        raise ValueError(f"link kind must be one of {tuple(need)}: {spec}")
    if need[kind] not in spec:
        raise ValueError(f"{kind} link needs {need[kind]!r}: {spec}")
    if spec.get("scope", "all") not in LINK_SCOPES:
        raise ValueError(f"link scope must be one of {LINK_SCOPES}: {spec}")
    return spec


async def stdio_streams() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=2**24)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    transport, protocol = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, sys.stdout)
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    return reader, writer


async def run_link(node: Node, spec: dict) -> None:
    """Open the link described by SPEC and run one session over it."""
    scope = spec.get("scope", "all")
    argv = link_argv(spec)
    if argv is None:
        host, port = parse_addr(spec["addr"])
        reader, writer = await asyncio.open_connection(host, port, limit=2**24)
        await Session(node, reader, writer, scope).run()
        return
    log.debug("spawning %s", shlex.join(argv))
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.PIPE, limit=2**24)
    try:
        await Session(node, proc.stdout, proc.stdin, scope).run()
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
        await proc.wait()


async def keep_link(node: Node, spec: dict, max_backoff: float = 60.0) -> None:
    """Run a link forever, reconnecting with exponential backoff."""
    label, delay = link_label(spec), 1.0
    while True:
        start = time.monotonic()
        try:
            await run_link(node, spec)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            log.warning("link %s failed: %s", label, err)
        if time.monotonic() - start > max_backoff:
            delay = 1.0
        log.info("link %s reconnecting in %.0fs", label, delay)
        await asyncio.sleep(delay)
        delay = min(max_backoff, delay * 2)


async def serve_tcp(node: Node, addr: str, scope: str = "all") -> asyncio.Server:
    """Accept TCP links on ADDR; each connection runs a session."""
    async def handle(reader, writer):
        peer = writer.get_extra_info("peername")
        try:
            await Session(node, reader, writer, scope).run()
        except Exception as err:
            log.warning("tcp link from %s failed: %s", peer, err)

    host, port = parse_addr(addr)
    server = await asyncio.start_server(handle, host, port, limit=2**24)
    log.info("listening on %s:%d (%s scope)", host, port, scope)
    return server
