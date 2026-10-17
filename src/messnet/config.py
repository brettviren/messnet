"""XDG-located, layered configuration: defaults → environment → config file → CLI."""

import getpass
import os
import socket
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


def _xdg(var: str, fallback: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / fallback)


def default_node() -> str:
    """Default node name: short hostname dot user."""
    return f"{socket.gethostname().split('.')[0]}.{getpass.getuser()}"


def default_config_path() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "messnet" / "config.toml"


@dataclass
class Config:
    node: str = field(default_factory=default_node)
    spool: Path = field(default_factory=lambda: _xdg("XDG_DATA_HOME", ".local/share") / "messnet" / "spool")
    local_spool: Path = field(default_factory=lambda: _xdg("XDG_STATE_HOME", ".local/state") / "messnet" / "spool")
    db: Path = field(default_factory=lambda: _xdg("XDG_DATA_HOME", ".local/share") / "messnet" / "messnet.db")
    state: Path = field(default_factory=lambda: _xdg("XDG_CACHE_HOME", ".cache") / "messnet" / "state")
    # Syncthing-shared directory holding peers/*.toml and networks/*.toml.
    etc: Path = field(default_factory=lambda: _xdg("XDG_DATA_HOME", ".local/share") / "messnet" / "etc")
    # Node identity key (ed25519 seed); the public key is the node / iroh id.
    key: Path = field(default_factory=lambda: _xdg("XDG_DATA_HOME", ".local/share") / "messnet" / "node.key")
    # iroh transport: bind ("HOST:PORT"), relays (list of URLs; empty disables relaying),
    # listen (accept iroh links), scope (widest scope of inbound iroh links).
    iroh: dict = field(default_factory=dict)
    # Local (never synced) directory of secret files, e.g. gonc keys.
    secrets: Path = field(default_factory=lambda: _xdg("XDG_CONFIG_HOME", ".config") / "messnet" / "secrets")
    gonc_bin: str = "gonc"
    # gonc responders this node runs (see messnet.gonc).
    gonc_serve: list[dict] = field(default_factory=list)
    # Path of the loaded config file, if any (not a config key).
    path: Path | None = field(default=None, repr=False)
    # (path argument, overrides) this was loaded with, so a running service can reload the
    # file the same way (not a config key).
    load_args: tuple | None = field(default=None, repr=False)
    # Pure consumers of events (the concentrator): each entry is {kind = "unix", path = ..., scope = ...}
    # or {kind = "tcp", addr = "HOST:PORT", scope = ...}.  The default is one unix socket; [] for none.
    publish: list = field(default_factory=lambda: [{"kind": "unix"}])
    # One file, overwritten with the newest event to arrive; None for none.
    latest: Path | None = field(default_factory=lambda: _xdg("XDG_CACHE_HOME", ".cache") / "messnet" / "latest.json")
    # `serve` / `run` log here (rotating) at log_level: off, error, warning, info or debug.
    log: Path = field(default_factory=lambda: _xdg("XDG_CACHE_HOME", ".cache") / "messnet" / "serve.log")
    log_level: str = "info"
    # Require the signed handshake on TCP links (in and out).
    tcp_auth: bool = True
    poll: float = 0.25
    listen: list = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    # Peers the link manager keeps linked: list of node names, or "*" for all known.
    peers: list[str] | str = field(default_factory=list)
    # Endpoints this node advertises in its peer file, by link kind.
    advertise: dict = field(default_factory=dict)
    # Force a network profile name instead of detecting one.
    network: str | None = None
    manage_interval: float = 30.0
    # Settings of 'messnet integrate TARGET', as the table [integrate.TARGET] (see integrate_claude).
    integrate: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {f.name: (str(v) if isinstance(v := getattr(self, f.name), Path) else v)
                for f in fields(self) if f.name not in ("path", "load_args")}


_PATHS = {"spool", "local_spool", "db", "state", "etc", "key", "secrets", "log", "latest"}
_ENV_KEYS = ("node", "spool", "local_spool", "db", "state", "etc", "key", "poll", "network", "log", "log_level")
LOG_LEVELS = ("off", "error", "warning", "info", "debug")


def _coerce(key: str, value):
    if key == "latest" and str(value).lower() in ("", "off", "none", "false"):
        return None
    if key in _PATHS:
        return Path(value).expanduser()
    if key in ("poll", "manage_interval"):
        return float(value)
    if key == "log_level":
        if str(value).lower() not in LOG_LEVELS:
            raise ValueError(f"log_level must be one of {LOG_LEVELS}: {value!r}")
        return str(value).lower()
    return value


def _apply(cfg: Config, values: dict) -> None:
    known = {f.name for f in fields(Config)}
    for key, value in values.items():
        if value is None:
            continue
        if key not in known:
            raise ValueError(f"unknown configuration key: {key}")
        setattr(cfg, key, _coerce(key, value))


def config_file(path: str | Path | None = None) -> Path:
    """The config file to read / edit: PATH, else $MESSNET_CONFIG, else the XDG default."""
    explicit = path or os.environ.get("MESSNET_CONFIG")
    return Path(explicit).expanduser() if explicit else default_config_path()


def load_config(path: str | Path | None = None, must_exist: bool = True, **overrides) -> Config:
    """Build the effective configuration.

    The config file is PATH, else $MESSNET_CONFIG, else the XDG default
    location; a missing default file is not an error.  Keyword overrides
    (from the CLI) win last.  None-valued overrides are ignored.
    """
    cfg = Config()
    _apply(cfg, {k: os.environ.get(f"MESSNET_{k.upper()}") for k in _ENV_KEYS})
    explicit = path or os.environ.get("MESSNET_CONFIG")
    cpath = Path(explicit).expanduser() if explicit else default_config_path()
    if cpath.exists():
        with open(cpath, "rb") as fp:
            values = tomllib.load(fp)
        values.pop("path", None)
        _apply(cfg, values)
        cfg.path = cpath
    elif explicit and must_exist:
        raise FileNotFoundError(f"config file not found: {cpath}")
    _apply(cfg, overrides)
    cfg.load_args = (path, dict(overrides))
    return cfg


# --- reloading a running service's configuration ---------------------------------------------

# Settings a running service picks up when the file changes.  The others (node name and the
# directories and files of the store) are opened once; changing them takes a restart.
RELOADABLE = ("listen", "links", "peers", "advertise", "gonc_serve", "gonc_bin", "secrets", "tcp_auth",
              "poll", "manage_interval", "network", "iroh", "log", "log_level", "publish", "latest")
# Of iroh's settings, these are fixed once the endpoint is open.
IROH_FIXED = ("bind", "relays")


def source_file(cfg: Config) -> Path:
    """The config file CFG was (or would be) loaded from."""
    return config_file(cfg.load_args[0] if cfg.load_args else None)


def reload_config(cfg: Config) -> Config:
    """Load the config file again the way CFG was loaded (same overrides)."""
    path, overrides = cfg.load_args
    return load_config(path, must_exist=True, **overrides)


def merge_reloadable(cfg: Config, new: Config) -> tuple[list[str], list[str]]:
    """Copy NEW's reloadable settings into CFG in place.

    Return (changed settings, settings that differ but need a restart and were left alone).
    """
    changed, ignored = [], []
    for key in RELOADABLE:
        old, value = getattr(cfg, key), getattr(new, key)
        if key == "iroh":
            value = dict(value)
            for fixed in IROH_FIXED:
                if old.get(fixed) != value.get(fixed):
                    ignored.append(f"iroh.{fixed}")
                    value.pop(fixed, None)
                    if fixed in old:
                        value[fixed] = old[fixed]
        if value != old:
            changed.append(key)
            setattr(cfg, key, value)
    for f in fields(Config):
        if f.name not in RELOADABLE and f.name not in ("path", "load_args", "integrate") and getattr(cfg, f.name) != getattr(new, f.name):
            ignored.append(f.name)
    return changed, ignored
