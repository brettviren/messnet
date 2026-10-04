import pytest

from messnet.event import EventError, make_event, parse_assignments, validate
from messnet.filters import parse_since
from messnet.ulid import ulid


def test_ulid_sorts_by_time():
    a, b = ulid(1_000), ulid(2_000)
    assert len(a) == 26 and a < b


def test_parse_assignments():
    assert parse_assignments(["a=1", "b:=1", "c:=[1,2]", "d=x=y", "e:={\"k\":true}"]) == {
        "a": "1", "b": 1, "c": [1, 2], "d": "x=y", "e": {"k": True}}
    with pytest.raises(EventError):
        parse_assignments(["novalue"])
    with pytest.raises(EventError):
        parse_assignments(["k:=notjson"])


def test_make_event():
    ev = make_event("t.v1", {"x": 1}, origin="n", seq=1, subject="s")
    assert ev["specversion"] == "1.0" and ev["scope"] == "all" and ev["data"] == {"x": 1}
    with pytest.raises(EventError):
        validate({**ev, "seq": 0})
    with pytest.raises(EventError):
        validate({**ev, "scope": "galaxy"})


def test_parse_since():
    assert parse_since("2h", now=10_000.0) == 10_000.0 - 7200
    assert parse_since(None) is None
    with pytest.raises(ValueError):
        parse_since("yesterday")
