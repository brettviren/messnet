"""messnet integrate claude / hook claude, against settings files in a temporary directory."""

import copy
import json

import pytest

from messnet import claudehook, integrate_claude
from messnet.integrate import IntegrateError
from test_cli import run


@pytest.fixture
def exe(tmp_path):
    path = tmp_path / "opt" / "bin" / "messnet"
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return str(path)


# The user's own settings, with hooks of their own that must survive everything below.
THEIRS = {
    "model": "sonnet",
    "hooks": {
        "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "/bin/title emit claude"}]}],
        "SessionStart": [{"hooks": [{"type": "command", "command": "/bin/title emit claude"}]}],
        "Elicitation": [{"hooks": [{"type": "command", "command": "/bin/other"}]}],
    },
}


class Rig:
    def __init__(self, tmp_path, exe):
        self.tmp_path, self.exe = tmp_path, exe
        self.settings = tmp_path / "claude" / "settings.json"
        self.config = tmp_path / "config.toml"
        self.config.write_text("")

    def conf(self, body: str) -> None:
        self.config.write_text(body)

    def write(self, settings: dict) -> None:
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text(json.dumps(settings, indent=2) + "\n")

    def read(self) -> dict:
        return json.loads(self.settings.read_text())

    def integrate(self, *args, codes=(0,)) -> str:
        return run(self.tmp_path, "-c", str(self.config), "integrate", "claude", "--file", str(self.settings),
                   "--exe", self.exe, *args, codes=codes)

    def events(self) -> list[str]:
        """Events the hooks announce, ours only."""
        return [e for e in claudehook.EVENTS if e in integrate_claude.installed(self.read())]


@pytest.fixture
def rig(tmp_path, exe):
    return Rig(tmp_path, exe)


# ---- hook input -> event ----------------------------------------------------------------

def test_event_type_names():
    assert claudehook.event_type("SessionStart") == "claude.session-start.v1"
    assert claudehook.event_type("PostToolUseFailure") == "claude.post-tool-use-failure.v1"
    assert claudehook.canonical("session_end") == "SessionEnd" == claudehook.canonical("SESSION-END")
    with pytest.raises(ValueError, match="unknown"):
        claudehook.canonical("Bogus")


def test_event_from_hook_keeps_little():
    got = claudehook.event_from_hook({
        "hook_event_name": "PreToolUse", "session_id": "abc", "cwd": "/w", "tool_name": "Bash",
        "tool_input": {"command": "secret"}, "prompt": "private", "transcript_path": "/t"})
    assert got == {"type": "claude.pre-tool-use.v1", "subject": "abc",
                   "data": {"hook": "PreToolUse", "cwd": "/w", "tool_name": "Bash"}}
    long = claudehook.event_from_hook({"hook_event_name": "Notification", "message": "x" * 1000})
    assert len(long["data"]["message"]) == claudehook.MAX_TEXT and long["subject"] is None
    with pytest.raises(ValueError):
        claudehook.event_from_hook({"session_id": "abc"})


# ---- the event set -----------------------------------------------------------------------

def test_wanted_events_layers():
    default = list(claudehook.DEFAULT_EVENTS)
    assert set(integrate_claude.wanted_events({})) == set(default)
    got = integrate_claude.wanted_events({"enable": ["pre-tool-use"], "disable": ["Notification", "Stop"]})
    assert "PreToolUse" in got and "Notification" not in got and "Stop" not in got
    assert got == [e for e in claudehook.EVENTS if e in got]                      # catalogue order
    assert "Stop" not in integrate_claude.wanted_events({"enable": "Stop", "disable": "Stop"})   # disable wins
    assert "Stop" in integrate_claude.wanted_events({"disable": ["Stop"]}, {"enable": ["Stop"]})  # later layer wins
    with pytest.raises(IntegrateError, match="unknown"):
        integrate_claude.wanted_events({"enable": ["Bogus"]})


# ---- the command -------------------------------------------------------------------------

def test_status_apply_remove_cycle(rig):
    assert "not integrated" in rig.integrate("--status")
    assert not rig.settings.exists()                           # --status never writes
    out = rig.integrate()
    assert "announcing SessionStart" in out
    assert rig.events() == [e for e in claudehook.EVENTS if e in claudehook.DEFAULT_EVENTS]
    status = rig.integrate("--status")
    assert status.startswith("claude: integrated\n") and rig.exe in status and "SessionEnd" in status
    assert "already integrated" in rig.integrate()
    rig.integrate("--remove")
    assert not rig.settings.exists() or rig.read() == {}
    assert "not integrated" in rig.integrate("--status")
    assert "nothing to do" in rig.integrate("--remove")


def test_apply_is_idempotent_and_leaves_other_hooks(rig):
    rig.write(THEIRS)
    rig.integrate()
    once = rig.settings.read_text()
    rig.integrate()
    assert rig.settings.read_text() == once
    after = rig.read()
    assert after["model"] == "sonnet" and after["hooks"]["Elicitation"] == THEIRS["hooks"]["Elicitation"]
    # Their handlers are still there, in their original places, beside ours.
    assert after["hooks"]["PreToolUse"] == THEIRS["hooks"]["PreToolUse"]
    assert after["hooks"]["SessionStart"][0] == THEIRS["hooks"]["SessionStart"][0]
    assert len(after["hooks"]["SessionStart"]) == 2
    rig.integrate("--remove")
    assert rig.read() == THEIRS


def test_follows_the_configuration(rig):
    rig.conf('[integrate.claude]\nenable = ["PreToolUse", "post-tool-use"]\ndisable = ["Notification"]\n')
    rig.integrate()
    got = rig.events()
    assert {"PreToolUse", "PostToolUse"} <= set(got) and "Notification" not in got and "SessionEnd" in got
    assert rig.read()["hooks"]["PreToolUse"][0]["hooks"][0]["async"] is True
    assert "async" not in rig.read()["hooks"]["SessionEnd"][0]["hooks"][0]
    # Change the configuration: the file follows, and status notices until it does.
    rig.conf('[integrate.claude]\nenable = ["SubagentStart"]\ndisable = ["Stop"]\n')
    status = rig.integrate("--status")
    assert "out of date" in status and "SubagentStart" in status and "PreToolUse" in status
    out = rig.integrate()
    lines = out.splitlines()
    assert "SubagentStart" in lines[0] and "no longer announcing" in lines[1] and "PreToolUse" in lines[1]
    got = rig.events()
    assert "SubagentStart" in got and "PreToolUse" not in got and "Stop" not in got and "Notification" in got
    assert rig.integrate("--status").startswith("claude: integrated\n")


def test_command_line_overrides_the_configuration(rig):
    rig.conf('[integrate.claude]\ndisable = ["Stop"]\n')
    rig.integrate("--enable", "Stop", "--disable", "SessionEnd")
    got = rig.events()
    assert "Stop" in got and "SessionEnd" not in got


def test_all_events_disabled_integrates_nothing(rig):
    rig.write(THEIRS)
    rig.integrate()
    rig.conf('[integrate.claude]\ndisable = [' + ",".join(f'"{e}"' for e in claudehook.DEFAULT_EVENTS) + ']\n')
    assert "every event is disabled" in rig.integrate()
    assert rig.read() == THEIRS


def test_changed_executable_updates_hooks(rig, tmp_path):
    rig.integrate()
    other = tmp_path / "new" / "messnet"
    other.parent.mkdir()
    other.write_text("#!/bin/sh\n")
    other.chmod(0o755)
    out = run(tmp_path, "-c", str(rig.config), "integrate", "claude", "--file", str(rig.settings), "--exe", str(other))
    assert "updated the hook commands" in out
    text = rig.settings.read_text()
    assert str(other) in text and rig.exe not in text


def test_global_options_are_passed_to_the_hooks(rig):
    run(rig.tmp_path, "-c", str(rig.config), "--node", "n1", "integrate", "claude", "--file", str(rig.settings),
        "--exe", rig.exe)
    command = rig.read()["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert command.endswith(f"{rig.config} --node n1 hook claude") and integrate_claude.is_ours({"command": command})


def test_bad_input_changes_nothing(rig):
    rig.settings.parent.mkdir(parents=True)
    rig.settings.write_text("{not json")
    assert "cannot read" in rig.integrate(codes=(1,))
    assert rig.settings.read_text() == "{not json"
    rig.write({"hooks": {"Stop": "oops"}})
    before = rig.settings.read_text()
    assert "layout" in rig.integrate(codes=(1,))
    assert rig.settings.read_text() == before
    rig.write(THEIRS)
    rig.conf('[integrate.claude]\nenable = ["Bogus"]\n')
    assert "unknown" in rig.integrate(codes=(1,))
    rig.conf('[integrate.claude]\nscope = "moon"\n')
    assert "scope" in rig.integrate(codes=(1,))
    assert rig.read() == THEIRS
    assert "--status and --remove" in rig.integrate("--status", "--remove", codes=(2,))


def test_symlinked_settings_stay_links(rig, tmp_path):
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text(json.dumps(copy.deepcopy(THEIRS)))
    real.chmod(0o644)
    rig.settings.parent.mkdir(parents=True)
    rig.settings.symlink_to(real)
    rig.integrate()
    assert rig.settings.is_symlink() and "SessionEnd" in json.loads(real.read_text())["hooks"]
    assert real.stat().st_mode & 0o777 == 0o644


def test_integrate_without_args_prints_help(tmp_path):
    assert "Usage" in run(tmp_path, "integrate", codes=(2,))
    assert "Usage" in run(tmp_path, "hook", codes=(2,))


# ---- the hook itself ---------------------------------------------------------------------

def test_hook_claude_emits_the_event(tmp_path):
    payload = {"hook_event_name": "SessionStart", "session_id": "s-1", "source": "startup", "cwd": "/w",
               "prompt": "never stored"}
    run(tmp_path, "hook", "claude", input=json.dumps(payload))
    (ev,) = [json.loads(line) for line in run(tmp_path, "tail", "-t", "claude.*").splitlines()]
    assert ev["type"] == "claude.session-start.v1" and ev["subject"] == "s-1" and ev["scope"] == "all"
    assert ev["data"] == {"hook": "SessionStart", "cwd": "/w", "source": "startup"}
    assert ev["source"].endswith("/claude")


def test_hook_claude_follows_the_scope_and_never_fails(tmp_path):
    conf = tmp_path / "c.toml"
    conf.write_text('[integrate.claude]\nscope = "host"\n')
    run(tmp_path, "-c", str(conf), "hook", "claude", input=json.dumps({"hook_event_name": "Stop", "session_id": "s"}))
    (ev,) = [json.loads(line) for line in run(tmp_path, "tail", "-t", "claude.*").splitlines()]
    assert ev["scope"] == "host"
    for junk in ("", "not json", "[]", '{"no": "event"}'):
        run(tmp_path, "hook", "claude", input=junk)                        # exit 0 every time
    assert len(run(tmp_path, "tail", "-t", "claude.*").splitlines()) == 1
