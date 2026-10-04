import pytest

from messnet.netdetect import matches, select_profile

FACTS = {"hostname": "hokum", "addrs": ["192.168.1.155", "100.96.203.121"],
         "ifaces": ["wlp82s0", "tailscale0"], "gateway": "192.168.1.1",
         "gateway_mac": "3C:F0:83:35:6B:DD", "domains": ["home.local"], "ssid": None,
         "tailscale": True}


def test_matches_each_key():
    assert matches({"ip_prefix": "192.168.1.0/24"}, FACTS)
    assert matches({"ip_prefix": ["10.0.0.0/8", "100.64.0.0/10"]}, FACTS)
    assert matches({"gateway_mac": "3c:f0:83:35:6b:dd"}, FACTS)
    assert matches({"domain": "home.local", "tailscale": True}, FACTS)
    assert not matches({"domain": "home.local", "tailscale": False}, FACTS)
    assert not matches({"ssid": "anything"}, FACTS)
    assert not matches({}, FACTS)
    with pytest.raises(ValueError):
        matches({"color": "blue"}, FACTS)


def test_select_profile():
    nets = [{"name": "home", "detect": {"domain": "home.local"}, "links": ["tcp"], "scope": "lan"},
            {"name": "home-ts", "detect": {"domain": "home.local", "tailscale": True},
             "priority": 5, "links": ["tcp", "ssh"]},
            {"name": "work", "detect": {"domain": "bnl.gov"}}]
    assert select_profile(nets, FACTS)["name"] == "home-ts"
    assert select_profile(nets, {**FACTS, "tailscale": False})["name"] == "home"
    assert select_profile(nets, {**FACTS, "domains": []})["name"] == "default"
    assert select_profile(nets, FACTS, forced="work")["links"] == ["tcp", "ssh", "cmd"]
    with pytest.raises(ValueError):
        select_profile(nets, FACTS, forced="nope")


def test_default_profile_override():
    nets = [{"name": "default", "links": ["ssh"]}]
    prof = select_profile(nets, FACTS)
    assert prof["links"] == ["ssh"] and prof["scope"] == "all"
