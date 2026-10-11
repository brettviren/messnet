"""messnet install / uninstall / install --list, with fake crontab, systemctl and loginctl
executables on PATH so nothing real is touched."""

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from messnet import install, lock
from test_cli import run

FAKE_CRONTAB = """#!/bin/sh
f="$FAKE/crontab"
echo "crontab $*" >> "$FAKE/calls"
case "$1" in
  -l) if [ -f "$f" ]; then cat "$f"; else echo "no crontab for test" >&2; exit 1; fi ;;
  -r) rm -f "$f" ;;
  -)  cat > "$f" ;;
esac
"""

FAKE_SYSTEMCTL = """#!/bin/sh
d="$FAKE/sd"; mkdir -p "$d"
shift; shift                       # --user --no-pager
cmd="$1"; unit="$2"
echo "systemctl $*" >> "$FAKE/calls"
case "$cmd" in
  is-enabled) if [ -f "$d/enabled" ]; then echo enabled; else echo disabled; exit 1; fi ;;
  is-active)  if [ -f "$d/active" ]; then echo active; else echo inactive; exit 3; fi ;;
  enable)     touch "$d/enabled" ;;
  start|restart) touch "$d/active" ;;
  disable)    rm -f "$d/enabled" "$d/active" ;;
  daemon-reload) ;;
esac
"""

FAKE_LOGINCTL = """#!/bin/sh
echo "loginctl $*" >> "$FAKE/calls"
case "$1" in
  show-user) cat "$FAKE/linger" 2>/dev/null || echo no ;;
  enable-linger)
    if [ -f "$FAKE/linger_denied" ]; then echo "Access denied" >&2; exit 1; fi
    echo yes > "$FAKE/linger" ;;
esac
"""


class Fake:
    def __init__(self, root: Path):
        self.root = root
        self.calls_file = root / "calls"

    @property
    def crontab(self) -> str | None:
        f = self.root / "crontab"
        return f.read_text() if f.exists() else None

    def set_crontab(self, text: str) -> None:
        (self.root / "crontab").write_text(text)

    def calls(self, prefix: str = "") -> list[str]:
        lines = self.calls_file.read_text().splitlines() if self.calls_file.exists() else []
        return [line for line in lines if line.startswith(prefix)]

    def mutating(self) -> list[str]:
        """Calls that change something (everything except queries)."""
        quiet = ("crontab -l", "systemctl is-", "loginctl show-user")
        return [c for c in self.calls() if not c.startswith(quiet)]

    def forget_calls(self) -> None:
        self.calls_file.unlink(missing_ok=True)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    root = tmp_path / "fake"
    bindir = root / "bin"
    bindir.mkdir(parents=True)
    for name, body in (("crontab", FAKE_CRONTAB), ("systemctl", FAKE_SYSTEMCTL), ("loginctl", FAKE_LOGINCTL)):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    monkeypatch.setenv("FAKE", str(root))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("MESSNET_CONFIG", raising=False)
    return Fake(root)


@pytest.fixture
def exe(tmp_path):
    path = tmp_path / "opt" / "bin" / "messnet"
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return str(path)


def unit_file(tmp_path) -> Path:
    return tmp_path / "cfg" / "systemd" / "user" / "messnet.service"       # the runner sets XDG_CONFIG_HOME=cfg


# ---- pure pieces ----------------------------------------------------------------------

def test_merge_block_replaces_in_place_and_roundtrips():
    block = ["# BEGIN messnet serve (x)", "@reboot a", "# END messnet serve"]
    other = "MAILTO=me\n0 3 * * * backup\n"
    assert install.merge_block("", block) == "\n".join(block) + "\n"
    added = install.merge_block(other, block)
    assert added == other + "\n".join(block) + "\n"
    assert install.merge_block(added, None) == other
    newer = ["# BEGIN messnet serve (y)", "@reboot b", "# END messnet serve"]
    middle = "MAILTO=me\n" + "\n".join(block) + "\n0 3 * * * backup\n"
    assert install.merge_block(middle, newer) == "MAILTO=me\n" + "\n".join(newer) + "\n0 3 * * * backup\n"
    two = "\n".join(block) + "\nx\n" + "\n".join(block) + "\n"
    assert install.merge_block(two, newer).count("BEGIN messnet serve") == 1
    with pytest.raises(install.InstallError, match="without"):
        install.merge_block("# BEGIN messnet serve\n@reboot a\n", block)


def test_cron_block_quotes_and_escapes():
    svc = install.Service("/opt/my tools/messnet", ("-c", "/home/a b/50%.toml"))
    entry = install.cron_block(svc, 5)[1]
    assert entry.startswith("@reboot PATH=") and r"50\%.toml" in entry and "'/opt/my tools/messnet'" in entry
    assert install.cron_block(svc, 5)[2].startswith("*/5 * * * * ")
    assert entry.endswith(" serve")


def test_global_args(monkeypatch, tmp_path):
    monkeypatch.delenv("MESSNET_CONFIG", raising=False)
    assert install.global_args() == ()
    got = install.global_args(config_path="c.toml", node="n", db="d.db", network="home")
    assert got == ("-c", os.path.abspath("c.toml"), "--node", "n", "--db", os.path.abspath("d.db"), "-n", "home")
    monkeypatch.setenv("MESSNET_CONFIG", str(tmp_path / "e.toml"))
    assert install.global_args() == ("-c", str(tmp_path / "e.toml"))


def test_default_exe(monkeypatch, tmp_path, exe):
    monkeypatch.setattr(sys, "argv", [exe, "install"])
    assert install.default_exe() == exe
    monkeypatch.setattr(sys, "argv", ["/nonexistent/pytest"])
    monkeypatch.setenv("PATH", str(Path(exe).parent))
    assert install.default_exe() == exe
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(install.InstallError, match="--exe"):
        install.default_exe()
    with pytest.raises(install.InstallError, match="not an executable"):
        install.resolve_exe(str(tmp_path / "nope"))


def test_unit_quoting():
    unit = install.render_unit(install.Service("/opt/a b/messnet", ("--node", "50%$x")))
    assert 'ExecStart="/opt/a b/messnet" --node 50%%$$x serve\n' in unit
    assert unit.startswith(install.UNIT_MARK) and "WantedBy=default.target" in unit


# ---- cron -----------------------------------------------------------------------------

def test_cron_install_is_idempotent(tmp_path, fake, exe):
    out = run(tmp_path, "install", "cron", "--exe", exe)
    assert "cron: installed" in out
    text = fake.crontab
    assert text.count("BEGIN messnet serve") == 1
    assert f"@reboot PATH={Path(exe).parent}:" in text and f"{exe} serve" in text
    assert "*/10 * * * * " in text
    writes = fake.mutating()
    assert len(writes) == 1
    out = run(tmp_path, "install", "cron", "--exe", exe)
    assert "already installed" in out
    assert fake.crontab == text and fake.mutating() == writes        # no second write


def test_cron_keeps_other_entries_and_uninstall_restores(tmp_path, fake, exe):
    original = "MAILTO=me@example.org\n# backups\n30 2 * * * /home/me/scripts/backup -q full\n"
    fake.set_crontab(original)
    run(tmp_path, "install", "cron", "--exe", exe, "--interval", "5")
    text = fake.crontab
    assert text.startswith(original) and "*/5 * * * *" in text
    run(tmp_path, "install", "cron", "--exe", exe, "--interval", "15")      # converges, in place
    assert fake.crontab.count("BEGIN messnet serve") == 1 and "*/15 * * * *" in fake.crontab
    assert fake.crontab.startswith(original)
    assert "removed" in run(tmp_path, "uninstall", "cron")
    assert fake.crontab == original


def test_cron_uninstall_when_absent_is_a_noop(tmp_path, fake):
    assert "not installed" in run(tmp_path, "uninstall", "cron")           # no crontab at all
    assert fake.mutating() == []
    fake.set_crontab("0 * * * * x\n")
    assert "not installed" in run(tmp_path, "uninstall", "cron")
    assert fake.crontab == "0 * * * * x\n" and fake.mutating() == []


def test_cron_removes_the_crontab_it_created(tmp_path, fake, exe):
    run(tmp_path, "install", "cron", "--exe", exe)
    run(tmp_path, "uninstall", "cron")
    assert fake.crontab is None


def test_cron_damaged_block_is_an_error(tmp_path, fake, exe):
    fake.set_crontab("# BEGIN messnet serve\n@reboot x\n")
    out = run(tmp_path, "install", "cron", "--exe", exe, codes=(1,))
    assert "without" in out and fake.mutating() == []


def test_cron_passes_global_options_on(tmp_path, fake, exe):
    cfg = tmp_path / "my.toml"
    cfg.write_text("")
    run(tmp_path, "-c", str(cfg), "--node", "box.me", "install", "cron", "--exe", exe)
    assert f"{exe} -c {cfg} --node box.me serve" in fake.crontab


# ---- systemd --------------------------------------------------------------------------

def test_systemd_install_is_idempotent(tmp_path, fake, exe):
    out = run(tmp_path, "install", "systemd", "--exe", exe)
    for word in ("wrote", "enabled messnet.service", "started", "enabled lingering"):
        assert word in out
    text = unit_file(tmp_path).read_text()
    assert f"ExecStart={exe} serve\n" in text and "WantedBy=default.target" in text
    assert "systemctl daemon-reload" in fake.calls()
    assert "systemctl enable messnet.service" in fake.calls()
    assert "systemctl start messnet.service" in fake.calls()
    assert any(c.startswith("loginctl enable-linger") for c in fake.calls())
    fake.forget_calls()
    out = run(tmp_path, "install", "systemd", "--exe", exe)
    assert "already installed" in out
    assert fake.mutating() == []                                              # queries only
    assert unit_file(tmp_path).read_text() == text


def test_systemd_changed_command_rewrites_and_restarts(tmp_path, fake, exe):
    run(tmp_path, "install", "systemd", "--exe", exe)
    fake.forget_calls()
    (tmp_path / "x.toml").write_text("")
    out = run(tmp_path, "-c", str(tmp_path / "x.toml"), "install", "systemd", "--exe", exe)
    assert "wrote" in out and "restarted" in out and "started" not in out.replace("restarted", "")
    assert "x.toml" in unit_file(tmp_path).read_text()
    assert [c for c in fake.mutating()] == ["systemctl daemon-reload",
                                            "systemctl restart messnet.service"]


def test_systemd_lingering_denied_warns_but_installs(tmp_path, fake, exe):
    (fake.root / "linger_denied").write_text("")
    res = run(tmp_path, "install", "systemd", "--exe", exe)
    assert unit_file(tmp_path).exists() and "started" in res
    assert "lingering is off" in res and "sudo loginctl enable-linger" in res       # on stderr, mixed in by the runner
    run(tmp_path, "install", "systemd", "--exe", exe)                             # rerun tries again
    assert len(fake.calls("loginctl enable-linger")) == 2


def test_systemd_does_not_start_when_a_service_is_already_running(tmp_path, fake, exe):
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "messnet"])
    try:
        lock_file = lock.lock_path(tmp_path / "db")
        lock_file.write_text(f"{holder.pid}\n")
        out = run(tmp_path, "install", "systemd", "--exe", exe)
        assert "already running" in out and "start messnet.service" not in "\n".join(fake.calls())
        listing = run(tmp_path, "install", "--list")
        assert f"running: messnet serve is running (pid {holder.pid})" in listing
    finally:
        holder.kill()
        holder.wait()


def test_systemd_foreign_unit_is_never_touched(tmp_path, fake, exe):
    path = unit_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("[Service]\nExecStart=/bin/true\n")
    assert "was not made by" in run(tmp_path, "install", "systemd", "--exe", exe, codes=(1,))
    assert "left alone" in run(tmp_path, "uninstall", "systemd")
    assert path.read_text() == "[Service]\nExecStart=/bin/true\n" and fake.mutating() == []


def test_systemd_uninstall(tmp_path, fake, exe):
    run(tmp_path, "install", "systemd", "--exe", exe)
    fake.forget_calls()
    out = run(tmp_path, "uninstall", "systemd")
    assert "removed" in out and "lingering was left" in out
    assert not unit_file(tmp_path).exists()
    assert fake.mutating() == ["systemctl disable --now messnet.service",
                               "systemctl daemon-reload"]
    fake.forget_calls()
    assert "not installed" in run(tmp_path, "uninstall", "systemd")           # second time: no-op
    assert fake.mutating() == []


# ---- --list and usage -----------------------------------------------------------------

def test_list(tmp_path, fake, exe):
    res = run(tmp_path, "install", "--list")
    assert res.strip() == "messnet: nothing installed"
    run(tmp_path, "install", "cron", "--exe", exe)
    run(tmp_path, "install", "systemd", "--exe", exe)
    out = run(tmp_path, "install", "--list")
    assert "cron: crontab entries:" in out and "  @reboot PATH=" in out and "  */10 * * * * PATH=" in out
    assert f"systemd: unit file: {unit_file(tmp_path)}" in out
    assert f"ExecStart={exe} serve" in out and "enabled, active" in out and "lingering: yes" in out
    run(tmp_path, "uninstall", "cron")
    assert "cron:" not in run(tmp_path, "install", "--list")


def test_usage_errors(tmp_path, fake):
    assert "Usage" in run(tmp_path, "install", codes=(2,))
    assert "TARGET" in run(tmp_path, "install", "--interval", "5", codes=(2,))
    assert "TARGET" in run(tmp_path, "install", "cron", "--list", codes=(2,))
    assert "Usage" in run(tmp_path, "uninstall", codes=(2,))
    assert "Invalid value" in run(tmp_path, "install", "launchd", codes=(2,))
    assert "Invalid value" in run(tmp_path, "install", "cron", "--interval", "99", codes=(2,))


def test_missing_tools_are_reported(tmp_path, monkeypatch, exe):
    monkeypatch.setenv("PATH", "/nonexistent")
    assert "crontab command not found" in run(tmp_path, "install", "cron", "--exe", exe, codes=(1,))


def test_uninstall_needs_no_config_file(tmp_path, fake):
    cfg = tmp_path / "missing.toml"
    out = run(tmp_path, "-c", str(cfg), "uninstall", "cron")
    assert "not installed" in out


# ---- the single-instance lock ---------------------------------------------------------

def test_lock_is_exclusive_and_released(tmp_path):
    path = tmp_path / "x" / "store.db.serve.lock"
    with lock.single_instance(path) as first:
        assert first
        assert lock.running_pid(path) is None or lock.running_pid(path) == os.getpid()
        with lock.single_instance(path) as second:
            assert not second
    with lock.single_instance(path) as again:
        assert again
    assert path.read_text() == ""


def test_serve_does_nothing_when_one_is_running(tmp_path):
    """A second 'serve' (what cron does every few minutes) returns at once, successfully."""
    def timed_out(*_):
        raise AssertionError("serve did not notice the running instance")
    signal.signal(signal.SIGALRM, timed_out)
    signal.alarm(20)
    try:
        with lock.single_instance(lock.lock_path(tmp_path / "db")) as owner:
            assert owner
            assert run(tmp_path, "serve") == ""
            assert run(tmp_path, "run") == ""
            assert run(tmp_path, "serve", "-l", "127.0.0.1:0") == ""
    finally:
        signal.alarm(0)
