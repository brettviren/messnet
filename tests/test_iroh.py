import asyncio

import pytest

from messnet import daemon, iroh_link, keys, manager
from messnet.links import keep_link

from test_replicate import types_at, until

pytestmark = pytest.mark.skipif(iroh_link.iroh is None, reason="iroh not installed")


def setup(node, **iroh_cfg):
    node.cfg.iroh = {"bind": "127.0.0.1:0", **iroh_cfg}


async def serving(node):
    tr = await iroh_link.transport(node)
    return tr, asyncio.create_task(tr.serve())


async def shutdown(*items):
    for item in items:
        if isinstance(item, asyncio.Task):
            item.cancel()
            await asyncio.gather(item, return_exceptions=True)
        elif item is not None:
            await item.close()


def test_node_id_is_iroh_id(make_node):
    a = make_node("a")
    setup(a)

    async def main():
        tr = await iroh_link.transport(a)
        try:
            assert tr.id() == keys.public_id(keys.load_seed(a.cfg.key))
        finally:
            await tr.close()

    asyncio.run(main())


def test_iroh_link_replicates(make_node):
    a, b = make_node("a"), make_node("b")
    setup(a)
    setup(b)
    b.cfg.advertise = {"iroh": [{}]}     # publishes b's id so a authorizes it
    daemon.publish_self(b)
    a.emit("x.v1")

    async def main():
        tr, srv = await serving(a)
        spec = {"kind": "iroh", "id": tr.id(), "addrs": tr.addrs(), "peer": "a"}
        task = asyncio.create_task(keep_link(b, spec))
        try:
            await until(lambda: types_at(b) == ["x.v1"], timeout=15)
            b.emit("y.v1")
            await until(lambda: types_at(a) == ["x.v1", "y.v1"], timeout=15)
        finally:
            await shutdown(task, srv, tr, b.iroh)

    asyncio.run(main())


def test_iroh_refuses_unknown_endpoint(make_node):
    a, b = make_node("a"), make_node("b", publish=False)
    setup(a)
    setup(b)
    a.emit("secret.v1")

    async def main():
        tr, srv = await serving(a)
        spec = {"kind": "iroh", "id": tr.id(), "addrs": tr.addrs()}
        try:
            from messnet.links import run_link
            with pytest.raises(Exception):
                await asyncio.wait_for(run_link(b, spec), 15)
            assert types_at(b) == []
        finally:
            await shutdown(srv, tr, b.iroh)

    asyncio.run(main())


def test_manager_plans_iroh(make_node):
    a, b = make_node("a"), make_node("b")
    setup(a)
    setup(b)
    b.cfg.peers, b.cfg.manage_interval = ["a"], 0.1
    a.emit("x.v1")

    async def main():
        tr, srv = await serving(a)
        a.cfg.advertise = {"iroh": [{"addrs": tr.addrs()}]}
        b.cfg.advertise = {"iroh": [{}]}      # so a knows b's id
        daemon.publish_self(a)
        daemon.publish_self(b)
        (a.cfg.etc / "networks").mkdir(parents=True, exist_ok=True)
        (a.cfg.etc / "networks" / "default.toml").write_text('links = ["iroh"]\n')
        _, plan = manager.current_plan(b, {"tailscale": False})
        assert [s["kind"] for s in plan["a"]] == ["iroh"]
        task = asyncio.create_task(manager.manage(b, lambda: {"tailscale": False}))
        try:
            await until(lambda: types_at(b) == ["x.v1"], timeout=15)
        finally:
            await shutdown(task, srv, tr, b.iroh)

    asyncio.run(main())
