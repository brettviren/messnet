"""Integrating messnet with other programs ('messnet integrate TARGET').

An integration changes some other program's configuration so that it talks to messnet.  Every
target is a :class:`Target` with three operations, all idempotent and all driven by the
``[integrate.TARGET]`` table of ``config.toml``:

``apply``   make the program's configuration reflect the messnet configuration, whatever it
            holds now: first time, repeated, or after the messnet configuration changed.
``status``  report whether it is integrated, and how, and whether that is up to date.
``remove``  take out exactly what ``apply`` puts in, leaving the program's own settings alone.

Targets live in their own modules and add themselves to :data:`TARGETS` with :func:`register`.
"""

import os
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from messnet.install import InstallError, Outcome, Service

__all__ = ["IntegrateError", "Outcome", "Request", "Status", "Target", "TARGETS", "register", "write_text"]


class IntegrateError(InstallError):
    pass


@dataclass(frozen=True)
class Request:
    """What a target needs to know: how to run messnet, its ``[integrate.TARGET]`` settings,
    the command line's overrides of the enable / disable lists, and the file to edit if the
    user named one instead of the program's usual one."""
    service: Service
    conf: dict = field(default_factory=dict)
    enable: tuple[str, ...] = ()
    disable: tuple[str, ...] = ()
    file: Path | None = None


@dataclass
class Status:
    """Whether a target is INTEGRATED, whether that is CURRENT with the messnet configuration,
    and LINES describing how it was done (or what is out of date)."""
    integrated: bool
    current: bool = True
    lines: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if not self.integrated:
            return "not integrated"
        return "integrated" if self.current else "integrated, but out of date (re-run to update)"


class Target(ABC):
    name: str
    help: str

    @abstractmethod
    def status(self, req: Request) -> Status: ...

    @abstractmethod
    def apply(self, req: Request) -> Outcome: ...

    @abstractmethod
    def remove(self, req: Request) -> Outcome: ...


TARGETS: dict[str, Target] = {}


def register(target: Target) -> Target:
    TARGETS[target.name] = target
    return target


def write_text(path: Path, text: str) -> None:
    """Replace the file at PATH with TEXT in one step, keeping its permissions and writing
    through a symbolic link (dotfiles often are) rather than replacing it."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as fp:
            fp.write(text)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


from messnet import integrate_claude  # noqa: E402,F401  (registers its target; needs the names above)
