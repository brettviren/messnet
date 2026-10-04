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

    def as_dict(self) -> dict:
        return {f.name: (str(v) if isinstance(v := getattr(self, f.name), Path) else v)
                for f in fields(self)}


_PATHS = {"spool", "local_spool", "db", "state", "etc"}
_ENV_KEYS = ("node", "spool", "local_spool", "db", "state", "etc", "poll", "network")


def _coerce(key: str, value):
    if key in _PATHS:
        return Path(value).expanduser()
    if key in ("poll", "manage_interval"):
        return float(value)
    return value


def _apply(cfg: Config, values: dict) -> None:
    known = {f.name for f in fields(Config)}
    for key, value in values.items():
        if value is None:
            continue
        if key not in known:
            raise ValueError(f"unknown configuration key: {key}")
        setattr(cfg, key, _coerce(key, value))


def load_config(path: str | Path | None = None, **overrides) -> Config:
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
            _apply(cfg, tomllib.load(fp))
    elif explicit:
        raise FileNotFoundError(f"config file not found: {cpath}")
    _apply(cfg, overrides)
    return cfg
