import tomllib

import pytest
from click.testing import CliRunner

from messnet import confedit
from messnet.cli import cli


def plain(doc):
    return tomllib.loads(confedit.tomlkit.dumps(doc))


def test_set_unset_keeps_comments(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("# my comment\nnode = \"a\"\n")
    doc = confedit.load(path)
    confedit.set_scalar(doc, "etc", str(tmp_path / "etc"))
    confedit.set_scalar(doc, "peers", "b, c")
    confedit.set_scalar(doc, "tcp_auth", "false")
    confedit.save(doc, path)
    text = path.read_text()
    assert text.startswith("# my comment") and (tmp_path / "etc").is_dir()
    assert tomllib.loads(text)["peers"] == ["b", "c"] and tomllib.loads(text)["tcp_auth"] is False
    assert confedit.unset_scalar(doc, "peers") and not confedit.unset_scalar(doc, "peers")
    with pytest.raises(confedit.ConfigEditError):
        confedit.set_scalar(doc, "bogus", "1")


def test_add_own_serving_side():
    doc = confedit.tomlkit.document()
    confedit.add_own(doc, "tcp", {"addr": "1.2.3.4:7701", "listen": "0.0.0.0:7701", "scope": "lan"})
    confedit.add_own(doc, "iroh", {"addrs": ["1.2.3.4:7702"], "bind": "0.0.0.0:7702",
                                   "relays": ["https://r"]})
    confedit.add_own(doc, "gonc", {"secret": "s", "mode": "tcp", "addr": "1.2.3.4:7703",
                                   "listen": "0.0.0.0:7703"})
    cfg = plain(doc)
    assert cfg["advertise"]["tcp"] == [{"addr": "1.2.3.4:7701", "scope": "lan"}]
    assert cfg["listen"] == [{"addr": "0.0.0.0:7701", "scope": "lan"}]
    assert cfg["iroh"] == {"bind": "0.0.0.0:7702", "relays": ["https://r"]}
    assert cfg["gonc_serve"] == [{"secret": "s", "mode": "tcp", "addr": "0.0.0.0:7703"}]
    with pytest.raises(ValueError):
        confedit.add_own(doc, "gonc", {"mode": "tcp", "secret": "s"})       # tcp needs addr
    with pytest.raises(confedit.ConfigEditError):
        confedit.add_own(doc, "carrier-pigeon", {})


def test_remove_own_cleans_serving_side():
    doc = confedit.tomlkit.document()
    confedit.add_own(doc, "tcp", {"addr": "h:1"})
    confedit.add_own(doc, "tcp", {"addr": "h:2"})
    confedit.add_own(doc, "gonc", {"secret": "s", "lan": True})
    confedit.remove_own(doc, "tcp", 1)
    assert plain(doc)["listen"] == ["h:2"]
    confedit.remove_own(doc, "tcp", None)
    confedit.remove_own(doc, "gonc", None)
    cfg = plain(doc)
    assert "listen" not in cfg and "gonc_serve" not in cfg and cfg["advertise"] == {}
    with pytest.raises(confedit.ConfigEditError):
        confedit.remove_own(doc, "ssh", 1)


def test_links_to_other_host():
    doc = confedit.tomlkit.document()
    confedit.add_link(doc, "b", "ssh", {"target": "b.example", "listen": "ignored", "tailscale": True})
    confedit.add_link(doc, "b", "ssh", {"target": "b2"})
    confedit.add_link(doc, "c", "tcp", {"addr": "c:1"})
    assert confedit.links_to(plain(doc), "b")["ssh"][1] == {"kind": "ssh", "peer": "b", "target": "b2"}
    confedit.remove_link(doc, "b", "ssh", 1)
    assert [s["target"] for s in plain(doc)["links"] if s["kind"] == "ssh"] == ["b2"]
    with pytest.raises(confedit.ConfigEditError):
        confedit.remove_link(doc, "b", "tcp", None)
    with pytest.raises(ValueError):
        confedit.add_link(doc, "b", "iroh", {})                              # iroh needs id


def test_methods_report():
    cfg = {"advertise": {"ssh": [{"target": "a"}]}, "listen": ["h:1"],
           "links": [{"kind": "tcp", "peer": "b", "addr": "b:1"}]}
    mine = "\n".join(confedit.methods_report(cfg, "a", "a", None))
    assert "ssh  1  a" in mine and "listen h:1" in mine and "-> b" in mine
    other = "\n".join(confedit.methods_report(cfg, "a", "b", {"tcp": [{"addr": "b:9"}]}))
    assert "tcp  b:9" in other and "tcp  1  b:1" in other


def test_new_secret(tmp_path):
    path = confedit.new_secret(tmp_path / "secrets", "x")
    assert len(path.read_text()) == 32 and path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(confedit.ConfigEditError):
        confedit.new_secret(tmp_path / "secrets", "x")


def run(tmp_path, *args):
    env = {"MESSNET_CONFIG": str(tmp_path / "config.toml"), "MESSNET_KEY": str(tmp_path / "key"),
           "MESSNET_DB": str(tmp_path / "db")}
    res = CliRunner().invoke(cli, list(args), env=env, catch_exceptions=False)
    assert res.exit_code == 0, res.output
    return res.output


def test_cli_config_flow(tmp_path):
    assert all(k in run(tmp_path, "config", "-h") for k in ("tcp", "ssh", "cmd", "iroh", "gonc", "--host"))
    run(tmp_path, "config", "set", "node", "a")
    run(tmp_path, "config", "set", "etc", str(tmp_path / "etc"))
    out = run(tmp_path, "config", "add", "tcp", "127.0.0.1:7701")
    assert "peer file updated" in out
    run(tmp_path, "config", "add", "--host", "b", "ssh", "b.example")
    peer = tomllib.loads((tmp_path / "etc" / "peers" / "a.toml").read_text())
    assert peer["tcp"] == [{"addr": "127.0.0.1:7701"}] and peer["id"]
    assert "tcp  1  127.0.0.1:7701" in run(tmp_path, "config", "methods")
    assert "ssh  1  b.example" in run(tmp_path, "config", "methods", "--host", "b")
    run(tmp_path, "config", "remove", "tcp", "1")
    assert "tcp" not in tomllib.loads((tmp_path / "etc" / "peers" / "a.toml").read_text())
    assert '"node": "a"' in run(tmp_path, "config", "show")
