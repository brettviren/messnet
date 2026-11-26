"""The built-in schemas for the events dee emits: dee.sync.v1 and systemd.unit.failed.v1.

Samples are what dee (src/dee/notify.py) sends.
"""

from pathlib import Path

import pytest

from messnet.schema import Schemas, check_file

SCHEMA_DIR = Path(__file__).parent.parent / "src" / "messnet" / "schemas"

SYNC = {"host": "haiku", "config": "/home/bv/.config/dee/config.toml",
        "actions": ["start syncthing.service", "mask ssh-agent.socket"], "files": 7, "errors": []}
FAILED = {"host": "yobox", "unit": "emacs@org-mcp.service", "result": "exit-code", "status": 255,
          "restarts": 4, "log": ["emacs: unknown option '--modules'"]}


def ev(type, data, subject="x"):
    return {"specversion": "1.0", "id": "01J0000000000000000000000", "source": "messnet://t",
            "type": type, "origin": "t", "seq": 1, "subject": subject, "data": data}


def problems(type, data, subject="x"):
    return Schemas().problems(ev(type, data, subject))


@pytest.mark.parametrize("name", ["dee.sync.v1", "systemd.unit.failed.v1"])
def test_lint_and_builtin(name):
    assert check_file(SCHEMA_DIR / f"{name}.json") == []
    assert name in Schemas().types()


def test_valid():
    assert problems("dee.sync.v1", SYNC, "haiku") == []
    assert problems("dee.sync.v1", {**SYNC, "errors": ["start x.service: failed"]}, "haiku") == []
    assert problems("systemd.unit.failed.v1", FAILED, "emacs@org-mcp.service") == []
    unknown = {**FAILED, "result": "", "status": None, "restarts": None, "log": []}
    assert problems("systemd.unit.failed.v1", unknown, "emacs@org-mcp.service") == []


def test_invalid():
    assert problems("dee.sync.v1", {k: v for k, v in SYNC.items() if k != "actions"})
    assert problems("dee.sync.v1", {**SYNC, "files": "7"})
    assert problems("systemd.unit.failed.v1", {**FAILED, "status": "255"})     # dee used to send strings
    assert problems("systemd.unit.failed.v1", {**FAILED, "restarts": -1})
    assert problems("dee.sync.v1", SYNC, subject=None)
