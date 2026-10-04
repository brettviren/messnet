import asyncio
import os
import shutil
import socket
import sys
from pathlib import Path

import pytest

from messnet import gonc, peers
from messnet.links import keep_link

from test_replicate import types_at, until

GONC = shutil.which("gonc") or str(Path.home() / "sync/bin/gonc")
have_gonc = os.access(GONC, os.X_OK)


def with_secret(node, name="s1", value="sekrit"):
    node.cfg.secrets = node.cfg.db.parent / "secrets"
    node.cfg.secrets.mkdir(exist_ok=True)
    (node.cfg.secrets / name).write_text(value)
    os.chmod(node.cfg.secrets / name, 0o600)
    node.cfg.gonc_bin = GONC


def test_classes():
    assert gonc.gonc_class({"mode": "tcp"}) == "tcp"
    assert gonc.gonc_class({"lan": True}) == "lan"
    assert gonc.gonc_class({"mqttsrv": "tcp://m:1883", "stunsrv": "s:3478"}) == "p2p"
    assert gonc.gonc_class({"mqttsrv": "tcp://m:1883"}) == "p2p-public"
    with pytest.raises(ValueError):
        gonc.gonc_class({"mode": "udp"})
    with pytest.raises(ValueError):
        gonc.check({"mode": "tcp", "secret": "x"})
    with pytest.raises(ValueError):
        gonc.check({"secret": "../etc/passwd"})


def test_argv(make_node):
    n = make_node("a")
    with_secret(n)
    key = f"@{n.cfg.secrets / 's1'}"
    assert gonc.dial_argv({"mode": "tcp", "secret": "s1", "addr": "h:7"}, n.cfg)[1:] == [
        "-tls", "-psk", key, "h", "7"]
    assert gonc.dial_argv({"secret": "s1", "mqttsrv": ["tcp://a", "tcp://b"], "stunsrv": "s"},
                          n.cfg)[1:] == ["-p2p", key, "-mqttsrv", "tcp://a,tcp://b", "-stunsrv", "s",
                                         "-mqtt-hello"]
    srv = gonc.serve_argv({"secret": "s1", "lan": True, "scope": "lan"}, n.cfg)
    assert srv[1:4] == ["-p2p", key, "-lan"] and srv[-1] == "-lan-passive"
    assert "--scope lan" in srv[srv.index("-e") + 1]
    with pytest.raises(FileNotFoundError):
        gonc.dial_argv({"secret": "missing"}, n.cfg)


def test_plan_filters_by_gonc_policy():
    rec = {"gonc": [{"secret": "s", "mqttsrv": "tcp://pub"},           # p2p-public
                    {"secret": "s", "lan": True},                       # lan
                    {"secret": "s", "mode": "tcp", "addr": "h:1"}]}     # tcp
    prof = {"name": "x", "links": ["gonc"], "scope": "all"}
    got = peers.plan_peer("b", rec, prof, {})
    assert [gonc.gonc_class(s) for s in got] == ["lan", "tcp"]
    got = peers.plan_peer("b", rec, {**prof, "gonc": ["p2p-public"]}, {})
    assert [gonc.gonc_class(s) for s in got] == ["p2p-public"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(not have_gonc, reason="gonc not installed")
def test_gonc_tcp_link_end_to_end(make_node):
    a, b = make_node("a"), make_node("b")
    with_secret(a)
    with_secret(b)
    addr = f"127.0.0.1:{free_port()}"
    a.emit("from.a")
    b.emit("from.b")
    ep = {"mode": "tcp", "secret": "s1", "addr": addr}

    async def main():
        responder = asyncio.create_task(gonc.serve(a.cfg, ep))
        await asyncio.sleep(1.0)
        dialer = asyncio.create_task(keep_link(b, {"kind": "gonc", "peer": "a", **ep}))
        try:
            await until(lambda: types_at(b) == ["from.a", "from.b"], timeout=20)
        finally:
            for t in (dialer, responder):
                t.cancel()
            await asyncio.gather(dialer, responder, return_exceptions=True)

    asyncio.run(main())
    a2 = make_node("a")   # the responder side ran in a gonc-spawned messnet process
    assert types_at(a2) == ["from.a", "from.b"]
