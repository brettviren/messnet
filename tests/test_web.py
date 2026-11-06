"""The web UI, through Flask's test client."""

import threading

import pytest

pytest.importorskip("flask")

from test_tuimodel import ev, make_state, put  # noqa: E402

from messnet import feed, web  # noqa: E402


@pytest.fixture
def client(tmp_path):
    make_state(tmp_path)
    app = web.create_app(tmp_path, "alpha", feed.LatestFeed(tmp_path))
    return app.test_client()


def get(client, url, status=200):
    res = client.get(url)
    assert res.status_code == status, res.text
    return res.text


def test_root_goes_to_the_first_page_and_every_page_has_the_navigation(client):
    res = client.get("/")
    assert res.status_code == 302 and res.headers["Location"].endswith("/hosts")
    for path in ("/hosts", "/events"):
        html = get(client, path)
        assert 'href="/hosts"' in html and 'href="/events"' in html           # persistent navigation
        assert html.count('" aria-current="page">') == 1
    assert 'href="/events" aria-current="page"' in get(client, "/events")
    get(client, "/nowhere", 404)


def test_hosts_page_lists_hosts_their_events_and_the_selected_one(client):
    html = get(client, "/hosts")
    assert "alpha" in html and "beta" in html
    assert "messnet.link.v1" in html                       # alpha, the first host: its events
    assert "Event" in html and "Latest event" in html      # detail and latest panels
    html = get(client, "/hosts?host=beta")
    assert "restic.commit.v1" in html and "restic.status.v1" in html and "demo.thing.v1" not in html
    assert "snapshot" in html and "ababab" in html          # the newest of beta's events is laid out


def test_events_page_is_the_transpose(client):
    html = get(client, "/events")
    for t in ("demo.thing.v1", "messnet.link.v1", "restic.commit.v1", "restic.status.v1"):
        assert t in html
    html = get(client, "/events?type=restic.status.v1")
    assert 'href="/events?type=restic.status.v1&amp;event=' in html or "event=restic.status.v1" in html
    assert "alpha" in html and "beta" in html


def test_an_event_can_be_selected_and_the_selection_is_in_the_links(client):
    html = get(client, "/hosts?host=beta")
    link = [part for part in html.split('"') if part.startswith("/hosts?host=beta&amp;event=restic.status")][0]
    detail = get(client, link.replace("&amp;", "&"))
    assert "BACKUPS" in detail.upper() and "fail" in detail
    assert 'class="sel"' in detail


def test_listings_are_narrowed_by_each_mode(client):
    html = get(client, "/hosts?host=beta&e_q=status")
    assert "restic.status.v1" in html and "restic.commit.v1" not in html.split("Event</h2>")[0].split("<tbody>")[2]
    glob = get(client, "/events?g_mode=glob&g_c_name=restic.*")
    assert "restic.commit.v1" in glob and "demo.thing.v1" not in glob.split("<tbody>")[1]
    rx = get(client, "/events?g_mode=regex&g_q=%5E(demo|messnet)")
    body = rx.split("<tbody>")[1].split("</tbody>")[0]
    assert "demo.thing.v1" in body and "messnet.link.v1" in body and "restic.status.v1" not in body
    assert "regular expression" in get(client, "/events?g_mode=regex&g_q=(")        # reported, not a crash


def order_of(html, names):
    body = html.split("<tbody>")[1].split("</tbody>")[0]
    return sorted(names, key=body.index)


def test_default_order_is_newest_first_and_headings_reverse_it(client):
    names = ["demo.thing.v1", "messnet.link.v1", "restic.commit.v1", "restic.status.v1"]
    newest_first = order_of(get(client, "/events"), names)
    assert newest_first == ["messnet.link.v1", "restic.status.v1", "restic.commit.v1", "demo.thing.v1"]
    oldest_first = order_of(get(client, "/events?g_sort=newest&g_dir=asc"), names)
    assert oldest_first == newest_first[::-1]
    assert order_of(get(client, "/events?g_sort=name&g_dir=desc"), names) == sorted(names, reverse=True)


def test_forms_keep_the_selection_and_other_table(client):
    html = get(client, "/hosts?host=beta&e_q=restic&g_q=b")
    assert '<input type="hidden" name="host" value="beta">' in html
    assert '<input type="hidden" name="e_q" value="restic">' in html            # the hosts form keeps the events filter
    assert '<input type="hidden" name="g_q" value="b">' in html                 # and the events form keeps the hosts one
    assert 'name="e_q" value="restic" placeholder' in html and 'name="g_q" value="b" placeholder' in html


def test_html_in_events_is_escaped(tmp_path):
    make_state(tmp_path)
    put(tmp_path, ev("demo.thing.v1", "evil", "<img src=x onerror=alert(1)>", "2026-10-12T00:00:00+00:00", {"n": "<b>x</b>"}))
    html = web.create_app(tmp_path).test_client().get("/hosts?host=evil").text
    assert "<img src=x onerror=alert(1)>" not in html and "&lt;img src=x onerror=alert(1)&gt;" in html


def test_latest_event_panel_follows_the_feed(tmp_path):
    make_state(tmp_path)
    updates = feed.LatestFeed(tmp_path)
    client = web.create_app(tmp_path, "alpha", updates).test_client()
    assert "alpha" in get(client, "/hosts") and "Waiting" not in get(client, "/hosts")      # newest in the state, to begin with
    updates.status("connected to x", feed.PUBLISHER)
    updates.arrived(ev("restic.commit.v1", "zed", "run", "2026-10-11T12:30:00+00:00",
                       {"result": "partial", "method": "backup", "repository": "/r", "location": "local", "paths": ["/h"],
                        "snapshot_id": "cd" * 32, "error": "x", "error_count": 3,
                        "total_files_processed": 5, "total_bytes_processed": 10}))
    for path in ("/hosts", "/events"):
        panel = get(client, path).split('id="latest"')[1]
        assert "Latest event · live" in panel and "zed" in panel and "3 unreadable" in panel


def test_empty_state_says_what_to_do(tmp_path):
    html = web.create_app(tmp_path / "nothing").test_client().get("/hosts").text
    assert "messnet serve" in html and "Waiting for events" in html


def test_a_page_can_be_added(tmp_path):
    from flask import render_template_string
    before = list(web.PAGES)
    try:
        @web.page("/extra", "Extra", latest=False)
        def extra():
            return render_template_string('{% extends "base.html" %}{% block content %}hello extra{% endblock %}')
        client = web.create_app(tmp_path).test_client()
        html = get(client, "/extra")
        assert "hello extra" in html and 'href="/extra" aria-current="page"' in html and 'id="latest"' not in html
        assert 'href="/extra"' in get(client, "/hosts")
    finally:
        web.PAGES[:] = before


def test_pages_carry_their_version_and_a_local_clock_for_ages(client):
    html = get(client, "/hosts")
    assert 'data-version="0"' in html and 'data-fallback="3600"' in html
    body = html.split("<tbody>")[1]
    assert '<time class="age" datetime="2026-10-11T11:55:00+00:00">' in body
    assert '<time class="age dim" datetime="' in html.split('id="latest"')[1]
    assert 'new EventSource("/_changes")' in html and "setInterval" not in html           # no polling


def read_message(res, timeout=5.0):
    """The next data line of a server-sent-event response, skipping comments."""
    done = threading.Event()
    got = []

    def reader():
        for chunk in res.response:
            for line in chunk.decode().splitlines():
                if line.startswith("data:"):
                    got.append(line[5:].strip())
                    done.set()
                    return
    threading.Thread(target=reader, daemon=True).start()
    assert done.wait(timeout), "no message"
    return got[0]


def test_a_changed_state_file_is_pushed_and_quiet_means_a_heartbeat(tmp_path):
    make_state(tmp_path)
    app = web.create_app(tmp_path, "alpha", feed.LatestFeed(tmp_path), heartbeat=0.1, watch=0.05)
    client = app.test_client()
    version = client.get("/hosts").text.split('data-version="')[1].split('"')[0]
    res = client.get("/_changes", buffered=False)
    try:
        assert res.mimetype == "text/event-stream" and res.headers["Cache-Control"] == "no-cache"
        assert read_message(res) == version                          # the current version, to begin with
        put(tmp_path, ev("demo.thing.v1", "gamma", None, "2026-10-12T00:00:00+00:00", {"n": 1}))
        assert int(read_message(res)) == int(version) + 1            # pushed without being asked
    finally:
        res.close()


def test_a_change_between_drawing_and_connecting_is_reported_at_once(tmp_path):
    make_state(tmp_path)
    app = web.create_app(tmp_path, watch=0.05)
    client = app.test_client()
    drawn = int(client.get("/hosts").text.split('data-version="')[1].split('"')[0])
    put(tmp_path, ev("demo.thing.v1", "gamma", None, "2026-10-12T00:00:00+00:00", {"n": 1}))
    res = client.get("/_changes", buffered=False)
    try:
        assert int(read_message(res)) > drawn                         # the page sees it differs and fetches
    finally:
        res.close()


def test_a_quiet_stream_sends_heartbeats_the_page_ignores(tmp_path):
    make_state(tmp_path)
    client = web.create_app(tmp_path, heartbeat=0.05, watch=0.05).test_client()
    res = client.get("/_changes", buffered=False)
    try:
        chunks = iter(res.response)
        seen = [next(chunks).decode() for _ in range(3)]
        assert seen[0].startswith("retry:") and "data: " in seen[0]
        assert seen[1:] == [": alive\n\n", ": alive\n\n"]                # comments: no 'data:' line, so no page action
    finally:
        res.close()


def test_every_cell_of_a_row_selects_it_and_only_the_secondary_list_scrolls(client):
    html = get(client, "/hosts?host=beta")
    groups, events = html.split('aria-label="Event"')[0].split('<section class="box" aria-label=')[1:3]
    for table in (groups, events):
        for row in table.split("<tbody>")[1].split("</tbody>")[0].split("<tr")[1:]:
            assert row.count("<a href=") == row.count("<td")                    # a link in every cell
    cells = events.split("<tbody>")[1].split("<tr")[1]
    assert len({part.split('"')[0] for part in cells.split('<a href="')[1:]}) == 1       # all to the same event
    assert 'class="scroll" data-scroll="hosts:beta"' in events and "data-scroll" not in groups
    assert 'data-scroll="types:restic.status.v1"' in get(client, "/events?type=restic.status.v1")
