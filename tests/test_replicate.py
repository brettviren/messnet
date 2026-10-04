import asyncio
import sys

import pytest

from messnet.links import keep_link, run_link, serve_tcp
from messnet.replicate import LINK_EVENT, ProtocolError


async def until(cond, timeout=5.0):
    async def poll():
        while not cond():
            await asyncio.sleep(0.02)
    await asyncio.wait_for(poll(), timeout)


def types_at(node):
    """Event types at NODE, excluding the node's own link status events."""
    return sorted(ev["type"] for _, ev in node.store.query() if ev["type"] != LINK_EVENT)


async def linked(a, b, scope, body):
    server = await serve_tcp(a, "127.0.0.1:0", scope)
    port = server.sockets[0].getsockname()[1]
    task = asyncio.create_task(run_link(b, {"kind": "tcp", "addr": f"127.0.0.1:{port}", "scope": scope}))
    try:
        await body()
    finally:
        task.cancel()
        server.close()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("scope,expect", [("all", ["all.v1"]), ("lan", ["all.v1", "lan.v1"])])
def test_catchup_respects_scope(make_node, scope, expect):
    a, b = make_node("a"), make_node("b")
    a.emit("all.v1")
    a.emit("lan.v1", scope="lan")
    a.emit("host.v1", scope="host")

    async def body():
        await until(lambda: len(types_at(b)) == len(expect))
        await asyncio.sleep(0.1)

    asyncio.run(linked(a, b, scope, body))
    assert types_at(b) == expect


def test_live_bidirectional(make_node):
    a, b = make_node("a"), make_node("b")

    async def body():
        await asyncio.sleep(0.1)
        a.emit("from.a")
        b.emit("from.b")
        await until(lambda: types_at(a) == types_at(b) == ["from.a", "from.b"])

    asyncio.run(linked(a, b, "all", body))


def test_multihop(make_node):
    a, b, c = make_node("a"), make_node("b"), make_node("c")
    a.emit("x.v1")

    async def main():
        async def body():
            await until(lambda: types_at(c) == ["x.v1"])
            a.emit("y.v1")
            await until(lambda: types_at(c) == ["x.v1", "y.v1"])
        await linked(a, b, "all", lambda: linked(b, c, "all", body))

    asyncio.run(main())


def test_reconnect_resumes(make_node):
    a, b = make_node("a"), make_node("b")
    a.emit("x.v1")

    async def body():
        await until(lambda: len(types_at(b)) == 1)

    asyncio.run(linked(a, b, "all", body))
    a.emit("y.v1")
    asyncio.run(linked(a, b, "all", lambda: until(lambda: len(types_at(b)) == 2)))


def test_cmd_link_over_subprocess_stdio(make_node, tmp_path, monkeypatch):
    """A cmd link (as ssh/gonc would be) to a 'messnet link --stdio' child process."""
    a, b = make_node("a"), make_node("b")
    monkeypatch.setenv("MESSNET_KEY", str(b.cfg.key))
    monkeypatch.setenv("MESSNET_LOCAL_SPOOL", str(b.cfg.local_spool))
    b.emit("from.b")
    a.emit("from.a")
    b.close()
    argv = [sys.executable, "-m", "messnet", "--node", "b", "--spool", str(b.cfg.spool),
            "--db", str(b.cfg.db), "--state", str(b.cfg.state), "link", "--stdio"]

    async def main():
        task = asyncio.create_task(keep_link(a, {"kind": "cmd", "argv": argv}))
        try:
            await until(lambda: types_at(a) == ["from.a", "from.b"], timeout=15)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())
    b2 = make_node("b")
    assert types_at(b2) == ["from.a", "from.b"]


def link_states(node):
    return [(ev["subject"], ev["data"]["state"])
            for _, ev in node.store.query([LINK_EVENT])]


def test_link_status_events_and_registry(make_node):
    a, b = make_node("a"), make_node("b")

    async def body():
        await until(lambda: a.linked("b") and b.linked("a"))

    asyncio.run(linked(a, b, "all", body))
    assert link_states(b) == [("a", "up"), ("a", "down")]
    assert not b.linked("a")
    assert all(ev["scope"] == "host" for _, ev in b.store.query([LINK_EVENT]))


def test_expected_peer_mismatch(make_node):
    a, b = make_node("a"), make_node("b")

    async def main():
        server = await serve_tcp(a, "127.0.0.1:0")
        port = server.sockets[0].getsockname()[1]
        try:
            with pytest.raises(ProtocolError):
                await run_link(b, {"kind": "tcp", "addr": f"127.0.0.1:{port}", "peer": "c"})
        finally:
            server.close()

    asyncio.run(main())
