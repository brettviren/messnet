"""The built-in restic.* schemas: well formed, and accepting/rejecting the right events.

Sample values are taken from real restic 0.18 output (backup --json summary, copy text).
"""

import copy
from pathlib import Path

import pytest

from messnet.schema import Schemas, check_file

SCHEMA_DIR = Path(__file__).parent.parent / "src" / "messnet" / "schemas"
SNAP = "9de968c1e8dd1f54e7b4339f393eafa339c878b6ea02b4015971ef755bde2477"

BACKUP_OK = {
    "result": "ok", "method": "backup", "repository": "/srv/bv/backups/restic", "location": "local",
    "paths": ["/home/bv"], "snapshot_id": SNAP, "exit_code": 0,
    "files_new": 2, "files_changed": 0, "files_unmodified": 0, "dirs_new": 8, "dirs_changed": 0,
    "dirs_unmodified": 0, "data_blobs": 2, "tree_blobs": 9, "data_added": 103713,
    "data_added_packed": 102936, "total_files_processed": 2, "total_bytes_processed": 100006,
    "started": "2026-10-11T08:35:24.709389986-04:00", "ended": "2026-10-11T08:35:25.436478628-04:00",
    "duration": 0.727088714, "restic_version": "0.18.0",
}
COPY_OK = {
    "result": "ok", "method": "copy", "location": "remote",
    "repository": "sftp:bv@yobox.pigeon-alpha.ts.net:/mnt/boxdsk1/backups/restic",
    "source_repository": "/srv/bv/backups/restic", "snapshots_copied": 2,
    "snapshots": [{"id": "33665e8c", "source_id": "93f5e196", "packs": 4}, {"id": "8f63c089", "source_id": "e6fe83bb"}],
    "total_files_processed": 4, "total_bytes_processed": 200000, "duration": 31.5, "exit_code": 0,
}
FORGET_OK = {"result": "ok", "repository": "/srv/bv/backups/restic", "location": "local", "prune": True,
             "keep_policy": {"last": 2, "daily": 7}, "snapshots_kept": 2, "snapshots_removed": 2}
PRIME_OK = {"result": "ok", "ttl": 604800, "expires": "2026-10-18T09:00:00-04:00"}
STATUS_OK = {
    "result": "ok",
    "commits": {"run": {"result": "ok", "time": "2026-10-11T08:35:25-04:00", "method": "backup",
                        "location": "local", "snapshot_id": SNAP, "duration": 0.7},
                "push": {"result": "ok", "time": "2026-10-11T08:40:00-04:00", "method": "copy",
                         "location": "remote", "snapshots_copied": 2}},
    "forget": {"run": {"result": "ok", "time": "2026-10-11T08:35:00-04:00", "snapshots_removed": 2}},
    "prime": {"local": {"primed": True, "ttl_remaining": 500000, "expires": "2026-10-18T09:00:00-04:00"},
              "remote": {"primed": False}},
}


def ev(type, data, subject):
    return {"type": type, "subject": subject, "data": data}


def problems(type, data, subject="x"):
    return Schemas().problems(ev(type, data, subject))


def without(data, *keys):
    return {k: v for k, v in data.items() if k not in keys}


def test_schema_files_pass_lint():
    files = sorted(SCHEMA_DIR.glob("restic.*.json"))
    assert [f.stem for f in files] == ["restic.commit.v1", "restic.forget.v1", "restic.prime.v1", "restic.status.v1"]
    for file in files:
        assert check_file(file) == [], file


def test_all_registered_as_builtin():
    types = Schemas().types()
    assert {t for t in types if t.startswith("restic.")} == {
        "restic.commit.v1", "restic.forget.v1", "restic.prime.v1", "restic.status.v1"}
    assert set(types.values()) == {"built-in"}


@pytest.mark.parametrize("type, data, subject", [
    ("restic.commit.v1", BACKUP_OK, "run"),
    ("restic.commit.v1", COPY_OK, "push"),
    ("restic.forget.v1", FORGET_OK, "run"),
    ("restic.prime.v1", PRIME_OK, "local"),
    ("restic.status.v1", STATUS_OK, "backup"),
])
def test_valid_events(type, data, subject):
    assert problems(type, data, subject) == []


@pytest.mark.parametrize("type, data", [
    ("restic.commit.v1", BACKUP_OK), ("restic.forget.v1", FORGET_OK),
    ("restic.prime.v1", PRIME_OK), ("restic.status.v1", STATUS_OK),
])
def test_subject_required(type, data):
    assert problems(type, data, None) == ["subject is required"]


def test_commit_direct_to_remote_and_partial_and_failure():
    direct = {**BACKUP_OK, "repository": "rest:https://backup.example.org/bv", "location": "remote"}
    assert problems("restic.commit.v1", direct, "run") == []
    partial = {**BACKUP_OK, "result": "partial", "exit_code": 3, "error_count": 1,
               "error": "Warning: at least one source file could not be read"}
    assert problems("restic.commit.v1", partial, "run") == []
    failed = {"result": "fail", "method": "backup", "repository": "/r", "location": "local",
              "paths": ["/home/bv"], "exit_code": 12, "error": "Fatal: wrong password or no key found"}
    assert problems("restic.commit.v1", failed, "run") == []
    nothing_to_copy = {**without(COPY_OK, "snapshots"), "snapshots_copied": 0}
    assert problems("restic.commit.v1", nothing_to_copy, "push") == []


@pytest.mark.parametrize("data, expect", [
    (without(BACKUP_OK, "paths"), "'paths' is a required property"),
    (without(BACKUP_OK, "snapshot_id"), "'snapshot_id' is a required property"),
    ({**BACKUP_OK, "result": "partial"}, "'error' is a required property"),
    ({**BACKUP_OK, "result": "fail"}, "'error' is a required property"),
    ({**BACKUP_OK, "result": "maybe"}, "data.result"),
    ({**BACKUP_OK, "location": "mars"}, "data.location"),
    ({**BACKUP_OK, "method": "rsync"}, "data.method"),
    ({**BACKUP_OK, "snapshot_id": "not-hex"}, "data.snapshot_id"),
    ({**BACKUP_OK, "files_new": "2"}, "data.files_new"),              # a shell-built string
    ({**BACKUP_OK, "duration": -1}, "data.duration"),
    ({**BACKUP_OK, "paths": []}, "data.paths"),
    (without(BACKUP_OK, "repository"), "'repository' is a required property"),
    (without(COPY_OK, "source_repository"), "'source_repository' is a required property"),
    (without(COPY_OK, "snapshots_copied"), "'snapshots_copied' is a required property"),
    ({**COPY_OK, "snapshots": [{"source_id": "93f5e196"}]}, "'id' is a required property"),
])
def test_invalid_commits(data, expect):
    msgs = problems("restic.commit.v1", data, "run")
    assert any(expect in m for m in msgs), msgs


@pytest.mark.parametrize("type, data, expect", [
    ("restic.forget.v1", {**FORGET_OK, "result": "fail"}, "'error' is a required property"),
    ("restic.forget.v1", {**FORGET_OK, "keep_policy": {"last": "two"}}, "data.keep_policy.last"),
    ("restic.forget.v1", {**FORGET_OK, "prune": "yes"}, "data.prune"),
    ("restic.prime.v1", {"result": "ok", "ttl": 604800}, "'expires' is a required property"),
    ("restic.prime.v1", {"result": "fail"}, "'error' is a required property"),
    ("restic.prime.v1", {**PRIME_OK, "ttl": 0}, "data.ttl"),
    ("restic.status.v1", without(STATUS_OK, "prime"), "'prime' is a required property"),
    ("restic.status.v1", without(STATUS_OK, "commits"), "'commits' is a required property"),
    ("restic.status.v1", {**STATUS_OK, "result": "great"}, "data.result"),
    ("restic.status.v1", {**copy.deepcopy(STATUS_OK), "commits": {"run": {"result": "ok"}}}, "'time' is a required"),
    ("restic.status.v1", {**STATUS_OK, "prime": {"local": {"ttl_remaining": 5}}}, "'primed' is a required"),
])
def test_invalid_other_events(type, data, expect):
    msgs = problems(type, data, "x")
    assert any(expect in m for m in msgs), msgs


def test_prime_failure_and_unknown_status():
    assert problems("restic.prime.v1", {"result": "fail", "error": "pass: gpg failed"}, "local") == []
    empty = {"result": "unknown", "commits": {}, "prime": {"local": {"primed": False, "error": "keyring unavailable"}}}
    assert problems("restic.status.v1", empty, "backup") == []


def test_events_really_emitted_by_the_backup_script():
    """tests/data/restic-events.jsonl holds events made by running ~/scripts/backup against
    scratch repositories (paths sanitised): successes, a partial backup, failures, a copy with
    nothing to copy, prime failures and status roll-ups.  They must satisfy the schemas."""
    import json
    schemas = Schemas()
    events = [json.loads(line) for line in (Path(__file__).parent / "data" / "restic-events.jsonl").read_text().splitlines()]
    seen = set()
    for event in events:
        assert schemas.problems(event) == [], event
        data = event["data"]
        if "location" in data:      # the script classifies the destination the way the schema says
            assert data["location"] == ("remote" if data["repository"].startswith("sftp:") else "local")
        seen.add((event["type"], event["subject"].split("/")[0], data.get("result"), data.get("method"),
                  data.get("snapshots_copied") == 0))
    assert {("restic.commit.v1", "run", "partial", "backup", False),
            ("restic.commit.v1", "run", "fail", "backup", False),
            ("restic.commit.v1", "push", "ok", "copy", True),
            ("restic.commit.v1", "push", "fail", "copy", True),
            ("restic.forget.v1", "run", "fail", None, False),
            ("restic.prime.v1", "local", "fail", None, False),
            ("restic.status.v1", "backup", "partial", None, False)} <= seen
