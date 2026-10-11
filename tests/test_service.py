"""The node service follows its configuration file and the peer directory."""

import asyncio
import logging
import socket
import types

import pytest

from messnet import logsetup
from messnet import config as cfgmod
from messnet.config import load_config
from messnet.links import run_link, serve_tcp
from messnet.manager import PeerPlan, _apply
from messnet.node import Node
from messnet.service import Service, supervised

from test_replicate import until


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def listening(port: int) -> bool:
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        return False
    writer.close()
    return True


class Env:
    """A node whose configuration comes from a file we can edit."""

    def __init__(self, tmp_path, name="svc"):
        self.tmp, self.path = tmp_path, tmp_path / f"{name}.toml"
        self.top = f'''node = "{name}"
spool = "{tmp_path / name / 'spool'}"
local_spool = "{tmp_path / name / 'local'}"
db = "{tmp_path / name / 'db'}"
state = "{tmp_path / name / 'state'}"
etc = "{tmp_path / 'etc'}"
key = "{tmp_path / name / 'key'}"
log = "{tmp_path / name / 'serve.log'}"
poll = 0.02
tcp_auth = false
'''
        self.node = None

    def start(self, top="", tail="") -> "Env":
        """Write the file and make the node from it, as 'messnet serve' would."""
        self.write(top, tail)
        self.node = Node(load_config(self.path))
        return self

    def write(self, top="", tail=""):
        """Rewrite the config file: base settings, then TOP (more top-level keys), then TAIL (tables)."""
        self.path.write_text(self.top + top + tail)

    async def apply(self, svc: Service) -> None:
        """What two looks of the service do: see the change, then see it again and apply it."""
        await svc.check_config()
        await svc.check_config()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    if e.node:
        e.node.close()


@pytest.fixture(autouse=True)
def clean_logging():
    yield
    logsetup.reset()


def test_listener_added_and_removed_by_editing_the_file(env):
    a, b = free_port(), free_port()
    env.start(f'listen = ["127.0.0.1:{a}"]\n')

    async def main():
        svc = Service(env.node)
        await svc.reconcile()
        try:
            assert await listening(a) and not await listening(b)
            env.write(f'listen = ["127.0.0.1:{a}", "127.0.0.1:{b}"]\n')
            await svc.check_config()                   # first look: not applied yet
            assert not await listening(b)
            await svc.check_config()                   # seen again: applied
            assert await listening(a) and await listening(b)
            env.write(f'listen = ["127.0.0.1:{b}"]\n')
            await env.apply(svc)
            assert not await listening(a) and await listening(b)
            env.write("")                              # no listeners at all
            await env.apply(svc)
            assert not await listening(b)
        finally:
            for comp in svc.components.values():
                await comp.stop()

    asyncio.run(main())


def test_removing_a_listener_ends_its_sessions(env, make_node):
    port = free_port()
    env.start(f'listen = ["127.0.0.1:{port}"]\n')
    peer = make_node("peer")
    peer.cfg.tcp_auth = False

    async def main():
        svc = Service(env.node)
        await svc.reconcile()
        task = asyncio.create_task(run_link(peer, {"kind": "tcp", "addr": f"127.0.0.1:{port}"}))
        try:
            await until(lambda: peer.linked("svc"))
            env.write("")
            await env.apply(svc)
            await until(lambda: not peer.linked("svc"))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            for comp in svc.components.values():
                await comp.stop()

    asyncio.run(main())


def test_unusable_file_keeps_the_running_configuration(env, caplog):
    port = free_port()
    env.start(f'listen = ["127.0.0.1:{port}"]\n')
    caplog.set_level(logging.INFO)

    async def main():
        svc = Service(env.node)
        await svc.reconcile()
        try:
            env.write('listen = [ "127.0.0.1:1"\n')                       # half typed
            await env.apply(svc)
            assert await listening(port)
            assert "not applied" in caplog.text and caplog.text.count("not applied") == 1
            await env.apply(svc)                                           # same bad content: said once
            assert caplog.text.count("not applied") == 1
            env.path.unlink()                                              # vanished (editor mid-save)
            await env.apply(svc)
            assert await listening(port) and "is missing" in caplog.text
            other = free_port()
            env.write(f'listen = ["127.0.0.1:{other}"]\n')                 # a good file again
            await env.apply(svc)
            assert await listening(other) and not await listening(port)
        finally:
            for comp in svc.components.values():
                await comp.stop()

    asyncio.run(main())


def test_changes_are_all_or_nothing(env, caplog):
    keep, new = free_port(), free_port()
    env.start(f'listen = ["127.0.0.1:{keep}"]\n')

    async def main():
        svc = Service(env.node)
        await svc.reconcile()
        try:
            env.write(f'listen = ["127.0.0.1:{keep}", "127.0.0.1:{new}"]\n',
                      '\n[[links]]\nkind = "carrier-pigeon"\n')           # one good change, one bad
            await env.apply(svc)
            assert not await listening(new)
            assert "carrier-pigeon" in caplog.text and env.node.cfg.listen == [f"127.0.0.1:{keep}"]
        finally:
            for comp in svc.components.values():
                await comp.stop()

    asyncio.run(main())


def test_settings_that_need_a_restart_are_left_alone(env, caplog):
    env.start()
    top = env.top.replace('node = "svc"', 'node = "renamed"')

    async def main():
        svc = Service(env.node)
        await svc.reconcile()
        try:
            env.path.write_text(top + 'iroh = {bind = "0.0.0.0:9"}\nlog_level = "debug"\n')
            await env.apply(svc)
        finally:
            for comp in svc.components.values():
                await comp.stop()

    asyncio.run(main())
    assert env.node.cfg.node == "svc"
    assert "node" in caplog.text and "restart" in caplog.text and "iroh.bind" in caplog.text
    assert env.node.cfg.log_level == "debug"                   # the rest of the edit still applied


def test_command_line_overrides_survive_a_reload(tmp_path):
    e = Env(tmp_path)
    e.write()
    cfg = load_config(e.path, node="forced")
    e.write('peers = ["x"]\n')
    new = cfgmod.reload_config(cfg)
    assert new.node == "forced" and new.peers == ["x"]
    changed, ignored = cfgmod.merge_reloadable(cfg, new)
    assert changed == ["peers"] and ignored == [] and cfg.peers == ["x"]


def test_hangup_applies_at_once(env):
    port = free_port()
    env.start()

    async def main():
        svc = Service(env.node)
        await svc.reconcile()
        try:
            env.write(f'listen = ["127.0.0.1:{port}"]\n')
            svc.poke()                                  # what SIGHUP does
            await svc.check_config()
            assert await listening(port)
        finally:
            for comp in svc.components.values():
                await comp.stop()

    asyncio.run(main())


def test_listener_that_cannot_bind_is_retried(env, caplog):
    port = free_port()
    env.start(f'listen = ["127.0.0.1:{port}"]\n')

    async def main():
        blocker = await asyncio.start_server(lambda r, w: None, "127.0.0.1", port)
        svc = Service(env.node)
        await svc.reconcile()                           # does not raise
        comp = svc.components[("listen", f"127.0.0.1:{port}", "all")]
        try:
            assert not comp.started and "cannot start" in caplog.text
            blocker.close()
            await blocker.wait_closed()
            comp.next_try = 0
            await svc._retry_failed()
            assert comp.started and await listening(port)
        finally:
            for c in svc.components.values():
                await c.stop()

    asyncio.run(main())


def test_crashing_component_is_restarted(caplog):
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("boom")
        await asyncio.sleep(0.05)

    asyncio.run(supervised("flaky", flaky, first_delay=0.01))
    assert len(calls) == 3 and caplog.text.count("crashed") == 2


def test_links_and_responders_follow_the_file(env):
    env.start()

    async def main():
        svc = Service(env.node)
        await svc.reconcile()
        assert set(svc.components) == {("maintain",)}
        env.write(tail='\n[[links]]\nkind = "tcp"\naddr = "127.0.0.1:9"\n')
        await env.apply(svc)
        kinds = sorted(k[0] for k in svc.components)
        assert kinds == ["link", "maintain"]
        env.write('peers = "*"\n')
        await env.apply(svc)
        assert sorted(k[0] for k in svc.components) == ["maintain", "manager"]
        env.write()
        await env.apply(svc)
        assert set(svc.components) == {("maintain",)}

    asyncio.run(main())


# ---- the peer directory ----------------------------------------------------------------

def test_peer_directory_changes_wake_the_manager(env):
    env.start()
    svc = Service(env.node)
    svc.check_peers()                                   # first look just records
    assert not env.node.replan.is_set()
    (env.tmp / "etc" / "peers").mkdir(parents=True)
    (env.tmp / "etc" / "peers" / "p.toml").write_text('node = "p"\n')
    svc.check_peers()
    assert env.node.replan.is_set()
    env.node.replan.clear()
    svc.check_peers()
    assert not env.node.replan.is_set()


def test_session_ends_when_the_peer_directory_stops_authorising_it(make_node, tmp_path):
    from messnet import peers
    a, b = make_node("a"), make_node("b")          # both trust each other (shared etc), tcp_auth on
    svc = Service(a)

    async def main():
        server = await serve_tcp(a, "127.0.0.1:0", "all")
        port = server.sockets[0].getsockname()[1]
        task = asyncio.create_task(run_link(b, {"kind": "tcp", "addr": f"127.0.0.1:{port}"}))
        try:
            await until(lambda: a.linked("b"))
            svc.audit()
            assert a.linked("b") and not svc.suspects             # authorised: untouched
            record = peers.load_peers(a.cfg.etc)["b"]
            (a.cfg.etc / "peers" / "b.toml").unlink()             # revoked...
            svc.audit()
            assert a.linked("b") and len(svc.suspects) == 1       # ...noted, not yet acted on
            peers.publish_peer(a.cfg.etc, record)                 # ...a glitch: it is back
            svc.audit()
            assert a.linked("b") and not svc.suspects
            (a.cfg.etc / "peers" / "b.toml").unlink()
            svc.audit()
            svc.audit()                                           # still gone: ended
            await until(lambda: not a.linked("b"))
        finally:
            task.cancel()
            server.close()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())


# ---- the manager keeps healthy links -------------------------------------------------------

def test_manager_keeps_a_working_link_when_its_spec_is_still_planned():
    s1, s2, s3 = ({"kind": "tcp", "addr": f"h:{n}", "scope": "all"} for n in (1, 2, 3))
    node = types.SimpleNamespace(linked=lambda peer: True)      # replacement tasks just wait

    async def main():
        task = asyncio.create_task(asyncio.sleep(60))
        plan = PeerPlan([s1, s2], active=s2)
        tasks = {"p": (plan, task)}
        profile = {"name": "x"}
        _apply(node, tasks, {"p": [s1, s2, s3]}, profile)        # an endpoint was added
        assert tasks["p"][1] is task and not task.cancelled() and plan.specs == [s1, s2, s3]
        _apply(node, tasks, {"p": [s1, s3]}, profile)            # the one in use was removed
        await asyncio.sleep(0)
        assert task.cancelled() and tasks["p"][1] is not task    # redialing with a new task
        replacement = tasks["p"][1]

        gone = asyncio.create_task(asyncio.sleep(60))
        tasks = {"q": (PeerPlan([s1], active=s1), gone)}
        _apply(node, tasks, {"q": []}, profile)                  # nothing usable any more
        await asyncio.sleep(0)
        assert "q" not in tasks and gone.cancelled()
        replacement.cancel()
        await asyncio.gather(replacement, return_exceptions=True)

    asyncio.run(main())


# ---- the log file ---------------------------------------------------------------------------

def test_log_file_levels_and_off(tmp_path):
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    path = tmp_path / "sub" / "serve.log"
    log = logging.getLogger("messnet.test")
    logsetup.configure(path, "info")
    log.info("hello info")
    log.debug("hidden debug")
    logsetup.configure(path, "warning")
    log.info("hidden now")
    log.warning("a warning")
    logsetup.configure(path, "off")
    log.warning("not in the file")
    text = path.read_text()
    assert "hello info" in text and "a warning" in text
    assert "hidden" not in text and "not in the file" not in text
    assert " INFO messnet.test: hello info" in text and text.split()[0].count("-") == 2


def test_unwritable_log_is_not_fatal(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    logsetup.configure(blocker / "serve.log", "info")           # a directory cannot be made under a file
    logging.getLogger("messnet.test").warning("still working")


# ---- end to end -------------------------------------------------------------------------------

def test_running_service_follows_an_edit(env):
    port = free_port()
    env.start()

    async def main():
        task = asyncio.create_task(Service(env.node, interval=0.05).run())
        try:
            await asyncio.sleep(0.2)
            assert not await listening(port)
            env.write(f'listen = ["127.0.0.1:{port}"]\n')
            for _ in range(100):
                if await listening(port):
                    break
                await asyncio.sleep(0.05)
            assert await listening(port)
            env.write()
            for _ in range(100):
                if not await listening(port):
                    break
                await asyncio.sleep(0.05)
            assert not await listening(port)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())
    assert not env.node.live


def test_peer_file_appearing_and_disappearing_makes_and_ends_the_link(tmp_path):
    a, b = Env(tmp_path, "a"), Env(tmp_path, "b")
    port = free_port()
    a.start(f'listen = ["127.0.0.1:{port}"]\n', f'\n[[advertise.tcp]]\naddr = "127.0.0.1:{port}"\n')
    b.start('peers = ["a"]\nmanage_interval = 30.0\n')            # polls slowly: only a wake-up helps
    a.node.emit("hello.v1")
    facts = {"hostname": "x", "addrs": [], "ifaces": [], "gateway": None, "gateway_mac": None,
             "domains": [], "ssid": None, "tailscale": False}
    peer_file = tmp_path / "etc" / "peers" / "a.toml"

    async def main():
        from messnet import manager
        svc_b = Service(b.node, interval=0.05, settle=0.05)
        # the real network facts are irrelevant here and slow to gather
        orig = manager.manage
        manager.manage = lambda node, facts_fn=None, settle=2.0: orig(node, lambda: facts, settle)
        tb = asyncio.create_task(svc_b.run())
        try:
            await asyncio.sleep(0.4)
            assert not b.node.linked("a")                        # no peer file for a yet
            ta = asyncio.create_task(Service(a.node, interval=0.05).run())      # publishes a's peer file
            await until(lambda: b.node.linked("a"), timeout=10)
            await until(lambda: [e for _, e in b.node.store.query(["hello.v1"])])
            peer_file.unlink()                                   # a is no longer in the directory
            await until(lambda: not b.node.linked("a"), timeout=10)
            ta.cancel()
            await asyncio.gather(ta, return_exceptions=True)
        finally:
            manager.manage = orig
            tb.cancel()
            await asyncio.gather(tb, return_exceptions=True)

    try:
        asyncio.run(main())
    finally:
        a.node.close()
        b.node.close()


# ---- settings and the CLI ---------------------------------------------------------------------

def test_log_settings_through_the_cli(tmp_path):
    from test_cli import run
    cfg = tmp_path / "c.toml"
    run(tmp_path, "-c", str(cfg), "config", "set", "log_level", "Debug")
    assert 'log_level = "debug"' in cfg.read_text()
    assert "must be one of" in run(tmp_path, "-c", str(cfg), "config", "set", "log_level", "chatty", codes=(1,))
    assert 'log_level = "debug"' in cfg.read_text()                      # the bad value was not written
    run(tmp_path, "-c", str(cfg), "config", "set", "log", str(tmp_path / "x.log"))
    import json
    shown = json.loads(run(tmp_path, "-c", str(cfg), "config", "show"))
    assert shown["log"] == str(tmp_path / "x.log") and shown["log_level"] == "debug" and "load_args" not in shown


def test_install_list_mentions_the_log(tmp_path):
    from test_cli import run
    assert "log:" not in run(tmp_path, "install", "--list")
    (tmp_path / "serve.log").write_text("x\n")
    assert f"log: {tmp_path / 'serve.log'} (info)" in run(tmp_path, "install", "--list")
