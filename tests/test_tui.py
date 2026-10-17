"""The terminal UI, driven headless with Textual's pilot."""

import asyncio
import json
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("textual")

from textual.widgets import DataTable  # noqa: E402

from messnet import publish  # noqa: E402
from messnet.tui import MessnetTui  # noqa: E402

from test_tuimodel import ev, make_state, put  # noqa: E402


def keys_of(table: DataTable) -> list[str]:
    return [k.value for k in table.rows]


def selected(table: DataTable) -> str | None:
    if not table.row_count:
        return None
    return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value


async def until(pilot, cond, timeout=5.0):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        await pilot.pause(0.05)
    raise AssertionError("condition not reached")


def app_for(state, **kw):
    kw.setdefault("check_every", 0.1)
    return MessnetTui(Path(state), kw.pop("target", None), kw.pop("latest_path", None), "alpha", **kw)


def run(coro):
    asyncio.run(coro)


def test_host_view_lists_hosts_and_shows_the_selected_event(tmp_path):
    make_state(tmp_path)

    async def main():
        app = app_for(tmp_path)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause(0.2)
            groups, events = app.query_one("#groups", DataTable), app.query_one("#events", DataTable)
            assert app.view == "hosts" and keys_of(groups) == ["alpha", "beta"]
            assert selected(groups) == "alpha" and groups.row_count == 2
            assert len(keys_of(events)) == 3                                 # alpha's three events, newest first
            assert "Link" in app.detail_text.title() or "LINK" in app.detail_text      # newest is the link event
            await pilot.press("down")                                         # beta
            await until(pilot, lambda: app.group_key == "beta")
            assert keys_of(events)[0].startswith("restic.commit.v1")
            assert "OUTCOME" in app.detail_text and "snapshot" in app.detail_text and "ababab" in app.detail_text
            await pilot.press("tab", "down")                                  # into the events table, next event
            await until(pilot, lambda: app.event_key and app.event_key.startswith("restic.status.v1"))
            assert "BACKUPS" in app.detail_text and "fail" in app.detail_text

    run(main())


def test_event_view_is_the_transpose(tmp_path):
    make_state(tmp_path)

    async def main():
        app = app_for(tmp_path)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("t")
            await until(pilot, lambda: app.view == "types")
            groups, events = app.query_one("#groups", DataTable), app.query_one("#events", DataTable)
            assert keys_of(groups) == ["demo.thing.v1", "messnet.link.v1", "restic.commit.v1", "restic.status.v1"]
            await pilot.press("down", "down", "down")                         # restic.status.v1
            await until(pilot, lambda: app.group_key == "restic.status.v1")
            assert [k.split("\x1f")[1] for k in keys_of(events)] == ["alpha", "beta"]       # the same type from both hosts
            assert [c.plain if hasattr(c, "plain") else c for c in events.get_row(keys_of(events)[0])][0] == "alpha"
            await pilot.press("h")
            await until(pilot, lambda: app.view == "hosts" and keys_of(groups) == ["alpha", "beta"])

    run(main())


def test_empty_state_says_what_to_do(tmp_path):
    async def main():
        app = app_for(tmp_path / "nothing")
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(0.2)
            assert app.query_one("#groups", DataTable).row_count == 0
            assert "messnet serve" in str(app.query_one("#detail").render())
            assert app.latest is None

    run(main())


def test_state_changes_appear_and_selection_is_kept(tmp_path):
    make_state(tmp_path)

    async def main():
        app = app_for(tmp_path)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("down")
            await until(pilot, lambda: app.group_key == "beta")
            put(tmp_path, ev("restic.commit.v1", "gamma", "run", "2026-10-11T11:59:00+00:00",
                             {"result": "fail", "method": "backup", "repository": "/r", "location": "local",
                              "paths": ["/h"], "error": "boom"}))
            groups = app.query_one("#groups", DataTable)
            await until(pilot, lambda: "gamma" in keys_of(groups))
            assert app.group_key == "beta" and selected(groups) == "beta"     # the user's place was kept

    run(main())


def test_latest_pane_follows_arrivals(tmp_path):
    make_state(tmp_path)

    async def main():
        app = app_for(tmp_path)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause(0.2)
            await until(pilot, lambda: app.feed == "state")
            assert "alpha demo.thing.v1" in app.latest_text                    # the file written last, to begin with
            app.arrived(ev("restic.commit.v1", "zed", "run", "2026-10-11T12:30:00+00:00",
                           {"result": "partial", "method": "backup", "repository": "/r", "location": "local",
                            "paths": ["/h"], "snapshot_id": "cd" * 32, "error": "x", "error_count": 3,
                            "total_files_processed": 5, "total_bytes_processed": 10}))
            assert "zed restic.commit.v1 partial" in app.latest_text and "3 unreadable" in app.latest_text

    run(main())


def test_ages_advance_without_new_events(tmp_path):
    make_state(tmp_path)
    when = datetime.now(timezone.utc) - timedelta(seconds=30)

    async def main():
        app = app_for(tmp_path, redraw_every=0.2)
        async with app.run_test(size=(130, 40)) as pilot:
            await until(pilot, lambda: app.feed == "state")
            app.arrived(ev("demo.thing.v1", "alpha", "s", when.isoformat(), {"n": 1}))
            first = app.latest_age
            assert first.endswith("s ago")
            await until(pilot, lambda: app.latest_age != first, timeout=3)     # no further arrival needed

    run(main())


@pytest.fixture
def sockdir():
    d = Path(tempfile.mkdtemp(prefix="mn-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_live_events_through_the_concentrator(make_node, tmp_path, sockdir):
    node = make_node("alpha")
    make_state(tmp_path)
    spec = publish.check_publish({"kind": "unix", "path": str(sockdir / "e.sock")})

    async def main():
        server = await publish.serve(node, spec)
        try:
            app = app_for(tmp_path, target=f"unix:{spec['path']}")
            async with app.run_test(size=(130, 40)) as pilot:
                await until(pilot, lambda: app.live.startswith("connected"))
                node.emit("live.v1", {"hello": "world"}, subject="x")
                await until(pilot, lambda: "alpha live.v1" in app.latest_text and "hello=world" in app.latest_text)
                node.emit("live.v1", {"hello": "again"}, subject="x")
                await until(pilot, lambda: "hello=again" in app.latest_text)
        finally:
            server.close()
            server.cleanup()

    run(main())


def test_without_a_node_the_latest_file_is_watched(tmp_path):
    make_state(tmp_path)
    latest = tmp_path / "latest.json"
    latest.write_text(json.dumps(ev("older.v1", "alpha", None, "2026-10-11T12:00:00+00:00", {"v": 0})))

    async def main():
        app = app_for(tmp_path, target=None, latest_path=latest)
        async with app.run_test(size=(130, 40)) as pilot:
            await until(pilot, lambda: app.live.startswith("polling latest.json"))
            latest.write_text(json.dumps(ev("watched.v1", "alpha", None, "2026-10-11T12:31:00+00:00", {"v": 1})))
            await until(pilot, lambda: "watched.v1" in app.latest_text, timeout=5)
            assert app.feed == "file" and "latest.json" in app.query_one("#latest").border_title

    run(main())


def test_no_live_source_follows_the_most_recently_changed_state_file(tmp_path, sockdir):
    """A node that predates the publisher (or is not running): nothing reports arrivals, yet the
    latest-event pane must not freeze; it follows whichever state file changed last."""
    make_state(tmp_path)

    async def main():
        # a socket path with nothing listening, and no latest file: exactly what an old daemon leaves
        app = app_for(tmp_path, target=f"unix:{sockdir / 'none.sock'}", latest_path=tmp_path / "latest.json")
        async with app.run_test(size=(130, 40)) as pilot:
            await until(pilot, lambda: app.feed == "state")
            assert "newest state file" in app.query_one("#latest").border_title
            first = app.latest_text
            assert first
            put(tmp_path, ev("restic.commit.v1", "gamma", "run", "2026-10-11T11:00:00+00:00",     # an old event time...
                             {"result": "ok", "method": "backup", "repository": "/r", "location": "local",
                              "paths": ["/h"], "snapshot_id": "ef" * 32, "total_files_processed": 1,
                              "total_bytes_processed": 2}))                                          # ...but it just arrived
            await until(pilot, lambda: "gamma restic.commit.v1" in app.latest_text)
            put(tmp_path, ev("demo.thing.v1", "alpha", None, "2026-10-11T10:00:00+00:00", {"n": 555}))
            await pilot.press("r")                                       # refresh gives the same answer
            await until(pilot, lambda: "alpha demo.thing.v1" in app.latest_text and "n=555" in app.latest_text)

    run(main())


def test_state_only_mode_with_no_target_or_file(tmp_path):
    make_state(tmp_path)

    async def main():
        app = app_for(tmp_path, target=None, latest_path=None)
        async with app.run_test(size=(130, 40)) as pilot:
            await until(pilot, lambda: app.feed == "state")
            put(tmp_path, ev("late.v1", "beta", None, "2026-10-01T00:00:00+00:00", {"v": 1}))
            await until(pilot, lambda: "beta late.v1" in app.latest_text)

    run(main())


def test_title_says_when_the_feed_is_live(make_node, tmp_path, sockdir):
    node = make_node("alpha")
    make_state(tmp_path)
    spec = publish.check_publish({"kind": "unix", "path": str(sockdir / "e.sock")})

    async def main():
        server = await publish.serve(node, spec)
        try:
            app = app_for(tmp_path, target=f"unix:{spec['path']}")
            async with app.run_test(size=(130, 40)) as pilot:
                await until(pilot, lambda: app.feed == "publisher")
                assert app.query_one("#latest").border_title == "Latest event · live"
                shown = app.latest_text
                put(tmp_path, ev("later.v1", "beta", None, "2026-10-12T00:00:00+00:00", {"v": 1}))   # state changes...
                await pilot.pause(0.5)
                assert app.latest_text == shown            # ...do not override what the live feed says
        finally:
            server.close()
            server.cleanup()

    run(main())


def test_unreachable_node_falls_back_to_the_file(tmp_path, sockdir):
    make_state(tmp_path)
    latest = tmp_path / "latest.json"
    latest.write_text(json.dumps(ev("fromfile.v1", "alpha", None, "2026-10-11T12:31:00+00:00", {"v": 1})))

    async def main():
        app = app_for(tmp_path, target=f"unix:{sockdir / 'none.sock'}", latest_path=latest)
        async with app.run_test(size=(130, 40)) as pilot:
            await until(pilot, lambda: app.live.startswith("polling latest.json"))
            assert "fromfile.v1" in app.latest_text and app.feed == "file"

    run(main())


def test_tui_command_reports_a_missing_toolkit_cleanly(tmp_path, monkeypatch):
    import sys
    from test_cli import run as cli_run
    for name in [m for m in sys.modules if m == "textual" or m.startswith("textual.")]:
        monkeypatch.setitem(sys.modules, name, None)                  # as if it were not installed
    monkeypatch.delitem(sys.modules, "messnet.tui", raising=False)
    out = cli_run(tmp_path, "tui", codes=(1,))
    assert "tui" in out and "textual" in out
