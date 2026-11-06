"""``messnet web``: the network in a browser.

This module is the framework the pages hang on: a registry of pages, the shared layout (a
navigation bar listing every registered page, and the latest-event panel), and helpers for lists
that can be narrowed and ordered from the URL.  The pages themselves are in ``webpages``; to add one,
write a view function and decorate it::

    @page("/backups", "Backups")
    def backups():
        ...
        return render_template("backups.html", ...)

and import the module from ``create_app``.  Content comes from the state files and the node's
publisher, as for the terminal UI (``tuimodel``, ``feed``); formatting from ``eventview``.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from flask import Flask, current_app, redirect, request

from messnet import eventview, feed, tablequery, tuimodel

REFRESH_SECONDS = 3


@dataclass(frozen=True)
class Page:
    path: str
    title: str                     # its entry in the navigation bar
    view: Callable
    latest: bool = True            # show the latest-event panel


PAGES: list[Page] = []


def page(path: str, title: str, latest: bool = True):
    """Register the decorated view function as the page at PATH, listed as TITLE in the navigation."""
    def register(view: Callable) -> Callable:
        PAGES[:] = [p for p in PAGES if p.path != path]
        PAGES.append(Page(path, title, view, latest))
        return view
    return register


class Context:
    """What views share: where the state is and the latest-event feed."""

    def __init__(self, state_root: Path, node: str = "", latest: feed.LatestFeed | None = None,
                 refresh: float = REFRESH_SECONDS):
        self.state_root, self.node, self.refresh = Path(state_root), node, refresh
        self.feed = latest or feed.LatestFeed(self.state_root)
        self._lock = threading.Lock()
        self._cache: tuple[tuple, list[dict]] | None = None

    def events(self) -> list[dict]:
        """Every event in the state files, newest first; read again only when a state file changed."""
        sig = tuimodel.signature(self.state_root)
        with self._lock:
            if self._cache is None or self._cache[0] != sig:
                self._cache = (sig, tuimodel.load(self.state_root))
            return self._cache[1]


def context() -> Context:
    return current_app.extensions["messnet"]


# ---- URLs ------------------------------------------------------------------------------------------

def link(**changes) -> str:
    """This page's URL with the given arguments set (None removes one), all others kept."""
    args = request.args.to_dict()
    for key, value in changes.items():
        if value is None:
            args.pop(key, None)
        else:
            args[key] = value
    return request.path + ("?" + urlencode(args) if args else "")


# ---- tables the user can narrow and order ----------------------------------------------------------------

@dataclass
class Heading:
    column: tablequery.Column
    url: str                       # a click on the title: order by this column
    arrow: str                     # ▲ ▼ or ''
    name: str                      # the form field holding this column's pattern
    value: str


@dataclass
class TableView:
    """A table ready to be drawn by ``_table.html``."""
    prefix: str
    title: str
    headings: list[Heading]
    rows: list[tablequery.Row]
    total: int
    query: tablequery.Query
    selected: str | None
    error: str | None
    hidden: list[tuple[str, str]] = field(default_factory=list)   # the other arguments the form keeps
    any_name: str = ""
    mode_name: str = ""
    modes: tuple = tablequery.MODES
    clear_url: str = ""
    empty: str = "Nothing to show."

    @property
    def counted(self) -> str:
        return f"{len(self.rows)} of {self.total}" if len(self.rows) != self.total else str(self.total)


def table_view(prefix: str, title: str, columns: list[tablequery.Column], rows: list[tablequery.Row],
               selected: str | None = None, empty: str = "Nothing to show.") -> tuple[TableView, tablequery.Result]:
    """Apply the request's narrowing and ordering for the table PREFIX to ROWS.

    The table's arguments are PREFIX plus q, mode, sort, dir and c_<column>; a page with two tables
    gives them different prefixes."""
    query = tablequery.parse(request.args, prefix, columns)
    result = tablequery.apply(rows, columns, query)
    headings = []
    for col in columns:
        sort, direction = tablequery.toggled(query, columns, col.key)
        arrow = ("▼" if result.descending else "▲") if result.sort == col.key else ""
        headings.append(Heading(col, link(**{tablequery.param(prefix, "sort"): sort,
                                             tablequery.param(prefix, "dir"): direction}), arrow,
                                tablequery.param(prefix, "", col.key), query.columns.get(col.key, "")))
    own = {k for k in request.args if k.startswith(prefix)}
    keep_own = {tablequery.param(prefix, "sort"), tablequery.param(prefix, "dir")}
    hidden = [(k, v) for k, v in request.args.items() if k not in own or k in keep_own]
    clear = link(**{k: None for k in own if k not in keep_own})
    view = TableView(prefix, title, headings, result.rows, result.total, query, selected, result.error, hidden,
                     tablequery.param(prefix, "q"), tablequery.param(prefix, "mode"), clear_url=clear, empty=empty)
    return view, result


# ---- the application --------------------------------------------------------------------------------------

def create_app(state_root: Path, node: str = "", latest: feed.LatestFeed | None = None,
               refresh: float = REFRESH_SECONDS) -> Flask:
    """The web UI over the state files in STATE_ROOT.  LATEST supplies the latest-event panel
    (call its ``start()`` for live updates)."""
    from messnet import webpages              # noqa: F401  (registers the pages)

    app = Flask(__name__)
    app.jinja_env.globals["styles"] = {eventview.OK: "ok", eventview.WARN: "warn", eventview.FAIL: "fail",
                                       eventview.DIM: "dim", eventview.KEY: "key", "": ""}     # style name -> CSS class
    app.extensions["messnet"] = ctx = Context(state_root, node, latest, refresh)
    for p in PAGES:
        app.add_url_rule(p.path, endpoint=p.path, view_func=p.view)
    app.add_url_rule("/", endpoint="root", view_func=lambda: redirect(PAGES[0].path))

    @app.context_processor
    def layout() -> dict:
        now = datetime.now(timezone.utc)
        current = next((p for p in PAGES if p.path == request.path), None)
        event = ctx.feed.get(ctx.events()) if current and current.latest else None
        return {"pages": PAGES, "current": current, "link": link, "node": ctx.node, "refresh": ctx.refresh,
                "latest": eventview.latest_view(event, now) if event else None,
                "latest_title": ctx.feed.title, "live": ctx.feed.live}

    return app
