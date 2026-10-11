"""'messnet serve' as a daemon, with real processes: detaching, idempotence, -k, -f, -d."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from messnet import lock


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Node:
    """A messnet configuration in a temp dir, and ways to run the command line against it."""

    def __init__(self, tmp: Path, top: str = "", tail: str = ""):
        self.tmp = tmp
        self.config = tmp / "config.toml"
        self.log = tmp / "serve.log"
        self.config.write_text(f'''node = "d.test"
spool = "{tmp}/spool"
local_spool = "{tmp}/local"
db = "{tmp}/db"
state = "{tmp}/state"
etc = "{tmp}/etc"
key = "{tmp}/key"
log = "{self.log}"
poll = 0.05
{top}
{tail}''')
        self.lockfile = lock.lock_path(tmp / "db")

    def argv(self, *args):
        return [sys.executable, "-m", "messnet", "-c", str(self.config), *args]

    def run(self, *args, timeout=60):
        return subprocess.run(self.argv(*args), capture_output=True, text=True, timeout=timeout)

    def popen(self, *args):
        return subprocess.Popen(self.argv(*args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def pid(self):
        return lock.running_pid(self.lockfile)

    def wait_for(self, cond, timeout=15.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def log_text(self):
        return self.log.read_text() if self.log.exists() else ""


@pytest.fixture
def node(tmp_path):
    n = Node(tmp_path)
    yield n
    pid = lock.running_pid(n.lockfile)             # never leave a daemon behind
    if pid:
        os.kill(pid, signal.SIGKILL)


def listening(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


def test_serve_starts_a_detached_daemon_and_is_idempotent(tmp_path):
    port = free_port()
    node = Node(tmp_path, f'listen = ["127.0.0.1:{port}"]')
    try:
        res = node.run("serve")
        assert res.returncode == 0 and res.stdout == "" and res.stderr == ""     # silent, like cron wants
        pid = node.pid()
        assert pid and pid != os.getpid()
        assert node.wait_for(lambda: listening(port))                            # really serving
        time.sleep(0.3)                                                          # let it log our probe
        # detached: not our child, its own session, no controlling terminal, streams on /dev/null
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        ppid, sid, tty = int(stat[1]), int(stat[3]), int(stat[4])
        assert ppid != os.getpid() and sid != os.getsid(0) and sid != pid and tty == 0
        for fd in (0, 1, 2):
            assert os.readlink(f"/proc/{pid}/fd/{fd}") == "/dev/null"
        assert "service starting" in node.log_text() and f"pid {pid}" in node.log_text()

        before = node.log_text()                                                 # after our own probe above
        for _ in range(3):                                                       # cron, over and over
            again = node.run("serve")
            assert again.returncode == 0 and again.stdout == "" and again.stderr == ""
        assert node.pid() == pid                                                 # same daemon
        assert node.log_text() == before                                         # and nothing logged

        stopped = node.run("serve", "-k")
        assert stopped.returncode == 0 and f"stopped the daemon (pid {pid})" in stopped.stdout
        assert node.pid() is None and not listening(port)
        assert "service stopped" in node.log_text()
        again = node.run("serve", "-k")                                          # nothing to stop: fine
        assert again.returncode == 0 and "no daemon is running" in again.stdout
    finally:
        node.run("serve", "-k")


def test_serve_recovers_after_a_hard_kill(node):
    assert node.run("serve").returncode == 0
    pid = node.pid()
    os.kill(pid, signal.SIGKILL)                                                 # no chance to clean up
    assert node.wait_for(lambda: node.pid() is None)
    assert node.lockfile.read_text().strip() == str(pid)                         # stale pid left behind
    assert node.run("serve", "-k").stdout.strip().endswith("no daemon is running")   # not fooled by it
    assert node.run("serve").returncode == 0
    assert node.pid() not in (None, pid)


def test_serve_reports_a_startup_failure_and_leaves_nothing_running(tmp_path):
    node = Node(tmp_path, tail='[[links]]\nkind = "carrier-pigeon"\n')
    res = node.run("serve")
    assert res.returncode == 1 and "carrier-pigeon" in res.stderr and res.stdout == ""
    assert node.pid() is None
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    other = tmp_path / "other"
    other.mkdir()
    bad = Node(other)
    bad.config.write_text(bad.config.read_text().replace(f'db = "{other}/db"', f'db = "{blocker}/db"'))
    res = bad.run("serve")
    assert res.returncode == 1 and "cannot open the event store" in res.stderr


def test_foreground_stays_logs_to_the_file_and_stops_on_sigterm(node):
    proc = node.popen("serve", "--foreground")
    try:
        assert node.wait_for(lambda: "started spool and state upkeep" in node.log_text())
        assert proc.poll() is None and node.pid() == proc.pid                    # the process we started
        other = node.run("serve", "--foreground")                                # a second one: nothing to do
        assert other.returncode == 0 and other.stdout == ""
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=15) == 0
        assert "service stopped" in node.log_text() and node.pid() is None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate()


def test_debug_runs_in_the_foreground_with_everything_on_the_screen(node):
    proc = node.popen("serve", "-d")
    try:
        assert node.wait_for(lambda: os.path.exists(node.lockfile) and node.pid() == proc.pid)
        busy = node.run("serve", "-d")                                           # a person is told
        assert busy.returncode == 1 and f"already running (pid {proc.pid})" in busy.stderr and "serve -k" in busy.stderr
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=15)
        assert proc.returncode == 0
        assert "service starting" in err and "DEBUG" in err or "INFO" in err      # the screen got the logging
        assert not node.log.exists()                                             # and no file was made
    finally:
        if proc.poll() is None:
            proc.kill()


def test_kill_can_stop_a_foreground_service_too(node):
    proc = node.popen("run")
    try:
        assert node.wait_for(lambda: node.pid() == proc.pid)
        assert node.run("serve", "-k").returncode == 0
        assert proc.wait(timeout=15) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate()


def test_kill_does_not_combine_with_other_options(node):
    res = node.run("serve", "-k", "-d")
    assert res.returncode == 2 and "cannot be combined" in res.stderr


def test_lock_holder_is_the_process_that_really_holds_it(tmp_path):
    path = tmp_path / "x.lock"
    code = ("import sys,time\nfrom pathlib import Path\nfrom messnet import lock\n"
            "with lock.single_instance(Path(sys.argv[1])) as o:\n    print(o, flush=True); time.sleep(60)\n")
    holder = subprocess.Popen([sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "True"
        assert lock.holder_pid(path) == holder.pid and lock.running_pid(path) == holder.pid
        # an unrelated process whose pid is in the file is not mistaken for the holder
        other = subprocess.Popen(["sleep", "60"])
        try:
            path.write_text(f"{other.pid}\n")
            assert lock.running_pid(path) == holder.pid
        finally:
            other.kill()
            other.wait()
        holder.kill()
        holder.wait()
        assert lock.holder_pid(path) == 0 and lock.running_pid(path) is None
    finally:
        holder.kill()
        holder.wait()
