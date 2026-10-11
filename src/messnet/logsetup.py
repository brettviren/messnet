"""The service log: a rotating file, next to (not instead of) the usual stderr logging.

The CLI sets up stderr logging at WARNING (``-v`` for more).  ``serve`` / ``run`` run unattended,
where nobody reads stderr, so they also log to ``cfg.log`` (default ``~/.cache/messnet/serve.log``)
at ``cfg.log_level``, which can change while the service runs.
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LEVELS = {"error": logging.ERROR, "warning": logging.WARNING, "info": logging.INFO, "debug": logging.DEBUG}
MAX_BYTES = 1 << 20
BACKUPS = 3
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

_handler: RotatingFileHandler | None = None
_path: Path | None = None
_console_level: int | None = None
_screen = False


def screen() -> None:
    """Debugging in the foreground: everything to the screen, and no log file (configure() is
    then a no-op, so a reload cannot add one)."""
    global _screen
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in root.handlers:
        h.setLevel(logging.DEBUG)
        h.setFormatter(logging.Formatter(FORMAT))
    _screen = True


def configure(path: Path, level: str) -> bool:
    """Log to PATH at LEVEL ("off" for no file); safe to call again to change either.

    Returns False if a file was wanted but could not be opened.
    """
    global _handler, _path, _console_level
    if _screen:
        return True
    root = logging.getLogger()
    if _console_level is None:                      # what stderr logging was set to; keep it so
        _console_level = root.level
        for h in root.handlers:
            h.setLevel(_console_level)
    path = Path(path)
    if _handler is not None and (level == "off" or path != _path):
        root.removeHandler(_handler)
        _handler.close()
        _handler = None
    if level == "off":
        root.setLevel(_console_level)
        return True
    if _handler is None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _handler = RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
        except OSError as err:
            logging.getLogger(__name__).warning("cannot write the log file %s: %s", path, err)
            return False
        _handler.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(_handler)
        _path = path
    _handler.setLevel(LEVELS[level])
    root.setLevel(min(_console_level, LEVELS[level]))
    return True


def reset() -> None:
    """Undo configure() (for tests)."""
    global _handler, _path, _console_level, _screen
    root = logging.getLogger()
    if _handler is not None:
        root.removeHandler(_handler)
        _handler.close()
    if _console_level is not None:
        root.setLevel(_console_level)
    _handler = _path = _console_level = None
    _screen = False
