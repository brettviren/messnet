"""The pages of the web UI: hosts and events, the two views of the terminal UI.

Each shows a list (hosts, or event types), the events of the selected entry, and the selected event
laid out in full.  The selection is in the URL (``?host=beta&event=...``) so a page can be linked,
bookmarked and survives the periodic refresh; ``g_`` arguments narrow and order the list, ``e_``
those of the events (see ``tablequery``).
"""

from flask import render_template, request

from messnet import eventview, tuimodel, webmodel
from messnet.web import context, link, page, table_view


def _grouped(view: str, select: str, title: str) -> str:
    """The page for VIEW ('hosts' or 'types'); SELECT names the argument that selects an entry."""
    events = context().events()
    groups, group_result = table_view("g_", title, webmodel.GROUP_COLUMNS[view], webmodel.group_rows(events, view),
                                      empty="No state files yet. Is 'messnet serve' running, and has anything been emitted?")
    chosen = request.args.get(select) or (group_result.rows[0].key if group_result.rows else None)
    for row in groups.rows:
        row.href = link(**{select: row.key, "event": None})
    groups.selected = chosen

    shown = webmodel.event_rows(events, view, chosen) if chosen is not None else []
    table, event_result = table_view("e_", f"{webmodel.GROUP_NAME[view]}: {chosen}" if chosen else "events",
                                     webmodel.EVENT_COLUMNS[view], shown, empty="No events.",
                                     scroll_id=f"{view}:{chosen}")
    wanted = request.args.get("event") or (event_result.rows[0].key if event_result.rows else None)
    for row in table.rows:
        row.href = link(event=row.key)
    table.selected = wanted
    detail = next((e for e in events if tuimodel.event_key(e) == wanted), None)
    return render_template("grouped.html", groups=groups, events=table,
                           sections=eventview.describe(detail) if detail else None)


@page("/hosts", "Hosts")
def hosts() -> str:
    return _grouped("hosts", "host", "Hosts")


@page("/events", "Events")
def events() -> str:
    return _grouped("types", "type", "Event types")
