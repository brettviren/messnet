import pytest

from messnet.config import Config
from messnet.node import Node


@pytest.fixture
def make_node(tmp_path):
    """Factory for isolated nodes; NAME nodes share SPOOL if given (simulating Syncthing)."""
    nodes = []

    def make(name: str, spool=None, poll: float = 0.02) -> Node:
        base = tmp_path / name
        cfg = Config(node=name, spool=spool or base / "spool", local_spool=base / "local",
                     db=base / "messnet.db", state=base / "state", poll=poll,
                     key=base / "node.key", etc=tmp_path / "etc")
        node = Node(cfg)
        nodes.append(node)
        return node

    yield make
    for node in nodes:
        node.close()
