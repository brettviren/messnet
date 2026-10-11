"""The concentrator: pure consumers are sent every event the node receives."""

import asyncio
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

import pytest

from messnet import publish, subscribe
from messnet.links import run_link, serve_tcp
from messnet.subscribe import SubscribeError

from test_replicate import until


@pytest.fixture
def sockdir():
    """A short directory: unix socket paths are limited to about 100 characters."""
    d = Path(tempfile.mkdtemp(prefix="mn-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


async def collect(gen, count, timeout=5.0):
    out = []

    async def pull():
        async for ev in gen:
            out.append(ev)
            if len(out) >= count:
                return

    await asyncio.wait_for(pull(), timeout)
    return out


def test_default_socket_does_not_depend_on_the_runtime_dir(monkeypatch, tmp_path):
    """cron does not set XDG_RUNTIME_DIR; a daemon it starts must be found by a client in a shell."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    under_cron = publish.default_socket()
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1001")
    assert publish.default_socket() == under_cron == tmp_path / "cache" / "messnet" / "events.sock"


def test_check_publish():
    assert publish.check_publish({"kind": "unix", "path": "/x/s"}) == {"kind": "unix", "path": "/x/s", "scope": "host"}
    assert publish.check_publish({"kind": "tcp", "addr": "127.0.0.1:9"}) == {"kind": "tcp", "addr": "127.0.0.1:9", "scope": "all"}
    assert publish.check_publish({"kind": "unix"})["path"].endswith("messnet/events.sock")

    for bad in ({"kind": "pigeon"}, {"kind": "tcp"}, {"kind": "tcp", "addr": "nope"}, {"kind": "unix", "scope": "everyone"}):
        with pytest.raises(ValueError):
            publish.check_publish(bad)


def test_consumer_gets_replay_then_live_events(make_node, sockdir):
    n = make_node("a")
    for i in range(5):
        n.emit("old.v1", {"i": i})
    spec = publish.check_publish({"kind": "unix", "path": str(sockdir / "e.sock")})

    async def main():
        server = await publish.serve(n, spec)
        try:
            gen = subscribe.subscribe(f"unix:{spec['path']}", replay=2)
            task = asyncio.create_task(collect(gen, 4))
            await asyncio.sleep(0.2)
            n.emit("new.v1", {"i": 100})
            n.emit("new.v1", {"i": 101})
            got = await task
            assert [e["data"]["i"] for e in got] == [3, 4, 100, 101]          # last 2 replayed, then live
            assert os.stat(spec["path"]).st_mode & 0o777 == 0o600 and stat.S_ISSOCK(os.stat(spec["path"]).st_mode)
        finally:
            server.close()
            server.cleanup()

    asyncio.run(main())
    assert not os.path.exists(spec["path"])


def test_filters_and_scope(make_node, sockdir):
    n = make_node("a")
    spec_all = publish.check_publish({"kind": "unix", "path": str(sockdir / "all.sock"), "scope": "all"})
    spec_host = publish.check_publish({"kind": "unix", "path": str(sockdir / "host.sock")})

    async def main():
        servers = [await publish.serve(n, spec_all), await publish.serve(n, spec_host)]
        try:
            everything = asyncio.create_task(collect(subscribe.subscribe(f"unix:{spec_host['path']}"), 4))
            public = asyncio.create_task(collect(subscribe.subscribe(f"unix:{spec_all['path']}"), 2))
            only_b = asyncio.create_task(collect(subscribe.subscribe(f"unix:{spec_host['path']}", types=["b.*"]), 1))
            await asyncio.sleep(0.3)
            n.emit("a.v1", {"k": 1})
            n.emit("b.v1", {"k": 2})
            n.emit("secret.v1", {"k": 3}, scope="host")
            n.emit("lan.v1", {"k": 4}, scope="lan")
            n.emit("c.v1", {"k": 5})
            got_all = await everything
            assert [e["type"] for e in got_all] == ["a.v1", "b.v1", "secret.v1", "lan.v1"]
            assert [e["type"] for e in await public] == ["a.v1", "b.v1"]              # only 'all' scope events
            assert [e["type"] for e in await only_b] == ["b.v1"]
        finally:
            for s in servers:
                s.close()
                s.cleanup()

    asyncio.run(main())


def test_events_received_over_a_link_reach_consumers(make_node, sockdir):
    a, b = make_node("a"), make_node("b")
    spec = publish.check_publish({"kind": "unix", "path": str(sockdir / "e.sock")})

    async def main():
        server = await publish.serve(b, spec)
        link_server = await serve_tcp(a, "127.0.0.1:0", "all")
        port = link_server.sockets[0].getsockname()[1]
        link = asyncio.create_task(run_link(b, {"kind": "tcp", "addr": f"127.0.0.1:{port}", "auth": False}))
        try:
            task = asyncio.create_task(collect(subscribe.subscribe(f"unix:{spec['path']}", types=["from.a*"]), 1))
            await asyncio.sleep(0.2)
            a.cfg.tcp_auth = False
            a.emit("from.a.v1", {"hello": "b"})
            (ev,) = await task
            assert ev["origin"] == "a" and ev["data"] == {"hello": "b"}              # b's consumer saw a's event
        finally:
            link.cancel()
            link_server.close()
            server.close()
            server.cleanup()
            await asyncio.gather(link, return_exceptions=True)

    asyncio.run(main())


def test_bad_subscription_and_missing_node(make_node, sockdir):
    n = make_node("a")
    spec = publish.check_publish({"kind": "unix", "path": str(sockdir / "e.sock")})

    async def main():
        with pytest.raises(SubscribeError, match="cannot connect"):
            async for _ in subscribe.subscribe(f"unix:{sockdir / 'nothing.sock'}"):
                pass
        server = await publish.serve(n, spec)
        try:
            reader, writer = await subscribe.open_target(f"unix:{spec['path']}")
            writer.write(b'{"op":"replicate"}\n')
            await writer.drain()
            frame = json.loads(await reader.readline())
            assert frame["op"] == "error" and "subscribe" in frame["reason"]
            writer.close()
        finally:
            server.close()
            server.cleanup()

    asyncio.run(main())


def test_stale_socket_is_replaced_but_a_live_one_or_a_file_is_not(make_node, sockdir):
    n = make_node("a")
    path = sockdir / "e.sock"
    spec = publish.check_publish({"kind": "unix", "path": str(path)})

    async def main():
        # a dead socket file (its owner crashed)
        import socket
        dead = socket.socket(socket.AF_UNIX)
        dead.bind(str(path))
        dead.close()
        assert path.exists()
        first = await publish.serve(n, spec)                       # takes it over
        try:
            with pytest.raises(OSError, match="in use"):
                await publish.serve(n, spec)                        # but not from a live one
        finally:
            first.close()
            first.cleanup()
        path.write_text("not a socket")
        with pytest.raises(OSError, match="not a socket"):
            await publish.serve(n, spec)

    asyncio.run(main())


def test_tcp_consumer(make_node):
    n = make_node("a")

    async def main():
        spec = publish.check_publish({"kind": "tcp", "addr": "127.0.0.1:0"})
        server = await publish.serve(n, spec)
        port = server.sockets[0].getsockname()[1]
        try:
            task = asyncio.create_task(collect(subscribe.subscribe(f"127.0.0.1:{port}"), 1))
            await asyncio.sleep(0.2)
            n.emit("t.v1", {"x": 1})
            n.emit("hidden.v1", scope="host")                       # a tcp consumer is 'all' scope
            (ev,) = await task
            assert ev["type"] == "t.v1"
        finally:
            server.close()

    asyncio.run(main())


def test_latest_file_follows_arrivals(make_node, tmp_path):
    n = make_node("a")
    path = tmp_path / "sub" / "latest.json"
    n.emit("before.v1", {"n": 0})

    async def main():
        task = asyncio.create_task(publish.write_latest(n, path))
        try:
            await until(lambda: path.exists())
            assert subscribe.read_latest(path)["type"] == "before.v1"            # the newest, at start
            n.emit("one.v1", {"n": 1})
            n.emit("two.v1", {"n": 2})
            await until(lambda: (subscribe.read_latest(path) or {}).get("type") == "two.v1")
            assert not [p for p in path.parent.iterdir() if p.name != "latest.json"]     # no temp files left
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())
    assert subscribe.read_latest(tmp_path / "missing.json") is None


def test_store_tail_and_scope_filter(make_node):
    n = make_node("a")
    for i in range(6):
        n.emit("x.v1" if i % 2 else "y.v1", {"i": i}, scope="host" if i == 5 else "all")
    assert [e["data"]["i"] for _, e in n.store.tail(3)] == [3, 4, 5]
    assert [e["data"]["i"] for _, e in n.store.tail(3, scopes=("all",))] == [2, 3, 4]
    assert [e["data"]["i"] for _, e in n.store.tail(5, types=["x.*"])] == [1, 3, 5]
    assert [e["data"]["i"] for _, e in n.store.tail(2, upto=3)] == [1, 2]
    assert [e["data"]["i"] for _, e in n.store.query(scopes=("host",))] == [5]
