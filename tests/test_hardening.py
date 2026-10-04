import asyncio
import time

import pytest

from messnet import keys, peers
from messnet.links import run_link, serve_tcp
from messnet.replicate import ProtocolError

from test_replicate import linked, types_at, until


def tcp_pair(a, b, check):
    async def main():
        server = await serve_tcp(a, "127.0.0.1:0")
        port = server.sockets[0].getsockname()[1]
        try:
            await check(port)
        finally:
            server.close()
    asyncio.run(main())


def test_tcp_auth_rejects_untrusted_dialer(make_node):
    a, b = make_node("a"), make_node("b", publish=False)
    a.emit("x.v1")

    async def check(port):
        with pytest.raises(ProtocolError, match="refused"):
            await asyncio.wait_for(run_link(b, {"kind": "tcp", "addr": f"127.0.0.1:{port}",
                                                "auth": False}), 5)
        assert types_at(b) == []

    tcp_pair(a, b, check)


def test_tcp_auth_rejects_impostor_key(make_node, tmp_path):
    a, b = make_node("a"), make_node("b")
    # Replace b's published id with someone else's key.
    other = keys.public_id(keys.load_seed(tmp_path / "other.key"))
    peers.publish_peer(a.cfg.etc, peers.peer_record("b", {}, {"id": other}))

    async def check(port):
        with pytest.raises(ProtocolError, match="refused"):
            await asyncio.wait_for(run_link(b, {"kind": "tcp", "addr": f"127.0.0.1:{port}",
                                                "auth": False}), 5)

    tcp_pair(a, b, check)


def test_tcp_auth_disabled(make_node):
    a, b = make_node("a", publish=False), make_node("b", publish=False)
    a.cfg.tcp_auth = b.cfg.tcp_auth = False
    a.emit("x.v1")
    asyncio.run(linked(a, b, "all", lambda: until(lambda: types_at(b) == ["x.v1"])))


def test_sign_verify():
    seed = bytes(range(32))
    pub = keys.public_id(seed)
    sig = keys.sign(seed, b"m")
    assert keys.verify(pub, b"m", sig) and not keys.verify(pub, b"n", sig)
    assert not keys.verify("zz", b"m", sig)


def test_prune_keeps_vv_and_seq(make_node):
    a = make_node("a")
    for _ in range(3):
        a.emit("old.v1", time="2020-01-01T00:00:00+00:00")
    a.emit("new.v1")
    events, files = a.prune(time.time() - 86400, spool=True)
    assert (events, files) == (3, 1)
    assert types_at(a) == ["new.v1"]
    assert a.store.version_vector() == {"a/all": 4}
    assert a.emit("x.v1")["seq"] == 5
    assert a.store.gaps() == {}


def test_pruned_events_not_reinserted(make_node):
    a, b = make_node("a"), make_node("b")
    old = [a.emit("old.v1", time="2020-01-01T00:00:00+00:00") for _ in range(2)]
    for ev in old:
        b.receive(ev)
    b.prune(time.time())
    assert not b.receive(old[0])
    a.emit("new.v1")
    asyncio.run(linked(a, b, "all", lambda: until(lambda: types_at(b) == ["new.v1"])))
    assert b.store.version_vector()["a/all"] == 3


def test_seq_safe_after_full_prune(make_node):
    a = make_node("a")
    a.emit("old.v1", time="2020-01-01T00:00:00+00:00")
    a.prune(time.time() + 10, spool=True)
    assert a.emit("x.v1")["seq"] == 2


def test_gaps(make_node):
    a, b = make_node("a"), make_node("b")
    evs = [a.emit("t.v1") for _ in range(6)]
    for i in (0, 3, 5):
        b.receive(evs[i])
    assert b.store.gaps() == {"a/all": [(2, 3), (5, 5)]}


def test_rebuild(make_node):
    a = make_node("a")
    a.emit("own.v1")
    a.receive({**a.emit("tmp.v1"), "origin": "z", "seq": 1})   # link-only event
    assert a.rebuild() == 2
    assert types_at(a) == ["own.v1", "tmp.v1"]
    assert {ev["origin"] for _, ev in a.store.query()} == {"a"}
    assert a.cfg.db.with_name(a.cfg.db.name + ".bak").exists()
