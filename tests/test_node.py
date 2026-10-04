import shutil

from messnet import state
from messnet.spool import Spool


def test_emit_sequences_per_scope(make_node):
    n = make_node("a")
    assert [n.emit("t.v1")["seq"] for _ in range(3)] == [1, 2, 3]
    assert n.emit("t.v1", scope="host")["seq"] == 1
    assert n.store.version_vector() == {"a/all": 3, "a/host": 1}


def test_host_and_lan_events_stay_out_of_shared_spool(make_node):
    n = make_node("a")
    n.emit("t.v1", scope="host")
    n.emit("t.v1", scope="lan")
    assert Spool(n.cfg.spool).files() == []


def test_seq_recovers_from_spool_after_store_loss(make_node, tmp_path):
    n = make_node("a")
    n.emit("t.v1")
    n.emit("t.v1")
    n.close()
    n.cfg.db.unlink()
    m = make_node("a")   # same paths, fresh store
    assert m.emit("t.v1")["seq"] == 3
    assert m.ingest() == 2


def test_shared_spool_ingest_dedupes(make_node, tmp_path):
    shared = tmp_path / "sync"
    a, b = make_node("a", spool=shared), make_node("b", spool=shared)
    a.emit("t.v1", {"n": 1})
    b.emit("t.v1", {"n": 2})
    assert b.ingest() == 1
    assert b.ingest() == 0
    assert {ev["origin"] for _, ev in b.store.query()} == {"a", "b"}


def test_partial_and_conflict_lines_ignored(make_node, tmp_path):
    a, b = make_node("a"), make_node("b")
    a.emit("t.v1")
    src = a.spools["all"].origin_files("a")[0]
    dst = b.cfg.spool / "a"
    dst.mkdir(parents=True)
    shutil.copy(src, dst / src.name)
    shutil.copy(src, dst / (src.stem + ".sync-conflict-x.jsonl"))
    with open(dst / src.name, "a") as fp:
        fp.write('{"partial": ')
    assert b.ingest() == 1
    with open(dst / src.name, "a") as fp:
        fp.write('true}\n')       # completes into an invalid event: skipped
    assert b.ingest() == 0


def test_version_vector_contiguous(make_node):
    n = make_node("a")
    evs = [n.emit("t.v1") for _ in range(4)]
    m = make_node("b")
    for ev in (evs[0], evs[1], evs[3]):
        m.receive(ev)
    assert m.store.version_vector() == {"a/all": 2}
    m.receive(evs[2])
    assert m.store.version_vector() == {"a/all": 4}


def test_state_latest_per_key(make_node):
    n = make_node("a")
    n.emit("backup.run.v1", {"result": "fail"}, subject="push")
    n.emit("backup.run.v1", {"result": "ok"}, subject="push")
    n.emit("backup.run.v1", {"result": "ok"}, subject="local/x")
    assert state.update(n.store, n.cfg.state) == 3
    got = {ev["subject"]: ev["data"]["result"] for ev in state.read(n.cfg.state, ["backup.*"])}
    assert got == {"push": "ok", "local/x": "ok"}
    assert state.update(n.store, n.cfg.state) == 0
    assert list(state.read(n.cfg.state, ["other.*"])) == []


def test_emit_object(make_node):
    n = make_node("a")
    ev = n.emit_object({"type": "x.v1", "data": [1], "subject": "s", "seq": 99, "origin": "evil"})
    assert (ev["origin"], ev["seq"], ev["data"]) == ("a", 1, [1])


def test_ingest_since_skips_old_files(make_node, tmp_path):
    shared = tmp_path / "sync"
    a, b = make_node("a", spool=shared), make_node("b", spool=shared)
    a.emit("old.v1", time="2020-01-01T00:00:00+00:00")
    a.emit("new.v1")
    assert b.ingest(since="2021-01-01") == 1
    assert b.ingest() == 1


def test_seq_recovery_with_backdated_events(make_node):
    n = make_node("a")
    n.emit("now.v1")
    n.emit("past.v1", time="2020-01-01T00:00:00+00:00")
    assert n.spools["all"].last_seq("a") == 2
