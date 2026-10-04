"""Edit config.toml: scalar settings and connectivity methods.

A *method* is one way to connect two nodes, of a link kind (tcp, ssh,
cmd, iroh, gonc).

- For this node, a method says how *others reach it*: an entry under
  ``[advertise]`` (published to the peer directory) plus the matching
  serving configuration (``listen`` for tcp, ``iroh.bind`` / ``iroh.relays``,
  ``[[gonc_serve]]`` for gonc).
- For another node, a method says how *this node reaches it*: a
  ``[[links]]`` entry with ``peer = "<node>"``.

Edits keep the file's comments and layout (tomlkit).
"""

import os
from pathlib import Path

import tomlkit
from tomlkit.items import AoT, Array

from messnet.links import ENDPOINT_KINDS, check_spec

# Settable scalar keys and their value types.
SCALARS = {
    "node": str, "etc": "dir", "spool": "dir", "local_spool": "dir", "db": str, "state": "dir",
    "key": str, "secrets": "dir", "gonc_bin": str, "network": str, "poll": float,
    "manage_interval": float, "tcp_auth": bool, "peers": "peers",
}

# Fields that only configure the serving side; never published.
SERVE_ONLY = ("listen", "bind")


class ConfigEditError(ValueError):
    pass


def load(path: Path) -> tomlkit.TOMLDocument:
    path = Path(path)
    return tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()


def save(doc: tomlkit.TOMLDocument, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(tomlkit.dumps(doc))
    tmp.replace(path)


def parse_scalar(key: str, text: str):
    kind = SCALARS.get(key)
    if kind is None:
        raise ConfigEditError(f"not a settable key: {key} (one of {', '.join(SCALARS)})")
    if kind is bool:
        if text.lower() not in ("true", "false", "yes", "no", "1", "0"):
            raise ConfigEditError(f"{key} must be true or false")
        return text.lower() in ("true", "yes", "1")
    if kind is float:
        return float(text)
    if kind == "peers":
        return "*" if text.strip() == "*" else [p.strip() for p in text.split(",") if p.strip()]
    return text


def set_scalar(doc, key: str, text: str) -> object:
    """Set KEY from TEXT; create the directory for directory keys. Return the value."""
    value = parse_scalar(key, text)
    if SCALARS[key] == "dir":
        Path(os.path.expanduser(value)).mkdir(parents=True, exist_ok=True)
    doc[key] = value
    return value


def unset_scalar(doc, key: str) -> bool:
    if key not in SCALARS:
        raise ConfigEditError(f"not a settable key: {key} (one of {', '.join(SCALARS)})")
    if key in doc:
        del doc[key]
        return True
    return False


# --- helpers for arrays of tables -------------------------------------------

def _table(parent, key: str):
    if key not in parent:
        parent[key] = tomlkit.table()
    return parent[key]


def _append(parent, key: str, value: dict) -> None:
    """Append VALUE to the array at PARENT[KEY], keeping its existing style."""
    if key not in parent:
        parent.add(key, tomlkit.aot())
    arr = parent[key]
    if isinstance(arr, AoT):
        item = tomlkit.table()
    elif isinstance(arr, Array):
        item = tomlkit.inline_table()
    else:
        raise ConfigEditError(f"{key} is not a list in the config file")
    item.update(value)
    arr.append(item)


def _clean(fields: dict) -> dict:
    return {k: v for k, v in fields.items() if v not in (None, "", [], False)}


# --- methods ------------------------------------------------------------------

def check_kind(kind: str) -> None:
    if kind not in ENDPOINT_KINDS:
        raise ConfigEditError(f"unknown method kind {kind!r}: one of {', '.join(ENDPOINT_KINDS)}")


def add_own(doc, kind: str, fields: dict) -> dict:
    """Add a way for others to reach this node. FIELDS may include serve-only
    ``listen`` (tcp, gonc tcp) and ``bind`` / ``relays`` (iroh). Returns the
    advertised entry."""
    check_kind(kind)
    fields = _clean(fields)
    listen, bind = fields.pop("listen", None), fields.pop("bind", None)
    relays = fields.pop("relays", None)
    ad = dict(fields)
    if kind == "iroh":
        ad.pop("id", None)                   # filled in from the node key on publish
    else:
        check_spec({"kind": kind, **ad})
    _append(_table(doc, "advertise"), kind, ad)
    if kind == "tcp":
        entry = {"addr": listen or ad["addr"]}
        if ad.get("scope") and ad["scope"] != "all":
            entry["scope"] = ad["scope"]
        if "listen" not in doc:
            doc["listen"] = tomlkit.array()
        doc["listen"].append(entry["addr"] if len(entry) == 1 else _inline(entry))
    elif kind == "iroh":
        iroh = doc["iroh"] if "iroh" in doc else tomlkit.inline_table()
        if bind:
            iroh["bind"] = bind
        if relays:
            iroh["relays"] = relays
        doc["iroh"] = iroh
    elif kind == "gonc":
        serve = {k: v for k, v in ad.items() if k not in ("tailscale", "networks")}
        if listen:
            serve["addr"] = listen
        _append(doc, "gonc_serve", serve)
    return ad


def _inline(d: dict):
    t = tomlkit.inline_table()
    t.update(d)
    return t


def add_link(doc, host: str, kind: str, fields: dict) -> dict:
    """Add a way for this node to reach HOST (a [[links]] entry)."""
    check_kind(kind)
    fields = {k: v for k, v in _clean(fields).items() if k not in SERVE_ONLY + ("tailscale", "networks")}
    spec = {"kind": kind, "peer": host, **fields}
    check_spec(spec)
    _append(doc, "links", spec)
    return spec


def _port(addr: str) -> str:
    return str(addr).rpartition(":")[2]


def remove_own(doc, kind: str, index: int | None) -> list[dict]:
    """Remove advertised KIND entries (1-based INDEX, or all) and their serving config."""
    check_kind(kind)
    adv = doc.get("advertise", {})
    arr = adv.get(kind)
    removed = _remove(arr, index, kind)
    if arr is not None and len(arr) == 0:
        del adv[kind]
    for ad in removed:
        if kind == "tcp" and "listen" in doc:
            port = _port(ad.get("addr", ""))
            keep = [x for x in doc["listen"] if _port(x if isinstance(x, str) else x.get("addr", "")) != port]
            if keep:
                doc["listen"] = tomlkit.array()
                for x in keep:
                    doc["listen"].append(x)
            else:
                del doc["listen"]
        elif kind == "gonc" and "gonc_serve" in doc:
            serves = doc["gonc_serve"]
            for i in reversed(range(len(serves))):
                s = serves[i]
                if s.get("secret") == ad.get("secret") and s.get("mode", "p2p") == ad.get("mode", "p2p"):
                    del serves[i]
            if len(serves) == 0:
                del doc["gonc_serve"]
        elif kind == "iroh" and not adv.get("iroh") and "iroh" in doc:
            del doc["iroh"]
    return removed


def remove_link(doc, host: str, kind: str, index: int | None) -> list[dict]:
    """Remove this node's [[links]] of KIND to HOST (1-based INDEX among them, or all)."""
    check_kind(kind)
    links = doc.get("links")
    if links is None:
        raise ConfigEditError(f"no {kind} links to {host}")
    positions = [i for i, s in enumerate(links) if s.get("peer") == host and s.get("kind") == kind]
    if not positions:
        raise ConfigEditError(f"no {kind} links to {host}")
    chosen = positions if index is None else [_pick(positions, index, kind)]
    removed = [links[i].unwrap() for i in chosen]
    for i in reversed(chosen):
        del links[i]
    if len(links) == 0:
        del doc["links"]
    return removed


def _pick(seq: list, index: int, kind: str):
    if not 1 <= index <= len(seq):
        raise ConfigEditError(f"no {kind} method number {index} (have {len(seq)})")
    return seq[index - 1]


def _remove(arr, index: int | None, kind: str) -> list[dict]:
    if arr is None or len(arr) == 0:
        raise ConfigEditError(f"no {kind} methods to remove")
    chosen = list(range(len(arr))) if index is None else [_pick(list(range(len(arr))), index, kind)]
    removed = [arr[i].unwrap() if hasattr(arr[i], "unwrap") else dict(arr[i]) for i in chosen]
    for i in reversed(chosen):
        del arr[i]
    return removed


def own_methods(cfg: dict) -> dict[str, list[dict]]:
    """Advertised methods of this node by kind, from a plain config dict."""
    adv = cfg.get("advertise", {})
    return {k: (adv[k] if isinstance(adv[k], list) else [adv[k]]) for k in ENDPOINT_KINDS if adv.get(k)}


def links_to(cfg: dict, host: str) -> dict[str, list[dict]]:
    out = {}
    for spec in cfg.get("links", []):
        if spec.get("peer") == host:
            out.setdefault(spec["kind"], []).append(spec)
    return out


def describe(kind: str, ep: dict) -> str:
    """One-line human description of a method entry."""
    skip = {"kind", "peer"}
    main = {"tcp": "addr", "ssh": "target", "cmd": "argv", "iroh": "addrs", "gonc": "mode"}[kind]
    head = ep.get(main, "p2p" if kind == "gonc" else "")
    if isinstance(head, list):
        head = " ".join(str(x) for x in head)
    rest = " ".join(f"{k}={_fmt(v)}" for k, v in ep.items() if k not in skip | {main})
    return f"{head} {rest}".strip()


def _fmt(v) -> str:
    if isinstance(v, list):
        return ",".join(str(x) for x in v)
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v)


def methods_report(cfg: dict, me: str, host: str, published: dict | None) -> list[str]:
    """Lines describing HOST's methods: this node's advertised + serving config,
    or for another node its published endpoints plus our local links to it."""
    lines = []
    if host == me:
        lines.append(f"{me} (this node): others reach it by")
        own = own_methods(cfg)
        lines += _numbered(own) or ["  (none advertised)"]
        serving = []
        if cfg.get("listen"):
            serving.append("listen " + " ".join(x if isinstance(x, str) else x.get("addr", "?")
                                                 for x in cfg["listen"]))
        if cfg.get("iroh"):
            serving.append("iroh " + " ".join(f"{k}={_fmt(v)}" for k, v in cfg["iroh"].items()))
        for s in cfg.get("gonc_serve", []):
            serving.append("gonc_serve " + describe("gonc", s))
        if serving:
            lines.append("serving:")
            lines += [f"  {s}" for s in serving]
        dials = [s for s in cfg.get("links", [])]
        if dials:
            lines.append("links this node dials:")
            lines += [f"  {s['kind']:4} -> {s.get('peer', '?')}: {describe(s['kind'], s)}" for s in dials]
    else:
        lines.append(f"{host}: published methods (from its peer file)")
        if published is None:
            lines.append("  (no peer file for this node)")
        else:
            pub = {k: published[k] for k in ENDPOINT_KINDS if published.get(k)}
            lines += [f"  {k:4} {describe(k, ep)}" for k, eps in pub.items() for ep in eps] or ["  (none)"]
        lines.append(f"local links: this node reaches {host} by")
        lines += _numbered(links_to(cfg, host)) or ["  (none)"]
    return lines


def _numbered(by_kind: dict[str, list[dict]]) -> list[str]:
    return [f"  {kind:4} {i}  {describe(kind, ep)}"
            for kind, eps in by_kind.items() for i, ep in enumerate(eps, 1)]


def new_secret(secrets_dir: Path, name: str) -> Path:
    """Create a strong random secret file (mode 0600) named NAME."""
    import base64
    if "/" in name or name.startswith("."):
        raise ConfigEditError(f"secret must be a plain file name: {name!r}")
    path = Path(os.path.expanduser(secrets_dir)) / name
    if path.exists():
        raise ConfigEditError(f"secret already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fp:
        fp.write(base64.b64encode(os.urandom(24)).decode())
    return path
