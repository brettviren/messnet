from datetime import datetime, timedelta, timezone

import pytest

from messnet import tablequery as tq
from messnet.tablequery import Cell, Column, Query, Row

T0 = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
COLUMNS = [Column("name", "name"), Column("n", "count", tq.NUMBER), Column("when", "when", tq.TIME)]


def row(name, n, minutes):
    when = None if minutes is None else T0 - timedelta(minutes=minutes)
    return Row(name, {"name": Cell(name), "n": Cell(str(n), sort=n), "when": Cell(f"{minutes}m", sort=when)})


ROWS = [row("restic.commit.v1", 9, 30), row("restic.status.v1", 10, 5), row("messnet.link.v1", 2, None),
        row("demo.thing.v1", 100, 60)]


def names(result):
    return [r.key for r in result.rows]


def test_time_column_is_newest_first_by_default_with_unknown_last():
    res = tq.apply(ROWS, COLUMNS, Query())
    assert res.sort == "when" and res.descending
    assert names(res) == ["restic.status.v1", "restic.commit.v1", "demo.thing.v1", "messnet.link.v1"]


def test_ordering_by_a_column_either_way_and_numbers_are_numeric():
    asc = tq.apply(ROWS, COLUMNS, Query(sort="n"))
    assert names(asc) == ["messnet.link.v1", "restic.commit.v1", "restic.status.v1", "demo.thing.v1"]   # 2 9 10 100
    assert names(tq.apply(ROWS, COLUMNS, Query(sort="n", descending=True)))[0] == "demo.thing.v1"
    by_name = tq.apply(ROWS, COLUMNS, Query(sort="name"))
    assert names(by_name)[0] == "demo.thing.v1" and not by_name.descending
    oldest = tq.apply(ROWS, COLUMNS, Query(sort="when", descending=False))
    assert names(oldest)[-1] == "messnet.link.v1" and names(oldest)[0] == "demo.thing.v1"   # still last when missing


def test_no_time_column_keeps_the_given_order():
    cols = [Column("name", "name")]
    res = tq.apply([Row(k, {"name": Cell(k)}) for k in "cab"], cols, Query())
    assert res.sort is None and names(res) == ["c", "a", "b"]


@pytest.mark.parametrize("mode, pattern, want", [
    ("fuzzy", "rsc", ["restic.commit.v1", "restic.status.v1"]),          # r.s.c in order
    ("fuzzy", "v1 mess", ["messnet.link.v1"]),                         # every word, any order
    ("fuzzy", "RESTIC", ["restic.commit.v1", "restic.status.v1"]),
    ("fuzzy", "ts", ["restic.status.v1"]),                              # t then s
    ("fuzzy", "zz", []),
    ("glob", "restic.*", ["restic.commit.v1", "restic.status.v1"]),
    ("glob", "*.link.*", ["messnet.link.v1"]),
    ("glob", "restic", []),                                            # a glob matches the whole cell
    ("glob", "restic.s?atus.v[0-9]", ["restic.status.v1"]),
    ("regex", "^(demo|messnet)", ["demo.thing.v1", "messnet.link.v1"]),
    ("regex", "commit|thing", ["restic.commit.v1", "demo.thing.v1"]),
])
def test_modes_on_any_column(mode, pattern, want):
    res = tq.apply(ROWS, COLUMNS, Query(any=pattern, mode=mode, sort="name"))
    assert res.error is None and sorted(names(res)) == sorted(want)


def test_patterns_per_column_combine():
    res = tq.apply(ROWS, COLUMNS, Query(columns={"name": "restic", "n": "10"}))
    assert names(res) == ["restic.status.v1"]
    res = tq.apply(ROWS, COLUMNS, Query(columns={"n": "^\\d{1}$"}, mode="regex"))
    assert sorted(names(res)) == ["messnet.link.v1", "restic.commit.v1"]
    res = tq.apply(ROWS, COLUMNS, Query(any="restic", columns={"n": "1*"}, mode="glob", sort="name"))
    assert res.rows == []                                              # 'restic' is no whole cell in glob mode


def test_an_invalid_regex_keeps_every_row_and_says_why():
    res = tq.apply(ROWS, COLUMNS, Query(any="(", mode="regex"))
    assert len(res.rows) == 4 and "regular expression" in res.error
    with pytest.raises(tq.PatternError):
        tq.matcher("nope", "x")


def test_parse_reads_arguments_and_ignores_bad_ones():
    args = {"g_q": " rest ", "g_mode": "glob", "g_c_name": "x*", "g_sort": "n", "g_dir": "desc", "e_q": "other"}
    q = tq.parse(args, "g_", COLUMNS)
    assert (q.any, q.mode, q.columns, q.sort, q.descending) == ("rest", "glob", {"name": "x*"}, "n", True)
    q = tq.parse({"g_mode": "bogus", "g_sort": "nope", "g_dir": "sideways"}, "g_", COLUMNS)
    assert (q.mode, q.sort, q.descending, q.active) == ("fuzzy", None, None, False)


def test_heading_clicks_toggle_starting_from_the_columns_own_order():
    assert tq.toggled(Query(), COLUMNS, "name") == ("name", "asc")
    assert tq.toggled(Query(), COLUMNS, "when") == ("when", "asc")        # already newest-first by default: flip
    assert tq.toggled(Query(sort="name"), COLUMNS, "name") == ("name", "desc")
    assert tq.toggled(Query(sort="name", descending=True), COLUMNS, "name") == ("name", "asc")
    assert tq.toggled(Query(sort="name"), COLUMNS, "when") == ("when", "desc")    # a time column starts newest-first
