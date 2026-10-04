"""Network facts gathering and network profile matching.

Facts are a plain dict so matching is a pure, testable function::

    {"hostname": "hokum", "addrs": ["192.168.1.10", ...], "ifaces": ["wlan0", ...],
     "gateway": "192.168.1.1", "gateway_mac": "aa:bb:...", "domains": ["example.org"],
     "ssid": "homenet", "tailscale": True}

A profile (``networks/<name>.toml``) has a ``detect`` table.  Every key in it
must match; a key's value may be a scalar or a list (any element matching):

    hostname, iface, gateway, gateway_mac, domain, ssid  -- exact (case-insensitive)
    ip_prefix                                            -- CIDR containing any address
    tailscale                                            -- boolean
"""

import ipaddress
import json
import logging
import shutil
import socket
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_PROFILE = {"name": "default", "links": ["tcp", "ssh", "cmd"], "scope": "all", "priority": -1,
                   "gonc": ["tcp", "lan", "p2p"]}


def _run(argv: list[str], timeout: float = 3.0) -> str:
    if not shutil.which(argv[0]):
        return ""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError) as err:
        log.debug("%s failed: %s", argv[0], err)
        return ""


def _json(argv: list[str]):
    try:
        return json.loads(_run(argv) or "null")
    except json.JSONDecodeError:
        return None


def _domains(resolv: Path = Path("/etc/resolv.conf")) -> list[str]:
    out = []
    try:
        for line in resolv.read_text().splitlines():
            parts = line.split()
            if parts and parts[0] in ("search", "domain"):
                out.extend(parts[1:])
    except OSError:
        pass
    return out


def gather_facts() -> dict:
    """Probe the current host's network situation (best effort, never raises)."""
    facts = {"hostname": socket.gethostname().split(".")[0], "addrs": [], "ifaces": [],
             "gateway": None, "gateway_mac": None, "domains": _domains(), "ssid": None,
             "tailscale": False}
    for iface in _json(["ip", "-j", "addr"]) or []:
        if iface.get("operstate") == "DOWN" or iface.get("ifname") == "lo":
            continue
        facts["ifaces"].append(iface.get("ifname"))
        facts["addrs"] += [a["local"] for a in iface.get("addr_info", []) if "local" in a]
    routes = _json(["ip", "-j", "route", "show", "default"]) or []
    if routes:
        facts["gateway"] = routes[0].get("gateway")
    if facts["gateway"]:
        neigh = _json(["ip", "-j", "neigh", "show", facts["gateway"]]) or []
        if neigh:
            facts["gateway_mac"] = neigh[0].get("lladdr")
    facts["ssid"] = _run(["iwgetid", "-r"]).strip() or None
    ts = _json(["tailscale", "status", "--json", "--peers=false"]) or {}
    facts["tailscale"] = ts.get("BackendState") == "Running"
    return facts


def _as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def _eq(fact, wanted) -> bool:
    facts = [str(f).lower() for f in _as_list(fact) if f is not None]
    return any(str(w).lower() in facts for w in _as_list(wanted))


def _in_prefix(addrs: list[str], prefixes) -> bool:
    for prefix in _as_list(prefixes):
        net = ipaddress.ip_network(prefix, strict=False)
        for addr in addrs:
            try:
                if ipaddress.ip_address(addr) in net:
                    return True
            except ValueError:
                continue
    return False


_MATCHERS = {
    "hostname": lambda f, w: _eq(f["hostname"], w),
    "iface": lambda f, w: _eq(f["ifaces"], w),
    "gateway": lambda f, w: _eq(f["gateway"], w),
    "gateway_mac": lambda f, w: _eq(f["gateway_mac"], w),
    "domain": lambda f, w: _eq(f["domains"], w),
    "ssid": lambda f, w: _eq(f["ssid"], w),
    "ip_prefix": lambda f, w: _in_prefix(f["addrs"], w),
    "tailscale": lambda f, w: bool(f["tailscale"]) == bool(w),
}


def matches(detect: dict, facts: dict) -> bool:
    """True if every condition in DETECT holds for FACTS (an empty DETECT never matches)."""
    if not detect:
        return False
    for key, wanted in detect.items():
        if key not in _MATCHERS:
            raise ValueError(f"unknown network detect key: {key}")
        if not _MATCHERS[key](facts, wanted):
            return False
    return True


def select_profile(profiles: list[dict], facts: dict, forced: str | None = None) -> dict:
    """Pick the matching profile with highest priority, or the default profile.

    A profile named "default" among PROFILES replaces the built-in default.
    """
    by_name = {p["name"]: p for p in profiles}
    default = {**DEFAULT_PROFILE, **by_name.get("default", {})}
    if forced:
        if forced == "default":
            return default
        if forced not in by_name:
            raise ValueError(f"no such network profile: {forced}")
        return {**DEFAULT_PROFILE, **by_name[forced]}
    hits = [p for p in profiles if p["name"] != "default" and matches(p.get("detect", {}), facts)]
    if not hits:
        return default
    best = max(hits, key=lambda p: (p.get("priority", 0), p["name"]))
    return {**DEFAULT_PROFILE, **best}
