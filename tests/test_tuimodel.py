import json
from datetime import datetime, timezone

from messnet import tuimodel

NOW = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)


def put(root, ev):
    path = root / ev["type"] / ev["origin"] / f"{ev.get('subject') or '_'}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ev))


def ev(type_, origin, subject, time, data):
    out = {"specversion": "1.0", "id": f"id-{type_}-{origin}-{subject}", "time": time, "source": "x", "type": type_,
           "origin": origin, "seq": 1, "scope": "all", "data": data}
    if subject:
        out["subject"] = subject
    return out


def make_state(root):
    put(root, ev("restic.status.v1", "alpha", "backup", "2026-10-11T11:50:00+00:00",
                 {"result": "ok", "commits": {}, "prime": {"local": {"primed": True}}}))
    put(root, ev("restic.status.v1", "beta", "backup", "2026-10-11T11:00:00+00:00",
                 {"result": "fail", "commits": {}, "prime": {"local": {"primed": True}}}))
    put(root, ev("restic.commit.v1", "beta", "run", "2026-10-11T11:30:00+00:00",
                 {"result": "ok", "method": "backup", "repository": "/r", "location": "local", "paths": ["/h"],
                  "snapshot_id": "ab" * 32, "total_files_processed": 3, "total_bytes_processed": 99}))
    put(root, ev("messnet.link.v1", "alpha", "beta", "2026-10-11T11:55:00+00:00", {"state": "up", "link": "tcp:x"}))
    put(root, ev("demo.thing.v1", "alpha", None, "2026-10-10T10:00:00+00:00", {"n": 1}))


def test_load_sorts_newest_first_and_skips_junk(tmp_path):
    make_state(tmp_path)
    (tmp_path / "restic.status.v1" / "alpha" / "garbage.json").write_text("{not json")
    (tmp_path / "restic.status.v1" / "alpha" / "list.json").write_text("[1]")
    (tmp_path / "x").mkdir()
    events = tuimodel.load(tmp_path)
    assert [e["type"] for e in events][:2] == ["messnet.link.v1", "restic.status.v1"]
    assert len(events) == 5 and tuimodel.newest(events)["type"] == "messnet.link.v1"
    assert tuimodel.load(tmp_path / "nowhere") == []


def test_host_view_groups(tmp_path):
    make_state(tmp_path)
    groups = {g.key: g for g in tuimodel.hosts(tuimodel.load(tmp_path))}
    assert list(groups) == ["alpha", "beta"]
    assert groups["alpha"].count == 3 and groups["beta"].count == 2
    assert groups["beta"].style == "fail" and groups["alpha"].style == "ok"        # beta's backups failed
    assert groups["alpha"].newest == datetime(2026, 10, 11, 11, 55, tzinfo=timezone.utc)


def test_event_view_groups(tmp_path):
    make_state(tmp_path)
    groups = {g.key: g for g in tuimodel.types(tuimodel.load(tmp_path))}
    assert list(groups) == ["demo.thing.v1", "messnet.link.v1", "restic.commit.v1", "restic.status.v1"]
    assert groups["restic.status.v1"].count == 2 and groups["restic.status.v1"].style == "fail"     # worst of both hosts
    assert groups["messnet.link.v1"].count == 1 and groups["messnet.link.v1"].style == ""     # connections, not health


def test_rows(tmp_path):
    make_state(tmp_path)
    events = tuimodel.load(tmp_path)
    host_rows = tuimodel.rows(events, "hosts", "beta", NOW)
    assert [r.cells[0] for r in host_rows] == ["restic.commit.v1", "restic.status.v1"]       # newest first, type shown
    assert host_rows[0].cells[1:3] == ["run", "30m ago"] and host_rows[0].style == "ok" and "files" in host_rows[0].cells[3]
    type_rows = tuimodel.rows(events, "types", "restic.status.v1", NOW)
    assert [r.cells[0] for r in type_rows] == ["alpha", "beta"] and [r.style for r in type_rows] == ["ok", "fail"]
    assert len({r.key for r in tuimodel.rows(events, "hosts", "alpha", NOW)}) == 3                # unique keys
    assert tuimodel.rows(events, "hosts", "alpha", NOW)[-1].cells[1] == "—"                       # no subject
    assert tuimodel.rows(events, "hosts", "nobody", NOW) == []


def test_signature_changes_with_the_state(tmp_path):
    make_state(tmp_path)
    first = tuimodel.signature(tmp_path)
    assert tuimodel.signature(tmp_path) == first
    put(tmp_path, ev("demo.thing.v1", "alpha", None, "2026-10-11T11:59:00+00:00", {"n": 222}))
    assert tuimodel.signature(tmp_path) != first
    path = tmp_path / "demo.thing.v1" / "alpha" / "_.json"
    before = tuimodel.signature(tmp_path)
    path.write_text(path.read_text())                                  # same content, same size...
    tmp = path.with_suffix(".tmp")
    tmp.write_text(path.read_text())
    tmp.replace(path)                                                   # ...but replaced the way state.py does
    assert tuimodel.signature(tmp_path) != before
    assert tuimodel.signature(tmp_path / "nowhere") == ()
