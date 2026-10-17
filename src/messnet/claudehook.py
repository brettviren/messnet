"""Claude Code hooks as messnet events.

Claude Code runs a *hook* command at points in a session and writes a JSON description of
the occurrence on its stdin.  ``messnet hook claude`` (which calls :func:`event_from_hook`)
turns that into an event of type ``claude.<hook-name>.v1``, e.g. ``SessionStart`` becomes
``claude.session-start.v1``.  The subject is the Claude session id, so the state files hold
the newest event of each kind per session.

Only a few small fields of the hook input are kept.  Prompts, tool inputs and tool results
are deliberately dropped: events travel to other hosts, and those can be large and private.

:data:`EVENTS` is the catalogue of hooks that can be announced; :data:`DEFAULT_EVENTS` is the
set announced unless the ``[integrate.claude]`` configuration says otherwise (see
``messnet.integrate_claude``).
"""

import re

# Hooks that only observe, in the order they are listed: name -> the hook-input fields kept
# besides the common ones.  Hooks left out of the catalogue (WorktreeCreate, ...) expect the
# command to answer with output, which a pure announcer cannot give.
EVENTS: dict[str, tuple[str, ...]] = {
    "SessionStart": ("source", "model", "session_title"),
    "SessionEnd": ("reason",),
    "UserPromptSubmit": (),
    "Stop": (),
    "StopFailure": ("error",),
    "Notification": ("notification_type", "message"),
    "PermissionRequest": ("tool_name",),
    "PermissionDenied": ("tool_name",),
    "SubagentStart": (),
    "SubagentStop": (),
    "PreToolUse": ("tool_name",),
    "PostToolUse": ("tool_name",),
    "PostToolUseFailure": ("tool_name",),
    "PostToolBatch": (),
    "PreCompact": ("trigger",),
    "PostCompact": ("trigger",),
    "TaskCreated": ("task_id", "task_subject"),
    "TaskCompleted": ("task_id", "task_subject"),
    "TeammateIdle": ("teammate_name",),
    "InstructionsLoaded": ("file_path", "load_reason"),
    "ConfigChange": ("source", "file_path"),
    "CwdChanged": ("old_cwd", "new_cwd"),
    "PostModelSwitch": ("from_model", "to_model"),
}

# What a person wants to hear about by default: the life of a session and the moments it needs
# attention.  Per-tool and bookkeeping hooks are chatty, so they must be asked for.
DEFAULT_EVENTS = ("SessionStart", "SessionEnd", "UserPromptSubmit", "Stop", "StopFailure",
                  "Notification", "PermissionRequest", "SubagentStop")

COMMON = ("cwd", "permission_mode", "agent_id", "agent_type")
MAX_TEXT = 300
TYPE_FORMAT = "claude.{}.v1"


def _key(name: str) -> str:
    return re.sub(r"[-_\s]", "", name).lower()


_BY_KEY = {_key(name): name for name in EVENTS}


def canonical(name: str) -> str:
    """The catalogue name of hook NAME, accepting any case and ``-`` / ``_`` separators
    (``session-start`` for ``SessionStart``).  Raise ValueError if it is not in the catalogue."""
    try:
        return _BY_KEY[_key(name)]
    except KeyError:
        raise ValueError(f"unknown Claude Code hook {name!r}; known: {', '.join(EVENTS)}") from None


def event_type(name: str) -> str:
    """The messnet event type announcing hook NAME (a catalogue name)."""
    return TYPE_FORMAT.format(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", name).lower())


def event_from_hook(payload: dict) -> dict:
    """Describe the hook input PAYLOAD as arguments for ``Node.emit``: type, data, subject.

    Raise ValueError if PAYLOAD is not a hook input.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("hook_event_name"), str):
        raise ValueError("not Claude Code hook input: no hook_event_name")
    name = payload["hook_event_name"]
    fields = COMMON + EVENTS.get(name, ())
    data = {"hook": name}
    for field in fields:
        value = payload.get(field)
        if isinstance(value, str) and value:
            data[field] = value if len(value) <= MAX_TEXT else value[:MAX_TEXT - 1] + "…"
        elif isinstance(value, (int, float, bool)):
            data[field] = value
    session = payload.get("session_id")
    return {"type": event_type(name), "data": data, "subject": session if isinstance(session, str) and session else None}
