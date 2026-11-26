"""sd_notify: no-op without systemd, READY=1 from 'serve -f' once the node is up."""

import os
import socket
import subprocess
import sys

from messnet import sdnotify


def test_notify_without_systemd(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert not sdnotify.notify("READY=1") and not sdnotify.ready()


def test_ready_sends_and_forgets_the_socket(tmp_path, monkeypatch):
    path = tmp_path / "notify"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        s.bind(str(path))
        monkeypatch.setenv("NOTIFY_SOCKET", str(path))
        assert sdnotify.ready("serving node t")
        assert s.recv(100) == b"READY=1\nSTATUS=serving node t"
        assert "NOTIFY_SOCKET" not in os.environ       # links it starts must not speak for it


def test_serve_foreground_says_ready(tmp_path):
    path = tmp_path / "notify"
    env = {**os.environ, "MESSNET_NODE": "t", "MESSNET_SPOOL": str(tmp_path / "spool"),
           "MESSNET_LOCAL_SPOOL": str(tmp_path / "local"), "MESSNET_DB": str(tmp_path / "db"),
           "MESSNET_STATE": str(tmp_path / "state"), "MESSNET_CONFIG": "", "MESSNET_ETC": str(tmp_path / "etc"),
           "MESSNET_LOG": str(tmp_path / "serve.log"), "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
           "XDG_DATA_HOME": str(tmp_path / "data"), "NOTIFY_SOCKET": str(path)}
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        s.bind(str(path))
        s.settimeout(30)
        proc = subprocess.Popen([sys.executable, "-m", "messnet", "serve", "-f"], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            msg = s.recv(200)
        finally:
            proc.terminate()
            proc.wait(20)
    assert msg.startswith(b"READY=1\nSTATUS=serving node t")
