import asyncio

from messnet import daemon, manager
from messnet.links import serve_tcp

from test_replicate import types_at, until


def test_manager_links_and_switches_network(make_node, tmp_path):
    etc = tmp_path / "etc"
    a, b = make_node("a"), make_node("b")
    for n in (a, b):
        n.cfg.etc = etc
        n.cfg.manage_interval = 0.1
    b.cfg.peers = "*"
    (etc / "networks").mkdir(parents=True)
    (etc / "networks" / "home.toml").write_text(
        'detect = {domain = "home.local"}\nlinks = ["tcp"]\nscope = "lan"\n')
    (etc / "networks" / "default.toml").write_text('links = ["ssh"]\n')
    facts = {"hostname": "x", "addrs": [], "ifaces": [], "gateway": None, "gateway_mac": None,
             "domains": ["home.local"], "ssid": None, "tailscale": False}
    a.emit("lan.v1", scope="lan")

    async def main():
        server = await serve_tcp(a, "127.0.0.1:0", "lan")
        port = server.sockets[0].getsockname()[1]
        a.cfg.advertise = {"tcp": [{"addr": f"127.0.0.1:{port}", "scope": "lan"}]}
        daemon.publish_self(a)
        task = asyncio.create_task(manager.manage(b, lambda: facts))
        try:
            await until(lambda: types_at(b) == ["lan.v1"])
            assert b.linked("a")
            facts["domains"] = []          # leave home: default allows only ssh (none advertised)
            await until(lambda: not b.linked("a"))
        finally:
            task.cancel()
            server.close()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())


def test_inbound_scope_narrowed_by_profile(make_node):
    a, b = make_node("a"), make_node("b")
    a.emit("lan.v1", scope="lan")
    a.emit("all.v1")
    a.profile_scope = "all"    # e.g. manager detected an untrusted network

    async def main():
        from test_replicate import linked
        await linked(a, b, "lan", lambda: until(lambda: types_at(b) == ["all.v1"]))
        await asyncio.sleep(0.1)

    asyncio.run(main())
    assert types_at(b) == ["all.v1"]
