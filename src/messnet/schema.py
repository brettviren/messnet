"""Payload schemas for event types.

A type ``family.noun.vN`` may have a JSON Schema (draft 2020-12) describing the
``data`` of its events, in a file ``<type>.json``.  Files are looked up in
``<etc>/schemas/`` (shared by Syncthing; a local file overrides a built-in one)
and then in the ``schemas`` directory shipped with the package.  A type without
a schema is not validated.

The custom ``x-messnet`` keyword carries what is about the envelope rather than
``data``::

    "x-messnet": {"subject": "required", "scope": "all", "semantics": "snapshot"}

Validation is a producer-side and audit-time tool.  Ingest and replication never
consult schemas: they store and forward every well formed event.
"""

import json
import logging
import re
from collections.abc import Iterable, Iterator
from importlib import resources
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError as JsonSchemaError

from messnet.event import SCOPES, EventError

log = logging.getLogger(__name__)

# family.noun[.noun...].vN: lower case segments, the version last.
_TYPE = re.compile(r"[a-z][a-z0-9-]*(\.[a-z0-9][a-z0-9-]*)+\.v[1-9][0-9]*")
SCHEMA_ID = "messnet:schema/{}"
EXTENSION = "x-messnet"
_META_VALUES = {"subject": ("required", "optional"), "scope": SCOPES, "semantics": ("snapshot", "log")}


class SchemaError(EventError):
    """An event does not conform to its type's schema, or the schema itself is bad."""


def is_type_name(text: str) -> bool:
    """True if TEXT follows the ``family.noun.vN`` naming convention."""
    return bool(_TYPE.fullmatch(text))


def _path_text(path) -> str:
    return "data" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in path)


def _parse(text: str, label: str) -> tuple[dict, Draft202012Validator]:
    try:
        schema = json.loads(text)
        if not isinstance(schema, dict):
            raise SchemaError(f"{label}: schema must be a JSON object")
        Draft202012Validator.check_schema(schema)
    except json.JSONDecodeError as err:
        raise SchemaError(f"{label}: not valid JSON: {err}") from err
    except JsonSchemaError as err:
        raise SchemaError(f"{label}: not a valid JSON Schema: {err.message}") from err
    return schema, Draft202012Validator(schema)


def check_file(path: Path) -> list[str]:
    """Lint a schema file; return a list of problems (empty if it is fine)."""
    path = Path(path)
    try:
        schema, _ = _parse(path.read_text(), path.name)
    except OSError as err:
        return [str(err)]
    except SchemaError as err:
        return [str(err)]
    problems = []
    if not is_type_name(path.stem):
        problems.append(f"file name {path.name!r} is not a type name like family.noun.v1")
    elif schema.get("$id", SCHEMA_ID.format(path.stem)) != SCHEMA_ID.format(path.stem):
        problems.append(f"$id must be {SCHEMA_ID.format(path.stem)!r}, not {schema['$id']!r}")
    if not schema.get("description"):
        problems.append("missing description: say when the event is emitted and what subject means")
    meta = schema.get(EXTENSION, {})
    if not isinstance(meta, dict):
        problems.append(f"{EXTENSION} must be an object")
    else:
        for key, value in meta.items():
            if key not in _META_VALUES:
                problems.append(f"{EXTENSION}: unknown key {key!r}")
            elif value not in _META_VALUES[key]:
                problems.append(f"{EXTENSION}.{key} must be one of {list(_META_VALUES[key])}: {value!r}")
    return problems


class Schemas:
    """Schema lookup and validation for one node's ``etc`` directory."""

    def __init__(self, etc: Path | None = None):
        self.dirs = [("local", Path(etc) / "schemas")] if etc else []
        self.dirs.append(("built-in", resources.files("messnet") / "schemas"))
        self._cache: dict[str, tuple[dict, Draft202012Validator] | None] = {}

    def types(self) -> dict[str, str]:
        """Map each type with a schema to where it comes from, sorted by type."""
        found = {}
        for source, directory in reversed(self.dirs):     # local entries override built-in ones
            if directory.is_dir():
                for entry in directory.iterdir():
                    name = entry.name.removesuffix(".json")
                    if entry.name.endswith(".json") and is_type_name(name):
                        found[name] = source
        return dict(sorted(found.items()))

    def _get(self, type: str) -> tuple[dict, Draft202012Validator] | None:
        if type not in self._cache:
            entry = None
            if is_type_name(type):                        # also keeps TYPE from escaping the directory
                for _, directory in self.dirs:
                    file = directory / f"{type}.json"
                    if file.is_file():
                        entry = _parse(file.read_text(), f"schema for {type}")
                        break
            self._cache[type] = entry
        return self._cache[type]

    def load(self, type: str) -> dict | None:
        """Return the schema of TYPE, or None if it has none."""
        entry = self._get(type)
        return entry and entry[0]

    def problems(self, ev: dict) -> list[str]:
        """Return messages for how EV violates its type's schema (none if conforming or unchecked)."""
        entry = self._get(ev["type"])
        if entry is None:
            return []
        schema, validator = entry
        out = []
        if schema.get(EXTENSION, {}).get("subject") == "required" and not ev.get("subject"):
            out.append("subject is required")
        errors = sorted(validator.iter_errors(ev.get("data")), key=lambda e: [str(p) for p in e.absolute_path])
        if "data" not in ev and errors:
            out.append("event has no data")
        else:
            out.extend(f"{_path_text(e.absolute_path)}: {e.message}" for e in errors)
        return out

    def check(self, ev: dict) -> None:
        """Raise SchemaError if EV does not conform to its type's schema.

        Types that break the naming convention are only warned about.
        """
        if not is_type_name(ev["type"]):
            log.warning("type %r does not follow family.noun.vN", ev["type"])
        if problems := self.problems(ev):
            raise SchemaError(f"{ev['type']} does not match its schema: " + "; ".join(problems))

    def audit(self, events: Iterable[dict]) -> Iterator[tuple[dict, list[str]]]:
        """Yield (event, problems) for each event whose type has a schema."""
        for ev in events:
            if self._get(ev["type"]) is not None:
                yield ev, self.problems(ev)
