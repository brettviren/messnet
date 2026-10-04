"""Peer directory and link planning.

The ``etc`` directory is meant to be shared by Syncthing::

    etc/peers/<node>.toml      each node writes only its own file
    etc/networks/<name>.toml   network profiles (hand written)

A peer file advertises how a node can be reached, by link kind::

    node = "hokum.bv"
    updated = "2026-10-04T12:00:00-04:00"
    [[tcp]]
    addr = "100.96.203.121:7701"
    scope = "lan"            # optional, default "all"
    tailscale = true         # optional: usable only when the dialer has tailscale up
    networks = ["home"]      # optional: usable only on these network profiles
    [[ssh]]
    target = "hokum"
    command = "messnet link --stdio"   # optional

The current network profile lists the allowed link kinds in preference
order and the widest link scope; planning turns both into ordered link
specs per peer.
"""

import logging
import tomllib
from pathlib import Path

from messnet.event import now_iso
from messnet.links import ENDPOINT_KINDS, check_spec

log = logging.getLogger(__name__)


def _load_dir(path: Path) -> list[tuple[Path, dict]]:
    out = []
    if not path.is_dir():
        return out
    for file in sorted(path.glob("*.toml")):
        if ".sync-conflict" in file.name:
            continue
        try:
            out.append((file, tomllib.loads(file.read_text())))
        except (OSError, tomllib.TOMLDecodeError) as err:
            log.warning("skipping unreadable %s: %s", file, err)
    return out


def load_peers(etc: Path) -> dict[str, dict]:
    """Map node name to peer record; the file stem names the node."""
    peers = {}
    for file, rec in _load_dir(Path(etc) / "peers"):
        if rec.setdefault("node", file.stem) != file.stem:
            log.warning("peer file %s names node %s, ignored", file, rec["node"])
            continue
        peers[file.stem] = rec
    return peers


def load_networks(etc: Path) -> list[dict]:
    nets = []
    for file, rec in _load_dir(Path(etc) / "networks"):
        rec.setdefault("name", file.stem)
        nets.append(rec)
    return nets


# --- writing our own peer file -------------------------------------------

def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k} = {_toml_value(v)}" for k, v in value.items()) + "}"
    raise TypeError(f"cannot write {type(value).__name__} to TOML")


def dump_peer(rec: dict) -> str:
    """Serialize a peer record: scalars first, then arrays of tables per kind."""
    lines, tables = [], []
    for key, value in rec.items():
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            tables.append((key, value))
        elif isinstance(value, dict):
            tables.append((key, value))
        else:
            lines.append(f"{key} = {_toml_value(value)}")
    for key, value in tables:
        for item in (value if isinstance(value, list) else [value]):
            lines.append("")
            lines.append(f"[[{key}]]" if isinstance(value, list) else f"[{key}]")
            lines += [f"{k} = {_toml_value(v)}" for k, v in item.items()]
    return "\n".join(lines) + "\n"


def peer_record(node: str, advertise: dict, extra: dict | None = None) -> dict:
    rec = {"node": node, **(extra or {})}
    for kind, endpoints in advertise.items():
        rec[kind] = endpoints if isinstance(endpoints, list) else [endpoints]
    return rec


def publish_peer(etc: Path, rec: dict) -> bool:
    """Write REC to etc/peers/<node>.toml unless only "updated" would change."""
    path = Path(etc) / "peers" / f"{rec['node']}.toml"
    if path.exists():
        try:
            old = tomllib.loads(path.read_text())
            old.pop("updated", None)
            if old == {k: v for k, v in rec.items() if k != "updated"}:
                return False
        except tomllib.TOMLDecodeError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(dump_peer({**rec, "updated": now_iso()}))
    tmp.replace(path)
    return True


# --- planning -----------------------------------------------------------

def narrower(a: str, b: str) -> str:
    """The link scope carrying less: "all" (wide area) is narrower than "lan"."""
    return "all" if "all" in (a, b) else "lan"


def endpoint_usable(ep: dict, profile: dict, facts: dict) -> bool:
    nets = ep.get("networks")
    if nets and profile["name"] not in nets:
        return False
    if ep.get("tailscale") and not facts.get("tailscale"):
        return False
    return True


def endpoint_spec(kind: str, ep: dict, peer: str, profile: dict) -> dict:
    spec = {k: v for k, v in ep.items() if k not in ("networks", "tailscale")}
    spec.update(kind=kind, peer=peer, scope=narrower(ep.get("scope", "all"), profile.get("scope", "all")))
    return check_spec(spec)


def plan_peer(peer: str, rec: dict, profile: dict, facts: dict) -> list[dict]:
    """Ordered link specs to try for PEER under PROFILE."""
    specs = []
    for kind in profile.get("links", []):
        if kind not in ENDPOINT_KINDS:
            log.warning("network %s lists unknown link kind %s", profile["name"], kind)
            continue
        for ep in rec.get(kind, []):
            if not endpoint_usable(ep, profile, facts):
                continue
            try:
                specs.append(endpoint_spec(kind, ep, peer, profile))
            except ValueError as err:
                log.warning("bad %s endpoint for %s: %s", kind, peer, err)
    return specs


def wanted_peers(selector, me: str, known: dict) -> list[str]:
    if selector == "*" or selector == ["*"]:
        names = list(known)
    else:
        names = list(selector or [])
    return sorted(n for n in names if n != me)


def plan(me: str, selector, peers: dict, profile: dict, facts: dict) -> dict[str, list[dict]]:
    """Map each wanted peer to its ordered link specs (possibly empty)."""
    return {name: plan_peer(name, peers.get(name, {}), profile, facts)
            for name in wanted_peers(selector, me, peers)}
