import json
import shutil
from pathlib import Path

import pytest

from messnet.event import EventError
from messnet.schema import Schemas, SchemaError, check_file, is_type_name
from test_cli import run

BACKUP = {
    "$id": "messnet:schema/backup.run.v1",
    "description": "A step of the backup script finished.  Subject: the step name.",
    "type": "object",
    "required": ["result"],
    "properties": {"result": {"enum": ["ok", "fail"]}, "n": {"type": "integer", "minimum": 0}},
    "x-messnet": {"subject": "required", "scope": "all", "semantics": "snapshot"},
}


def put_schema(etc, type, schema=BACKUP):
    (etc / "schemas").mkdir(parents=True, exist_ok=True)
    path = etc / "schemas" / f"{type}.json"
    path.write_text(json.dumps(schema))
    return path


@pytest.mark.parametrize("text, ok", [
    ("backup.run.v1", True), ("host.load.v12", True), ("a.b.c-d.v3", True),
    ("t.v1", False), ("backup.run", False), ("Backup.run.v1", False), ("backup.run.v0", False),
    ("backup..run.v1", False), ("../x.run.v1", False), ("backup.run.v1/", False), ("", False),
])
def test_is_type_name(text, ok):
    assert is_type_name(text) is ok


def test_emit_validates_against_local_schema(make_node):
    n = make_node("a")
    put_schema(n.cfg.etc, "backup.run.v1")
    ev = n.emit("backup.run.v1", {"result": "ok", "n": 3, "extra": True}, subject="push", validate=True)
    assert ev["seq"] == 1
    for data, subject, expect in [
        ({"result": "bad"}, "push", "data.result"),
        ({"n": 1}, "push", "'result' is a required property"),
        ({"result": "ok", "n": "3"}, "push", "data.n"),       # key=value gives a string
        ({"result": "ok"}, None, "subject is required"),
        (None, "push", "event has no data"),
    ]:
        with pytest.raises(SchemaError, match=expect):
            n.emit("backup.run.v1", data, subject=subject, validate=True)
    # Rejected events consume no seq and reach no spool.
    assert n.emit("backup.run.v1", {"result": "fail"}, subject="push", validate=True)["seq"] == 2
    assert n.store.version_vector() == {"a/all": 2}


def test_validation_is_opt_in_and_skips_unschematized_types(make_node):
    n = make_node("a")
    put_schema(n.cfg.etc, "backup.run.v1")
    n.emit("backup.run.v1", {"result": "bad"})                      # not validated
    n.emit("other.thing.v1", {"anything": 1}, validate=True)        # no schema
    n.emit("t.v1", {"anything": 1}, validate=True)                  # unconventional name, only warns
    assert n.store.version_vector() == {"a/all": 3}


def test_emit_object_validates(make_node):
    n = make_node("a")
    put_schema(n.cfg.etc, "backup.run.v1")
    with pytest.raises(EventError, match="backup.run.v1"):
        n.emit_object({"type": "backup.run.v1", "subject": "x", "data": {}}, validate=True)
    n.emit_object({"type": "backup.run.v1", "subject": "x", "data": {}})


def test_builtin_link_schema_and_local_override(make_node, tmp_path):
    n = make_node("a")
    assert n.schemas.types() == {"messnet.link.v1": "built-in"}
    n.emit("messnet.link.v1", {"state": "up", "link": "tcp x", "scope": "all"}, subject="b",
           scope="host", validate=True)
    with pytest.raises(SchemaError, match="data.state"):
        n.emit("messnet.link.v1", {"state": "sideways"}, subject="b", scope="host", validate=True)
    put_schema(n.cfg.etc, "messnet.link.v1", {"type": "object", "required": ["only"]})
    fresh = Schemas(n.cfg.etc)
    assert fresh.types() == {"messnet.link.v1": "local"}
    assert fresh.problems({"type": "messnet.link.v1", "data": {"state": "up"}})


def test_broken_schema_file_is_an_error(make_node):
    n = make_node("a")
    (n.cfg.etc / "schemas").mkdir(parents=True)
    (n.cfg.etc / "schemas" / "x.broken.v1.json").write_text("{nope")
    with pytest.raises(SchemaError, match="not valid JSON"):
        n.emit("x.broken.v1", {}, validate=True)
    put_schema(n.cfg.etc, "x.badtype.v1", {"type": "nonsense"})
    with pytest.raises(SchemaError, match="not a valid JSON Schema"):
        n.emit("x.badtype.v1", {}, validate=True)
    n.emit("x.broken.v1", {})                                       # --no-validate still works


def test_type_cannot_escape_schema_directory(make_node, tmp_path):
    n = make_node("a")
    (n.cfg.etc / "schemas").mkdir(parents=True)
    (tmp_path / "evil.v1.json").write_text(json.dumps({"type": "string"}))
    assert n.schemas.load("../../evil.v1") is None
    n.emit("../../evil.v1", {"x": 1}, validate=True)


def test_check_file(tmp_path):
    good = put_schema(tmp_path, "backup.run.v1")
    assert check_file(good) == []
    for name, schema, expect in [
        ("backup.run.v1", {"description": "d", "$id": "https://example.org/x"}, "$id must be"),
        ("backup.run.v1", {"type": "object"}, "missing description"),
        ("backup.run", {"description": "d"}, "not a type name"),
        ("backup.run.v1", {"description": "d", "x-messnet": {"semantics": "weird"}}, "x-messnet.semantics"),
        ("backup.run.v1", {"description": "d", "x-messnet": {"colour": "red"}}, "unknown key 'colour'"),
        ("backup.run.v1", {"description": "d", "type": 12}, "not a valid JSON Schema"),
    ]:
        path = put_schema(tmp_path / name.replace(".", "_"), name, schema)
        assert any(expect in p for p in check_file(path)), (name, schema, check_file(path))
    assert "No such file" in check_file(tmp_path / "missing.json")[0]


def test_cli_emit_validates(tmp_path):
    put_schema(tmp_path / "etc", "backup.run.v1")
    run(tmp_path, "emit", "backup.run.v1", "-s", "push", "result=ok", "n:=3")
    out = run(tmp_path, "emit", "backup.run.v1", "-s", "push", "result=ok", "n=3", codes=(1,))
    assert "data.n" in out
    run(tmp_path, "emit", "backup.run.v1", "-s", "push", "result=bad", "--no-validate")
    out = run(tmp_path, "emit", "--stdin", input='{"type":"backup.run.v1","data":{"result":"ok"}}\n', codes=(1,))
    assert "subject is required" in out
    assert run(tmp_path, "vv").strip() == "t/all 2"


def test_cli_schema_commands(tmp_path):
    put_schema(tmp_path / "etc", "backup.run.v1")
    assert run(tmp_path, "schema", "list").splitlines() == ["backup.run.v1 local", "messnet.link.v1 built-in"]
    assert json.loads(run(tmp_path, "schema", "show", "backup.run.v1"))["required"] == ["result"]
    assert "no schema" in run(tmp_path, "schema", "show", "no.such.v1", codes=(1,))
    assert run(tmp_path, "schema", "check", str(tmp_path / "etc/schemas/backup.run.v1.json")) == ""
    bad = put_schema(tmp_path / "bad", "backup.run", {"type": "object"})
    out = run(tmp_path, "schema", "check", str(bad), codes=(1,))
    assert "not a type name" in out and "missing description" in out
    assert "Usage" in run(tmp_path, "schema", codes=(2,))
    assert "Usage" in run(tmp_path, "schema", "check", codes=(2,))


def test_cli_validate_audits_stored_events(tmp_path):
    put_schema(tmp_path / "etc", "backup.run.v1")
    run(tmp_path, "emit", "backup.run.v1", "-s", "push", "result=ok")
    run(tmp_path, "emit", "other.thing.v1", "x=1")                  # no schema, not counted
    assert run(tmp_path, "validate").strip() == "1 events checked, 0 invalid"
    run(tmp_path, "emit", "backup.run.v1", "-s", "pull", "result=bad", "--no-validate")
    out = run(tmp_path, "validate", codes=(1,))
    assert "t/all/3 backup.run.v1: data.result" in out
    assert "2 events checked, 1 invalid" in out
    assert run(tmp_path, "validate", "-t", "other.*").strip() == "0 events checked, 0 invalid"


def test_documented_example_schema(make_node):
    """docs/schemas/backup.run.v1.json is the example docs/events.org shows; keep it working."""
    example = Path(__file__).parent.parent / "docs" / "schemas" / "backup.run.v1.json"
    assert check_file(example) == []
    n = make_node("a")
    (n.cfg.etc / "schemas").mkdir(parents=True)
    shutil.copy(example, n.cfg.etc / "schemas")
    n.emit("backup.run.v1", {"phase": "push", "result": "ok", "n": 3}, subject="push", validate=True)
    with pytest.raises(SchemaError):
        n.emit("backup.run.v1", {"result": "ok", "n": -1}, subject="push", validate=True)
