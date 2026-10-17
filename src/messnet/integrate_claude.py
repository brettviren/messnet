"""The ``claude`` integration: Claude Code hooks that announce what a session does.

Claude Code reads hooks from ``settings.json`` (user level: ``~/.claude/settings.json`` or
``$CLAUDE_CONFIG_DIR/settings.json``).  This target keeps one *command hook* per wanted event
there, each running ``messnet hook claude`` (see ``claudehook``), and touches nothing else:
the user's other hooks, even under the same event, are left as they are.  A handler is ours if
its command ends in ``hook claude`` and runs an executable named ``messnet*``.

Which events are announced comes from the ``[integrate.claude]`` table of ``config.toml``::

    [integrate.claude]
    enable  = ["PreToolUse"]      # also announce these, beyond the defaults
    disable = ["Notification"]    # do not announce these, even if default
    scope   = "all"               # how far the events travel: host, lan or all
    settings = "~/.claude/settings.json"

``messnet integrate claude`` makes ``settings.json`` agree with that table, so editing the table
and running it again is how the set of events changes.  Event names are the Claude Code hook
names, in any case, with or without ``-`` or ``_`` (``SessionStart``, ``session-start``).
"""

import copy
import json
import os
import shlex
from collections.abc import Iterable
from pathlib import Path

from messnet import claudehook
from messnet.event import SCOPES
from messnet.integrate import IntegrateError, Outcome, Request, Status, Target, register, write_text

# Hooks the session waits for: SessionEnd runs as Claude Code exits, so it must finish first.
# The rest run in the background so that announcing never slows the session.
SYNC_EVENTS = ("SessionEnd",)
HOOK_TIMEOUT = 30
HOOK_ARGS = ("hook", "claude")


def settings_path(conf: dict | None = None, override: Path | None = None) -> Path:
    """The settings file to edit: OVERRIDE, else the config's ``settings``, else the user's."""
    if override:
        return Path(override).expanduser()
    if conf and conf.get("settings"):
        return Path(str(conf["settings"])).expanduser()
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser() / "settings.json"


def _names(value, what: str) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise IntegrateError(f"integrate.claude: {what} must be a list of hook names")
    try:
        return [claudehook.canonical(v) for v in value]
    except ValueError as err:
        raise IntegrateError(f"integrate.claude: {what}: {err}") from err


def wanted_events(*layers: dict) -> list[str]:
    """The hooks to announce, in catalogue order: the defaults, then each layer's ``enable``
    and then its ``disable`` (so within a layer disable wins, and a later layer wins over an
    earlier one)."""
    chosen = set(claudehook.DEFAULT_EVENTS)
    for layer in layers:
        chosen |= set(_names(layer.get("enable", []), "enable"))
        chosen -= set(_names(layer.get("disable", []), "disable"))
    return [name for name in claudehook.EVENTS if name in chosen]


def scope_of(conf: dict) -> str:
    scope = conf.get("scope", "all")
    if scope not in SCOPES:
        raise IntegrateError(f"integrate.claude: scope must be one of {', '.join(SCOPES)}: {scope!r}")
    return scope


def hook_command(service) -> str:
    """The shell command of our hooks: the messnet executable, its global options, 'hook claude'."""
    return shlex.join([service.exe, *service.args, *HOOK_ARGS])


def is_ours(handler) -> bool:
    """Whether a hook handler in settings.json was put there by this integration."""
    if not isinstance(handler, dict) or handler.get("type", "command") != "command":
        return False
    try:
        argv = shlex.split(str(handler.get("command", "")))
    except ValueError:
        return False
    return len(argv) > len(HOOK_ARGS) and tuple(argv[-2:]) == HOOK_ARGS and os.path.basename(argv[0]).startswith("messnet")


def make_handler(event: str, command: str) -> dict:
    handler = {"type": "command", "command": command, "timeout": HOOK_TIMEOUT}
    if event not in SYNC_EVENTS:
        handler["async"] = True
    return handler


# ---- editing the settings --------------------------------------------------------------

def load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        settings = json.loads(path.read_text() or "{}")
    except (OSError, ValueError) as err:
        raise IntegrateError(f"cannot read {path}: {err}") from err
    if not isinstance(settings, dict):
        raise IntegrateError(f"{path} is not a JSON object")
    return settings


def _hooks_of(settings: dict, path) -> dict:
    hooks = settings.get("hooks", {})
    bad = not isinstance(hooks, dict) or any(
        not isinstance(groups, list) or any(not isinstance(g, dict) or not isinstance(g.get("hooks", []), list)
                                            for g in groups)
        for groups in hooks.values())
    if bad:
        raise IntegrateError(f"{path}: 'hooks' does not have the layout Claude Code uses; not touching it")
    return hooks


def installed(settings: dict, path=None) -> dict[str, list[dict]]:
    """Our handlers in SETTINGS, by event."""
    found: dict[str, list[dict]] = {}
    for event, groups in _hooks_of(settings, path).items():
        for group in groups:
            found.setdefault(event, []).extend(h for h in group.get("hooks", []) if is_ours(h))
    return {event: hs for event, hs in found.items() if hs}


def reconcile(settings: dict, wanted: Iterable[str], command: str, path=None) -> dict:
    """A copy of SETTINGS with our handlers exactly those for WANTED events running COMMAND.

    Handlers already there are updated where they stand, so repeating this changes nothing
    and the user's other hooks keep their places.
    """
    wanted = list(wanted)
    out = copy.deepcopy(settings)
    hooks = _hooks_of(out, path)
    placed: set[str] = set()
    for event in list(hooks):
        groups = hooks[event]
        for group in list(groups):
            keep = []
            for handler in group.get("hooks", []):
                if not is_ours(handler):
                    keep.append(handler)
                elif event in wanted and event not in placed:
                    placed.add(event)
                    keep.append(make_handler(event, command))
            if keep:
                group["hooks"] = keep
            else:
                groups.remove(group)
        if not groups:
            del hooks[event]
    for event in wanted:
        if event not in placed:
            hooks.setdefault(event, []).append({"hooks": [make_handler(event, command)]})
    if hooks:
        out["hooks"] = hooks
    else:
        out.pop("hooks", None)
    return out


def _text(settings: dict) -> str:
    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"


class ClaudeTarget(Target):
    name = "claude"
    help = "Claude Code: hooks announcing session start and end and what happens in between."

    def _plan(self, req: Request):
        path = settings_path(req.conf, req.file)
        settings = load_settings(path)
        scope_of(req.conf)
        wanted = wanted_events(req.conf, {"enable": req.enable, "disable": req.disable})
        return path, settings, wanted, hook_command(req.service)

    def status(self, req: Request) -> Status:
        path, settings, wanted, command = self._plan(req)
        have = installed(settings, path)
        if not have:
            return Status(False, lines=[f"settings: {path}", "events it would announce: " + ", ".join(wanted)])
        commands = {h["command"] for hs in have.values() for h in hs}
        lines = [f"settings: {path}",
                 "how: Claude Code command hooks run '" + "' and '".join(sorted(commands)) + "'",
                 "events: " + ", ".join(e for e in claudehook.EVENTS if e in have),
                 f"scope: {scope_of(req.conf)}"]
        missing = [e for e in wanted if e not in have]
        extra = [e for e in claudehook.EVENTS if e in have and e not in wanted]
        stale = reconcile(settings, wanted, command, path) != settings
        if missing:
            lines.append("configured but not integrated: " + ", ".join(missing))
        if extra:
            lines.append("integrated but no longer configured: " + ", ".join(extra))
        if stale and not (missing or extra):
            lines.append(f"hook commands differ from the current one: {command}")
        return Status(True, current=not stale, lines=lines)

    def _write(self, path: Path, settings: dict, wanted: list[str], command: str) -> Outcome:
        before = installed(settings, path)
        new = reconcile(settings, wanted, command, path)
        out = Outcome(changed=new != settings)
        if out.changed:
            write_text(path, _text(new))
        after = installed(new, path)
        added = [e for e in after if e not in before]
        removed = [e for e in before if e not in after]
        if added:
            out.notes.append("announcing " + ", ".join(added))
        if removed:
            out.notes.append("no longer announcing " + ", ".join(removed))
        if out.changed and not (added or removed):
            out.notes.append("updated the hook commands")
        if out.changed:
            out.notes.append(f"wrote {path}; running Claude Code sessions may need /hooks or a restart to notice")
        elif after:
            out.notes.append(f"already integrated in {path}: " + ", ".join(e for e in claudehook.EVENTS if e in after))
        else:
            out.notes.append(f"not integrated in {path}; nothing to do")
        return out

    def apply(self, req: Request) -> Outcome:
        path, settings, wanted, command = self._plan(req)
        out = self._write(path, settings, wanted, command)
        if not wanted:
            out.notes.append("every event is disabled in the configuration")
        return out

    def remove(self, req: Request) -> Outcome:
        path, settings, _, command = self._plan(req)
        return self._write(path, settings, [], command)


CLAUDE = register(ClaudeTarget())
