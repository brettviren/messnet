"""``messnet tui``: watch the network from a terminal.

The screen has a list on the left (hosts, or event types: ``h`` / ``t`` switch), the events of the
selected entry on the right with the selected event laid out below them, and, across the bottom,
the most recent event to arrive from anywhere, which updates as it happens.

Content comes from the state files (``tuimodel``) and is formatted by ``eventview``; this module
only arranges it.  Live updates come from the node's concentrator (``subscribe``) when a
``messnet serve`` is publishing, and from the ``latest`` file otherwise; with neither the screen
still follows the state files, a little less promptly.
"""

import asyncio
import contextlib
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import DataTable, Footer, Header, Static

from messnet import eventview, subscribe, tuimodel

STYLES = {eventview.OK: "green", eventview.WARN: "yellow", eventview.FAIL: "bold red",
          eventview.DIM: "dim", eventview.KEY: "cyan", "": ""}
DOT = {eventview.OK: "green", eventview.WARN: "yellow", eventview.FAIL: "red", "": "grey50", eventview.DIM: "grey50"}


def render_sections(sections: list[eventview.Section]) -> Group:
    """Sections as Rich renderables: titled blocks of label/value lines and small tables."""
    parts: list = []
    for sec in sections:
        parts.append(Text(sec.title, style="bold cyan"))
        if sec.lines:
            grid = Table.grid(padding=(0, 2))
            grid.add_column(style="dim", no_wrap=True)
            grid.add_column(overflow="fold")
            for line in sec.lines:
                grid.add_row(line.label, Text(line.value, style=STYLES.get(line.style, "")))
            parts.append(grid)
        if sec.columns:
            table = Table(box=None, padding=(0, 2), header_style="dim", show_edge=False, pad_edge=False)
            for column in sec.columns:
                table.add_column(column, overflow="fold")
            styles = sec.row_styles or [""] * len(sec.rows)
            for row, style in zip(sec.rows, styles, strict=False):
                table.add_row(*[Text(str(c), style=STYLES.get(style, "") if sec.columns[i] == "result" else "")
                                for i, c in enumerate(row)])
            parts.append(table)
        parts.append(Text(""))
    return Group(*parts)


def render_latest(ev: dict, now: datetime) -> Group:
    """The compact 'latest event' block: who and when, the summary, then a few key facts."""
    when = eventview.parse_time(ev.get("time"))
    s = eventview.summary(ev)
    head = Text()
    head.append(f"{eventview.age(when, now):>9}  ", style="dim")
    head.append(str(ev.get("origin", "?")), style="bold")
    head.append("  ")
    head.append(str(ev.get("type", "?")), style="cyan")
    if ev.get("subject"):
        head.append(" · ", style="dim")
        head.append(str(ev["subject"]), style="dim")
    body = [head, Text(s.text, style=STYLES.get(s.style, ""))]
    first = eventview.describe(ev, now)[0]
    for line in first.lines[:3] if eventview.has_formatter(ev.get("type", "")) else []:
        if line.label and line.label not in ("result", "overall"):
            row = Text()
            row.append(f"{line.label}: ", style="dim")
            row.append(line.value, style=STYLES.get(line.style, ""))
            body.append(row)
    return Group(*body)


class MessnetTui(App[None]):
    TITLE = "messnet"
    CSS = """
    #main { height: 1fr; }
    #groups { width: 38; height: 1fr; border: round $primary-darken-1; }
    #right { width: 1fr; }
    #events { height: 40%; border: round $primary-darken-1; }
    #detail-box { height: 1fr; border: round $primary-darken-1; padding: 0 1; }
    #latest { height: 8; border: round $success; padding: 0 1; }
    DataTable:focus { border: round $accent; }
    """
    BINDINGS = [
        Binding("h", "view('hosts')", "Hosts"),
        Binding("t", "view('types')", "Event types"),
        Binding("tab", "focus_next", "Next pane"),
        Binding("r", "reload", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, state_root: Path, target: str | None = None, latest_path: Path | None = None,
                 node: str = "", check_every: float = 1.5, redraw_every: float = 5.0):
        super().__init__()
        self.state_root, self.target, self.latest_path, self.node = Path(state_root), target, latest_path, node
        self.check_every, self.redraw_every = check_every, redraw_every
        self.view = "hosts"
        self.events: list[dict] = []
        self.by_key: dict[str, dict] = {}
        self.group_key: str | None = None
        self.event_key: str | None = None
        self.latest: dict | None = None
        self.live = "starting"
        self.feed = "starting"            # where 'latest' comes from: publisher | file | state
        self.detail_text = ""             # the detail and latest panes as plain text, for tests
        self.latest_text = ""
        self.latest_age = ""              # the age shown in the latest pane, for tests
        self._sig: tuple | None = None
        self._reload_timer = None

    # ---- layout --------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            yield DataTable(id="groups", cursor_type="row", zebra_stripes=True)
            with Vertical(id="right"):
                yield DataTable(id="events", cursor_type="row", zebra_stripes=True)
                with VerticalScroll(id="detail-box"):
                    yield Static(id="detail")
        yield Static(id="latest")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#latest").border_title = self.TITLES["starting"]
        self.query_one("#detail-box").border_title = "Event"
        self._set_columns()
        self.reload(force=True)
        self.query_one("#groups").focus()
        self.set_interval(self.check_every, self.reload)
        self.set_interval(self.redraw_every, self.redraw)
        self.run_worker(self._follow(), name="follow", exclusive=True)

    def _set_columns(self) -> None:
        groups, events = self.query_one("#groups", DataTable), self.query_one("#events", DataTable)
        groups.clear(columns=True)
        events.clear(columns=True)
        if self.view == "hosts":
            groups.add_columns("host", "events", "newest")
            events.add_columns("type", "subject", "age", "summary")
            groups.border_title, events.border_title = "Hosts", "Events of the selected host"
        else:
            groups.add_columns("event type", "hosts", "newest")
            events.add_columns("host", "subject", "age", "summary")
            groups.border_title, events.border_title = "Event types", "The selected type, from every host"

    # ---- data -----------------------------------------------------------------------------

    def reload(self, force: bool = False) -> None:
        """Read the state files again if any changed."""
        sig = tuimodel.signature(self.state_root)
        if not force and sig == self._sig:
            return
        self._sig = sig
        self.events = tuimodel.load(self.state_root)
        self.by_key = {tuimodel.event_key(e): e for e in self.events}
        if self.feed == "state":
            # nothing reports arrivals, so the latest event is the one in the file changed last
            ev = tuimodel.latest_arrival(self.state_root)
            if ev and ev.get("id") != (self.latest or {}).get("id"):
                self.latest = ev
                self._show_latest()
        elif self.latest is None:
            self.latest = self._initial_latest()
            self._show_latest()
        self.redraw()

    def action_reload(self) -> None:
        self.reload(force=True)

    def _initial_latest(self) -> dict | None:
        if self.latest_path:
            ev = subscribe.read_latest(self.latest_path)
            if ev:
                return ev
        return tuimodel.newest(self.events)

    def redraw(self) -> None:
        """Fill both lists from the loaded events, keeping the selections."""
        now = datetime.now(timezone.utc)
        groups = tuimodel.hosts(self.events) if self.view == "hosts" else tuimodel.types(self.events)
        try:
            table = self.query_one("#groups", DataTable)
        except NoMatches:                      # a timer fired while the screen is going away
            return
        rows = []
        for g in groups:
            name = Text()
            name.append("● ", style=DOT.get(g.style, "grey50"))
            name.append(g.key)
            rows.append((g.key, [name, str(g.count), eventview.age(g.newest, now).replace(" ago", "")]))
        self._fill(table, rows, self.group_key)
        keys = [k for k, _ in rows]
        if self.group_key not in keys:
            self.group_key = keys[0] if keys else None
        self._fill_events(now)
        self._show_latest()                    # its age moves on too
        if not self.events:
            self._set_detail(Text(f"No state files in {self.state_root}.\nIs 'messnet serve' running, and has anything "
                                  "been emitted?", style="dim"), "")

    def _fill_events(self, now: datetime) -> None:
        try:
            table = self.query_one("#events", DataTable)
        except NoMatches:
            return
        rows = []
        for r in tuimodel.rows(self.events, self.view, self.group_key or "", now):
            cells = list(r.cells)
            cells[3] = Text(cells[3], style=STYLES.get(r.style, ""))
            rows.append((r.key, cells))
        self._fill(table, rows, self.event_key)
        keys = [k for k, _ in rows]
        if self.event_key not in keys:
            self.event_key = keys[0] if keys else None
        self._show_detail()

    @staticmethod
    def _fill(table: DataTable, rows: list[tuple[str, list]], keep: str | None) -> None:
        table.clear()
        for key, cells in rows:
            table.add_row(*cells, key=key)
        keys = [k for k, _ in rows]
        if keys:
            table.move_cursor(row=keys.index(keep) if keep in keys else 0, animate=False)

    # ---- the detail and latest panes -------------------------------------------------------------

    def _set_detail(self, renderable, plain: str) -> None:
        self.detail_text = plain
        with contextlib.suppress(NoMatches):
            self.query_one("#detail", Static).update(renderable)

    def _show_detail(self) -> None:
        ev = self.by_key.get(self.event_key or "")
        if ev is None:
            if self.events:
                self._set_detail(Text("Nothing selected.", style="dim"), "")
            return
        sections = eventview.describe(ev)
        self._set_detail(render_sections(sections), eventview.to_text(sections))

    def _show_latest(self) -> None:
        if self.latest is None:
            self.latest_text = self.latest_age = ""
            shown = Text("Waiting for events…", style="dim")
        else:
            now = datetime.now(timezone.utc)
            shown = render_latest(self.latest, now)
            self.latest_age = eventview.age(eventview.parse_time(self.latest.get("time")), now)
            s = eventview.summary(self.latest)
            self.latest_text = f"{self.latest.get('origin')} {self.latest.get('type')} {s.text}"
        with contextlib.suppress(NoMatches):
            self.query_one("#latest", Static).update(shown)

    # ---- following the network ----------------------------------------------------------------------

    def arrived(self, ev: dict) -> None:
        """A new event came in: show it, and refresh the lists shortly."""
        self.latest = ev
        self._show_latest()
        if self._reload_timer is None:
            self._reload_timer = self.set_timer(0.6, self._reload_soon)

    def _reload_soon(self) -> None:
        self._reload_timer = None
        self.reload()

    TITLES = {"publisher": "Latest event · live",
              "file": "Latest event · from the latest.json file",
              "state": "Latest event · newest state file (no live feed: is 'messnet serve' running, and recent?)",
              "starting": "Latest event"}

    def _status(self, text: str, feed: str) -> None:
        """Say where live information is coming from (TEXT, for the header) and use FEED for 'latest'."""
        self.live, self.feed = text, feed
        self.sub_title = f"live: {text}"
        with contextlib.suppress(NoMatches):
            self.query_one("#latest").border_title = self.TITLES[feed]
        if feed == "state":
            self.reload(force=True)

    async def _follow(self) -> None:
        """Take live events from the concentrator; when it is not there, watch the latest file."""
        seen = (self.latest or {}).get("id")
        while True:
            if self.target:
                try:
                    async for ev in subscribe.subscribe(
                            self.target, replay=1, on_hello=lambda hello: self._status(f"connected to {self.target}", "publisher")):
                        seen = ev.get("id")
                        self.arrived(ev)
                except subscribe.SubscribeError:
                    pass
                except (OSError, ValueError):
                    pass
            if self.latest_path and self.latest_path.exists():
                self._status(f"polling {self.latest_path.name} (no node publishing)", "file")
                for _ in range(5):                  # then try the concentrator again
                    ev = subscribe.read_latest(self.latest_path)
                    if ev and ev.get("id") != seen:
                        seen = ev.get("id")
                        self.arrived(ev)
                    await asyncio.sleep(1.0)
            else:
                self._status("none (following the state files only)", "state")
                await asyncio.sleep(2.0)

    # ---- interaction ----------------------------------------------------------------------------------

    def action_view(self, view: str) -> None:
        if view == self.view:
            return
        self.view, self.group_key, self.event_key = view, None, None
        self._set_columns()
        self.redraw()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value if event.row_key else None
        if key is None:
            return
        if event.data_table.id == "groups":
            if key != self.group_key:
                self.group_key, self.event_key = key, None
                self._fill_events(datetime.now(timezone.utc))
        elif event.data_table.id == "events" and key != self.event_key:
            self.event_key = key
            self._show_detail()

    async def on_unmount(self) -> None:
        with contextlib.suppress(Exception):
            self.workers.cancel_all()
