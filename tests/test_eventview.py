import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from messnet import eventview as ev_

FIXTURES = [json.loads(line) for line in (Path(__file__).parent / "data" / "restic-events.jsonl").read_text().splitlines()]
NOW = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)


def full(fixture: dict, **over) -> dict:
    """A fixture (type, subject, scope, data) as a complete event."""
    ev = {"specversion": "1.0", "id": "01M4NC6QJJHD8QB2BGYYJZKR1C", "time": "2026-10-11T11:55:00+00:00",
          "source": "messnet://hokum/bv/backup", "origin": "hokum", "seq": 7, **fixture}
    return {**ev, **over}


def pick(type_, **conds):
    return [f for f in FIXTURES if f["type"] == type_ and all(f["data"].get(k) == v for k, v in conds.items())]


def sections_by_title(sections):
    return {s.title: s for s in sections}


def test_helpers():
    assert ev_.human_bytes(0) == "0 B" and ev_.human_bytes(1536) == "1.5 KiB" and ev_.human_bytes(5 * 1024**3) == "5.0 GiB"
    assert [ev_.human_duration(x) for x in (0.7, 59, 90, 3700, 90000)] == ["0.7s", "59s", "1m30s", "1h01m", "1d1h"]
    t = NOW - timedelta(minutes=5)
    assert ev_.age(t, NOW) == "5m ago" and ev_.age(NOW - timedelta(seconds=3), NOW) == "3s ago"
    assert ev_.age(NOW - timedelta(days=2), NOW) == "2d ago" and ev_.age(None, NOW) == "-"
    assert ev_.age(NOW + timedelta(hours=1), NOW) == "in the future"
    assert ev_.clip("a  b\nc" * 30, 10) == "a b c a b …" or len(ev_.clip("x" * 100, 10)) == 10
    assert ev_.parse_time("garbage") is None and ev_.parse_time("2026-10-11T08:00:00").tzinfo is not None


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f"{f['type']}:{f['subject']}:{f['data'].get('result')}")
def test_every_real_event_has_a_formatter(fixture):
    ev = full(fixture)
    s = ev_.summary(ev)
    assert s.text and "\n" not in s.text
    sections = ev_.describe(ev, NOW)
    assert sections[-1].title == "Event" and sections[-1].lines[1] == ev_.Line("host", "hokum")
    assert len(sections) >= 2 and not (sections[0].title == "Data")        # not the generic fallback
    text = ev_.to_text(sections)
    assert "Event".upper() in text and "hokum" in text


def test_commit_summaries():
    ok = full(pick("restic.commit.v1", method="backup", result="ok")[0])
    s = ev_.summary(ok)
    assert s.style == ev_.OK and s.text.startswith("ok: ") and "files" in s.text and "→" in s.text and " in " in s.text
    partial = full(pick("restic.commit.v1", result="partial")[0])
    assert ev_.summary(partial).style == ev_.WARN and "unreadable" in ev_.summary(partial).text
    failed = full(pick("restic.commit.v1", result="fail", method="backup")[0])
    assert ev_.summary(failed).style == ev_.FAIL and ev_.summary(failed).text.startswith("fail: ")
    copy = full(pick("restic.commit.v1", method="copy", result="ok")[0])
    assert "copied" in ev_.summary(copy).text and "snapshot" in ev_.summary(copy).text


def test_commit_detail_has_the_facts_a_reader_needs():
    ok = full(pick("restic.commit.v1", method="backup", result="ok")[0])
    secs = sections_by_title(ev_.describe(ok, NOW))
    assert {"Outcome", "Where", "Files", "Data", "Timing", "Event"} <= set(secs)
    where = {l.label: l.value for l in secs["Where"].lines}
    assert where["location"] == "local" and where["snapshot"] == ok["data"]["snapshot_id"] and "paths" in where
    assert any("new" in l.value for l in secs["Files"].lines)
    assert secs["Outcome"].lines[0] == ev_.Line("result", "ok", ev_.OK)
    copy = full(pick("restic.commit.v1", method="copy", result="ok", snapshots_copied=7)[0])
    secs = sections_by_title(ev_.describe(copy, NOW))
    table = next(s for t, s in secs.items() if t.startswith("Snapshots copied"))
    assert table.columns == ["new", "from", "packs"] and len(table.rows) == 7
    failed = full(pick("restic.commit.v1", result="fail", method="backup")[0])
    out = sections_by_title(ev_.describe(failed, NOW))["Outcome"]
    assert any(l.label == "error" and l.style == ev_.FAIL for l in out.lines)


def test_status_tables_and_password_warning():
    ev = full({"type": "restic.status.v1", "subject": "backup", "scope": "all", "data": {
        "result": "partial",
        "commits": {"run": {"result": "ok", "time": "2026-10-11T11:00:00+00:00", "snapshot_id": "ab" * 32,
                            "total_files_processed": 12, "total_bytes_processed": 4096},
                    "push": {"result": "fail", "time": "2026-10-11T10:00:00+00:00", "error": "Fatal: wrong password"}},
        "forget": {"run": {"result": "ok", "time": "2026-10-11T10:59:00+00:00", "snapshots_removed": 2}},
        "prime": {"local": {"primed": True, "ttl_remaining": 3600},
                  "remote": {"primed": False}}}})
    s = ev_.summary(ev)
    assert s.style == ev_.WARN and "push fail" in s.text and "remote NOT PRIMED" in s.text
    secs = sections_by_title(ev_.describe(ev, NOW))
    commits = secs["Latest commit per stream"]
    assert commits.columns == ["stream", "result", "when", "what"]
    assert [r[0] for r in commits.rows] == ["push", "run"] and commits.row_styles == [ev_.FAIL, ev_.OK]
    assert commits.rows[0][2] == "2h ago" and "wrong password" in commits.rows[0][3]
    pw = {l.label: l for l in secs["Password cache"].lines}
    assert pw["local"].style == ev_.WARN and "expires in 1h00m" in pw["local"].value      # under a day left
    assert pw["remote"].style == ev_.FAIL and "NOT PRIMED" in pw["remote"].value
    bad = full({"type": "restic.status.v1", "subject": "backup", "scope": "all",
                "data": {"result": "ok", "commits": {}, "prime": {"local": {"primed": False}}}})
    assert ev_.summary(bad).style == ev_.WARN                       # backups fine but the password is not


def test_prime_forget_and_link():
    prime = full(pick("restic.prime.v1", result="ok")[0], time="2026-10-11T08:00:00+00:00")
    prime["data"] = {**prime["data"], "ttl": 604800, "expires": "2026-10-18T08:00:00+00:00"}
    assert "cached for 7d0h" in ev_.summary(prime).text
    secs = sections_by_title(ev_.describe(prime, NOW))
    assert any(l.label == "expires" and "in 6d" in l.value and l.style == ev_.OK for l in secs["Password priming"].lines)
    expired = {**prime, "data": {**prime["data"], "expires": "2026-10-10T08:00:00+00:00"}}
    assert any("expired" in l.value and l.style == ev_.FAIL for l in sections_by_title(ev_.describe(expired, NOW))["Password priming"].lines)
    forget = full(pick("restic.forget.v1", result="ok")[0])
    assert "kept" in ev_.summary(forget).text and "removed" in ev_.summary(forget).text
    assert any(l.label == "policy" for l in sections_by_title(ev_.describe(forget, NOW))["Retention"].lines)
    link = full({"type": "messnet.link.v1", "subject": "haiku", "scope": "host",
                 "data": {"state": "down", "link": "tcp:h:1", "scope": "all", "sent": 3, "received": 4, "error": "boom"}})
    s = ev_.summary(link)
    assert s.style == ev_.FAIL and "down with haiku via tcp:h:1" in s.text and "sent 3, received 4" in s.text and "boom" in s.text


def test_unknown_types_get_the_generic_layout():
    ev = full({"type": "demo.thing.v1", "subject": "x", "scope": "all",
               "data": {"result": "ok", "n": 3, "nested": {"a": [1, 2], "b": {"c": True}}, "items": [{"k": 1}, {"k": 2}]}})
    s = ev_.summary(ev)
    assert s.style == ev_.OK and "result=ok" in s.text and "n=3" in s.text and "nested" not in s.text
    data = ev_.describe(ev, NOW)[0]
    got = {l.label: l.value for l in data.lines}
    assert data.title == "Data" and got["nested.a"] == "1, 2" and got["nested.b.c"] == "yes" and got["items[1].k"] == "2"
    assert ev_.summary(full({"type": "x.y.v1", "data": "just text"})).text == "just text"
    assert ev_.describe(full({"type": "x.y.v1"}), NOW)[0].lines[0].value == "(no data)"
    nosubject = ev_.describe(full({"type": "x.y.v1"}), NOW)[-1].lines
    assert next(l for l in nosubject if l.label == "subject").value == "—"


@pytest.mark.parametrize("data", [None, {}, {"result": "ok"}, {"result": 5, "commits": 3}, "str", [1]])
def test_malformed_events_never_break_the_display(data):
    for type_ in ("restic.commit.v1", "restic.status.v1", "restic.prime.v1", "restic.forget.v1", "messnet.link.v1"):
        ev = full({"type": type_, "data": data})
        assert ev_.summary(ev).text is not None
        assert ev_.to_text(ev_.describe(ev, NOW))


def test_plain_text_rendering():
    ev = full(pick("restic.commit.v1", method="copy", result="ok", snapshots_copied=7)[0])
    text = ev_.to_text(ev_.describe(ev, NOW))
    assert "OUTCOME" in text and "SNAPSHOTS COPIED (7)" in text and "new       from" in text
