"""Install, list and remove the entries that keep ``messnet serve`` running.

Targets (all per user, no root needed):

``cron``     a marker-delimited block in the user's crontab: ``@reboot`` plus a periodic
             check.  cron runs these with no login at all, so the node starts at boot.
``systemd``  a user unit ``~/.config/systemd/user/messnet.service`` enabled for
             ``default.target``, plus *lingering* so the user's service manager (and with it
             the node) starts at boot instead of at first login, whether that login is
             graphical, on the console or only over ssh.

Everything here is idempotent: each step first looks at what is already in place and does
only what is missing.  The entries are too: ``messnet serve`` holds a lock (see ``lock``),
so a second start is a harmless no-op, and cron or systemd may fire it as often as they like.
cron runs ``messnet serve``, which starts the daemon and returns; systemd runs
``messnet serve --foreground``, because it wants to supervise the process it started.
"""

import getpass
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from messnet import lock

DEFAULT_INTERVAL = 10
BEGIN = "# BEGIN messnet serve"
END = "# END messnet serve"
UNIT = "messnet.service"
UNIT_MARK = "# Managed by 'messnet install systemd'"


class InstallError(Exception):
    pass


@dataclass
class Outcome:
    """What an install / uninstall did: CHANGED if it altered anything, NOTES say what."""
    changed: bool = False
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Service:
    """The command to keep running: EXE, any global options ARGS, then ``serve``."""
    exe: str
    args: tuple[str, ...] = ()

    def argv(self, foreground: bool = False) -> list[str]:
        """``serve`` starts a daemon and returns, which is right for cron; systemd wants the
        process it started to stay, so its unit asks for ``--foreground``."""
        return [self.exe, *self.args, "serve", *(["--foreground"] if foreground else [])]


def default_exe() -> str:
    """Absolute path of the messnet executable being run, else the one on PATH."""
    me = Path(sys.argv[0])
    if me.name.startswith("messnet") and me.is_file() and os.access(me, os.X_OK):
        return os.path.abspath(me)
    found = shutil.which("messnet")
    if found:
        return os.path.abspath(found)
    raise InstallError("cannot find the messnet executable; give it with --exe PATH")


def resolve_exe(exe: str | None = None) -> str:
    """The messnet executable to run: EXE if given (it must be one), else the current one."""
    if not exe:
        return default_exe()
    path = os.path.abspath(os.path.expanduser(exe))
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        raise InstallError(f"{path} is not an executable file")
    return path


def global_args(config_path=None, node=None, spool=None, db=None, state=None, etc=None, network=None) -> tuple[str, ...]:
    """Global options the user gave, to pass on: cron and systemd have none of our environment.

    $MESSNET_CONFIG counts as ``-c``.  Paths are made absolute.
    """
    out: list[str] = []
    for flag, value, is_path in (("-c", config_path or os.environ.get("MESSNET_CONFIG"), True),
                                 ("--node", node, False), ("--spool", spool, True), ("--db", db, True),
                                 ("--state", state, True), ("--etc", etc, True), ("-n", network, False)):
        if value:
            out += [flag, os.path.abspath(os.path.expanduser(value)) if is_path else value]
    return tuple(out)


def search_path(exe: str) -> str:
    """The PATH given to the service under cron (which has almost none); fixed, so reruns agree."""
    dirs = [os.path.dirname(exe), os.path.expanduser("~/.local/bin"), "/usr/local/bin", "/usr/bin", "/bin"]
    return ":".join(dict.fromkeys(dirs))


def _run(*argv: str, input: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, input=input, capture_output=True, text=True)
    except FileNotFoundError as err:
        raise InstallError(f"{argv[0]} command not found") from err


# ---- cron -----------------------------------------------------------------------------

def cron_block(service: Service, interval: int = DEFAULT_INTERVAL) -> list[str]:
    """The lines to keep in the crontab."""
    command = f"PATH={shlex.quote(search_path(service.exe))} {shlex.join(service.argv())}".replace("%", r"\%")
    return [f"{BEGIN} (managed by 'messnet install cron'; edits between the markers are overwritten)",
            f"@reboot {command}",
            f"*/{interval} * * * * {command}",
            END]


def merge_block(text: str, block: list[str] | None) -> str:
    """Return crontab TEXT with its messnet block(s) replaced by BLOCK (removed if None).

    The block goes where the first one was, else at the end.  Nothing else is touched, so
    installing and then removing gives back the original text.
    """
    out: list[str] = []
    placed = False
    skipping = False
    for line in text.splitlines():
        if skipping:
            skipping = not line.startswith(END)
        elif line.startswith(BEGIN):
            skipping = True
            if block and not placed:
                out += block
                placed = True
        else:
            out.append(line)
    if skipping:
        raise InstallError(f"the crontab has '{BEGIN}' without '{END}'; fix it with 'crontab -e'")
    if block and not placed:
        out += block
    return "".join(f"{line}\n" for line in out)


def read_crontab() -> str:
    res = _run("crontab", "-l")
    if res.returncode == 0:
        return res.stdout
    if "no crontab" in res.stderr:
        return ""
    raise InstallError(f"crontab -l failed: {res.stderr.strip()}")


def write_crontab(text: str) -> None:
    res = _run("crontab", "-r") if not text else _run("crontab", "-", input=text)
    if res.returncode != 0:
        raise InstallError(f"crontab update failed: {res.stderr.strip()}")


def install_cron(service: Service, interval: int = DEFAULT_INTERVAL) -> Outcome:
    old = read_crontab()
    new = merge_block(old, cron_block(service, interval))
    if new == old:
        return Outcome(notes=["already installed; nothing to do"])
    write_crontab(new)
    return Outcome(True, [f"installed @reboot and every {interval} minutes in the crontab"])


def uninstall_cron() -> Outcome:
    old = read_crontab()
    new = merge_block(old, None)
    if new == old:
        return Outcome(notes=["not installed; nothing to do"])
    write_crontab(new)
    return Outcome(True, ["removed from the crontab"])


def describe_cron() -> list[str] | None:
    """Summary lines of the installation, or None if there is none."""
    lines, inside = [], False
    for line in read_crontab().splitlines():
        if line.startswith(BEGIN):
            inside = True
        elif inside and line.startswith(END):
            inside = False
        elif inside:
            lines.append(line)
    return ["crontab entries:", *[f"  {line}" for line in lines]] if lines else None


# ---- systemd (user) ---------------------------------------------------------------------

def _systemd_quote(arg: str) -> str:
    arg = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{arg}"' if not arg or any(c in arg for c in " \t'\"\\") else arg


def unit_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "systemd" / "user" / UNIT


def render_unit(service: Service) -> str:
    return (f"{UNIT_MARK}; edits are overwritten.\n"
            "[Unit]\n"
            "Description=messnet node: event spool, state and links\n"
            "\n"
            "[Service]\n"
            f"ExecStart={' '.join(_systemd_quote(a) for a in service.argv(foreground=True))}\n"
            "Restart=on-failure\n"
            "RestartSec=10\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n")


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return _run("systemctl", "--user", "--no-pager", *args)


def _systemctl_ok(*args: str) -> None:
    res = _systemctl(*args)
    if res.returncode != 0:
        raise InstallError(f"systemctl --user {' '.join(args)} failed: {(res.stderr or res.stdout).strip()}"
                           "\n(is the user service manager reachable? over ssh try: "
                           "export XDG_RUNTIME_DIR=/run/user/$(id -u))")


def lingering() -> bool | None:
    """Whether the user's service manager runs without a login; None if unknown."""
    res = _run("loginctl", "show-user", getpass.getuser(), "--property=Linger", "--value")
    if res.returncode != 0:
        return None
    return res.stdout.strip() == "yes"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as fp:
        fp.write(text)
    os.replace(tmp, path)


def _ours(path: Path) -> bool | None:
    """Whether the unit file at PATH is ours: None if there is no file."""
    return UNIT_MARK in path.read_text() if path.exists() else None


def install_systemd(service: Service, serving: bool = False) -> Outcome:
    """Install the user unit, enable it, start it unless a node service is SERVING already,
    and make sure the user's service manager runs from boot."""
    path, wanted = unit_path(), render_unit(service)
    out = Outcome()
    if _ours(path) is False:
        raise InstallError(f"{path} exists and was not made by 'messnet install'; "
                           "move it away or edit it by hand")
    rewritten = not path.exists() or path.read_text() != wanted
    if rewritten:
        _atomic_write(path, wanted)
        _systemctl_ok("daemon-reload")
        out.changed = True
        out.notes.append(f"wrote {path}")
    if _systemctl("is-enabled", UNIT).returncode != 0:
        _systemctl_ok("enable", UNIT)
        out.changed = True
        out.notes.append(f"enabled {UNIT} for default.target")
    active = _systemctl("is-active", UNIT).returncode == 0
    if active and rewritten:
        _systemctl_ok("restart", UNIT)
        out.notes.append(f"restarted {UNIT} to pick up the new command")
    elif not active and not serving:
        _systemctl_ok("start", UNIT)
        out.changed = True
        out.notes.append(f"started {UNIT}")
    elif not active:
        out.notes.append("a messnet service is already running (not started by systemd); not starting another")
    linger = lingering()
    if linger is False:
        res = _run("loginctl", "enable-linger", getpass.getuser())
        if res.returncode == 0:
            out.changed = True
            out.notes.append("enabled lingering: messnet now starts at boot, before anyone logs in")
        else:
            out.warnings.append(
                "lingering is off, so the user service manager (and messnet) starts only when "
                f"you first log in. Enable it with: sudo loginctl enable-linger {getpass.getuser()}\n"
                f"  (loginctl said: {res.stderr.strip() or 'failed'}); 'messnet install cron' starts it at boot meanwhile")
    elif linger is None:
        out.warnings.append("could not tell whether lingering is enabled (loginctl unavailable)")
    if not out.notes:
        out.notes.append("already installed; nothing to do")
    return out


def uninstall_systemd() -> Outcome:
    path = unit_path()
    ours = _ours(path)
    if ours is None:
        return Outcome(notes=["not installed; nothing to do"])
    if not ours:
        return Outcome(notes=[f"{path} was not made by 'messnet install'; left alone"])
    _systemctl("disable", "--now", UNIT)       # fine if it was not enabled or running
    path.unlink()
    _systemctl_ok("daemon-reload")
    return Outcome(True, [f"stopped, disabled and removed {path}",
                          "lingering was left as it is (other services may rely on it)"])


def describe_systemd() -> list[str] | None:
    path = unit_path()
    if not path.exists():
        return None
    lines = [f"unit file: {path}" + ("" if _ours(path) else "  (not made by messnet install)")]
    lines += [f"  {line}" for line in path.read_text().splitlines() if line.startswith("ExecStart=")]
    try:
        enabled = _systemctl("is-enabled", UNIT).stdout.strip() or "unknown"
        active = _systemctl("is-active", UNIT).stdout.strip() or "unknown"
        lines.append(f"  {enabled}, {active}")
    except InstallError:
        lines.append("  (systemctl unavailable)")
    linger = lingering()
    lines.append("  lingering: " + {True: "yes", False: "no (starts at first login only)", None: "unknown"}[linger])
    return lines


# ---- all targets ------------------------------------------------------------------------

TARGETS = ("cron", "systemd")


def install(target: str, service: Service, interval: int = DEFAULT_INTERVAL, serving: bool = False) -> Outcome:
    if target == "cron":
        return install_cron(service, interval)
    if target == "systemd":
        return install_systemd(service, serving)
    raise InstallError(f"unknown target {target!r}")


def uninstall(target: str) -> Outcome:
    if target == "cron":
        return uninstall_cron()
    if target == "systemd":
        return uninstall_systemd()
    raise InstallError(f"unknown target {target!r}")


def describe(db: Path) -> dict[str, list[str]]:
    """Summary lines of each existing installation, by target, plus a running service."""
    found = {}
    for target, fn in (("cron", describe_cron), ("systemd", describe_systemd)):
        try:
            if lines := fn():
                found[target] = lines
        except InstallError as err:
            found[target] = [f"(cannot examine: {err})"]
    if pid := lock.running_pid(lock.lock_path(db)):
        found["running"] = [f"messnet serve is running (pid {pid})"]
    return found
