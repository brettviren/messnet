"""gonc links: NAT traversal / encrypted TCP via https://github.com/threatexpert/gonc

An endpoint (in a peer file ``[[gonc]]`` table, or a ``gonc_serve`` config
entry on the responding node) has::

    mode = "p2p" | "tcp"
    secret = "hokum-haiku"      # file name in the local, unsynced secrets dir
    addr = "HOST:PORT"          # tcp mode: responder listen / dialer target
    lan = true                  # p2p: LAN broadcast discovery only
    mqttsrv = "tcp://..."       # p2p: self-hosted rendezvous broker(s)
    stunsrv = "host:3478"       # p2p: self-hosted STUN server(s)
    args = ["-kcp"]             # extra gonc arguments
    scope = "all"

Each endpoint has a policy class; network profiles allow classes with
``gonc = [...]`` (default ``["tcp", "lan", "p2p"]``):

    tcp         direct TLS+PSK TCP, no third parties
    lan         p2p with LAN discovery only
    p2p         p2p using only self-hosted mqttsrv AND stunsrv
    p2p-public  p2p using gonc's default public MQTT / STUN servers
"""

import asyncio
import contextlib
import logging
import os
import shlex
import sys
from pathlib import Path

from messnet.config import Config

log = logging.getLogger(__name__)

CLASSES = ("tcp", "lan", "p2p", "p2p-public")
DEFAULT_ALLOWED = ["tcp", "lan", "p2p"]


def gonc_class(ep: dict) -> str:
    mode = ep.get("mode", "p2p")
    if mode == "tcp":
        return "tcp"
    if mode != "p2p":
        raise ValueError(f"gonc mode must be p2p or tcp: {mode!r}")
    if ep.get("lan"):
        return "lan"
    if ep.get("mqttsrv") and ep.get("stunsrv"):
        return "p2p"
    return "p2p-public"


def allowed(ep: dict, profile: dict) -> bool:
    return gonc_class(ep) in profile.get("gonc", DEFAULT_ALLOWED)


def check(ep: dict) -> dict:
    gonc_class(ep)
    if not ep.get("secret"):
        raise ValueError(f"gonc endpoint needs a 'secret' name: {ep}")
    if "/" in ep["secret"] or ep["secret"].startswith("."):
        raise ValueError(f"gonc secret must be a plain file name: {ep['secret']!r}")
    if ep.get("mode") == "tcp" and not ep.get("addr"):
        raise ValueError(f"gonc tcp endpoint needs 'addr': {ep}")
    return ep


def secret_path(cfg: Config, name: str) -> Path:
    path = Path(cfg.secrets) / name
    if not path.is_file():
        raise FileNotFoundError(f"gonc secret not found: {path}")
    if path.stat().st_mode & 0o077:
        log.warning("gonc secret %s is readable by others; chmod 600 it", path)
    return path


def _common(ep: dict, cfg: Config) -> list[str]:
    argv = [cfg.gonc_bin]
    key = f"@{secret_path(cfg, ep['secret'])}"
    if ep.get("mode") == "tcp":
        argv += ["-tls", "-psk", key]
    else:
        argv += ["-p2p", key]
        if ep.get("lan"):
            argv.append("-lan")
        for opt in ("mqttsrv", "stunsrv"):
            if ep.get(opt):
                value = ep[opt]
                argv += [f"-{opt}", ",".join(value) if isinstance(value, list) else value]
    return argv + [str(a) for a in ep.get("args", [])]


def _host_port(addr: str) -> list[str]:
    host, _, port = addr.rpartition(":")
    return [host.strip("[]") or "0.0.0.0", port]


def dial_argv(ep: dict, cfg: Config) -> list[str]:
    """gonc command whose stdin/stdout reach the peer's responder."""
    argv = _common(check(ep), cfg)
    if ep.get("mode") == "tcp":
        return argv + _host_port(ep["addr"])
    return argv + ["-mqtt-hello"] if not ep.get("lan") else argv


def link_command(cfg: Config, scope: str) -> list[str]:
    """The messnet command gonc runs for each inbound connection."""
    cmd = [sys.executable, "-m", "messnet"]
    if cfg.path:
        cmd += ["-c", str(cfg.path)]
    cmd += ["--node", cfg.node, "--spool", str(cfg.spool), "--db", str(cfg.db),
            "--state", str(cfg.state), "--etc", str(cfg.etc)]
    return cmd + ["link", "--stdio", "--no-maintain", "--scope", scope]


def serve_argv(ep: dict, cfg: Config) -> list[str]:
    """gonc responder command running 'messnet link --stdio' per connection."""
    argv = _common(check(ep), cfg) + ["-k", "-e", shlex.join(link_command(cfg, ep.get("scope", "all")))]
    if ep.get("mode") == "tcp":
        return argv + ["-l"] + _host_port(ep["addr"])
    return argv + ["-mqtt-wait"] if not ep.get("lan") else argv + ["-lan-passive"]


def child_env(cfg: Config) -> dict:
    env = dict(os.environ)
    env["MESSNET_LOCAL_SPOOL"] = str(cfg.local_spool)
    env["MESSNET_KEY"] = str(cfg.key)
    return env


async def serve(cfg: Config, ep: dict, max_backoff: float = 300.0) -> None:
    """Keep a gonc responder running, restarting it with backoff."""
    delay = 1.0
    while True:
        argv = serve_argv(ep, cfg)
        log.info("starting gonc responder (%s)", gonc_class(ep))
        loop = asyncio.get_running_loop()
        start = loop.time()
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.DEVNULL,
                                                    env=child_env(cfg))
        try:
            rc = await proc.wait()
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                await proc.wait()
        log.warning("gonc responder exited with %s", rc)
        delay = 1.0 if loop.time() - start > max_backoff else min(max_backoff, delay * 2)
        await asyncio.sleep(delay)
