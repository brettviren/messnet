import tomllib

from messnet import peers

PROFILE = {"name": "home", "links": ["tcp", "ssh"], "scope": "lan"}


def test_publish_roundtrip_and_idempotent(tmp_path):
    rec = peers.peer_record("a.bv", {"tcp": [{"addr": "1.2.3.4:7701", "scope": "lan",
                                              "networks": ["home"]}],
                                     "ssh": {"target": "a"}})
    assert peers.publish_peer(tmp_path, rec)
    path = tmp_path / "peers" / "a.bv.toml"
    loaded = tomllib.loads(path.read_text())
    assert loaded["ssh"] == [{"target": "a"}] and loaded["tcp"][0]["networks"] == ["home"]
    assert not peers.publish_peer(tmp_path, rec)
    assert peers.load_peers(tmp_path)["a.bv"]["node"] == "a.bv"


def test_load_skips_conflicts_and_misnamed(tmp_path):
    d = tmp_path / "peers"
    d.mkdir()
    (d / "a.toml").write_text('node = "a"\n')
    (d / "b.toml").write_text('node = "evil"\n')
    (d / "a.sync-conflict-1.toml").write_text('node = "a"\n')
    (d / "c.toml").write_text('not toml [[[')
    assert list(peers.load_peers(tmp_path)) == ["a"]


def test_plan_orders_and_filters():
    rec = {"node": "b",
           "ssh": [{"target": "b"}],
           "tcp": [{"addr": "100.1.1.1:1", "tailscale": True},
                   {"addr": "192.168.1.2:1", "networks": ["home"], "scope": "lan"},
                   {"addr": "10.0.0.2:1", "networks": ["work"]}],
           "cmd": [{"argv": ["gonc", "x"]}]}
    specs = peers.plan_peer("b", rec, PROFILE, {"tailscale": False})
    assert [(s["kind"], s.get("addr") or s.get("target"), s["scope"]) for s in specs] == [
        ("tcp", "192.168.1.2:1", "lan"), ("ssh", "b", "all")]
    assert all(s["peer"] == "b" for s in specs)
    specs = peers.plan_peer("b", rec, {**PROFILE, "scope": "all"}, {"tailscale": True})
    assert [s["scope"] for s in specs] == ["all", "all", "all"]


def test_plan_peer_selection():
    known = {"a": {}, "b": {"ssh": [{"target": "b"}]}, "c": {}}
    assert list(peers.plan("a", "*", known, PROFILE, {})) == ["b", "c"]
    assert peers.plan("a", ["b", "z"], known, PROFILE, {}) == {
        "b": [{"target": "b", "kind": "ssh", "peer": "b", "scope": "all"}], "z": []}
    assert peers.plan("a", [], known, PROFILE, {}) == {}
