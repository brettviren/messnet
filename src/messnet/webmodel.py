"""What the web UI's lists show: the TUI's host and event-type views as sortable, searchable tables.

Builds ``tablequery`` rows from the events (as ``tuimodel.load`` yields them) and the groupings of
``tuimodel``; ``eventview`` supplies the text.  No web framework in here.
"""

from datetime import datetime, timezone

from messnet import eventview, tuimodel
from messnet.tablequery import NUMBER, TIME, Cell, Column, Row

HOSTS, TYPES = "hosts", "types"

STATUS = {eventview.OK: "ok", eventview.WARN: "warn", eventview.FAIL: "fail"}

GROUP_COLUMNS = {
    HOSTS: [Column("name", "host"), Column("count", "events", NUMBER), Column("newest", "newest", TIME),
            Column("status", "status")],
    TYPES: [Column("name", "event type"), Column("count", "hosts", NUMBER), Column("newest", "newest", TIME),
            Column("status", "status")],
}
EVENT_COLUMNS = {
    HOSTS: [Column("who", "type"), Column("subject", "subject"), Column("time", "time", TIME),
            Column("summary", "summary")],
    TYPES: [Column("who", "host"), Column("subject", "subject"), Column("time", "time", TIME),
            Column("summary", "summary")],
}
GROUP_NAME = {HOSTS: "host", TYPES: "event type"}


def time_cell(when: datetime | None, now: datetime) -> Cell:
    """An age ('5m ago'), ordered by the moment itself; patterns see the age and the clock time."""
    full = eventview.local_time(when)
    return Cell(eventview.age(when, now), sort=when, search=f"{eventview.age(when, now)} {full}", title=full,
                time=when.isoformat() if when else None)


def group_rows(events: list[dict], view: str, now: datetime | None = None) -> list[Row]:
    """One row per host (VIEW 'hosts') or event type ('types')."""
    now = now or datetime.now(timezone.utc)
    groups = tuimodel.hosts(events) if view == HOSTS else tuimodel.types(events)
    return [Row(g.key, {"name": Cell(g.key, style=g.style), "count": Cell(str(g.count), sort=g.count),
                        "newest": time_cell(g.newest, now), "status": Cell(STATUS.get(g.style, "-"), style=g.style)},
                g.style) for g in groups]


def event_rows(events: list[dict], view: str, key: str, now: datetime | None = None) -> list[Row]:
    """The events of the host or event type named KEY."""
    now = now or datetime.now(timezone.utc)
    out = []
    for r in tuimodel.rows(events, view, key, now, width=200):
        ev = r.event
        who, subject, _, text = r.cells
        out.append(Row(r.key, {"who": Cell(who), "subject": Cell(subject), "time": time_cell(eventview.parse_time(ev.get("time")), now),
                               "summary": Cell(text, style=r.style)}, r.style))
    return out
