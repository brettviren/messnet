"""Narrowing and ordering the rows of a table: the logic behind the web UI's list controls.

A table is a list of ``Row`` over ``Column``.  A ``Query`` says which rows to keep and in what
order; it is read from request arguments (``parse``) and applied with ``apply``:

* a pattern for *any* column and one per column, each interpreted in one *mode*: ``fuzzy`` (the
  characters appear in order; several words must all do so), ``glob`` (``*``, ``?``, ``[..]``, the
  whole cell) or ``regex`` (found anywhere in the cell).  Matching ignores case.
* a column to order by, ascending or descending.  With none given, a time column orders
  newest-first.  Cells without a value (no time, say) come last in either direction.

Pure functions, no web framework.
"""

import fnmatch
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

MODES = ("fuzzy", "glob", "regex")
TEXT, NUMBER, TIME = "text", "number", "time"


@dataclass(frozen=True)
class Column:
    key: str
    title: str
    kind: str = TEXT                  # text | number | time

    @property
    def default_descending(self) -> bool:
        return self.kind == TIME      # newest first; text and numbers start ascending


@dataclass
class Cell:
    text: str                         # what is shown
    sort: datetime | float | str | None = None      # what orders it; defaults to the text
    search: str | None = None         # what patterns see; defaults to the text
    style: str = ""
    title: str | None = None          # a tooltip

    def sort_value(self, kind: str):
        if self.sort is not None:
            return self.sort
        return self.text.lower() if kind == TEXT else None

    def haystack(self) -> str:
        return self.text if self.search is None else self.search


@dataclass
class Row:
    key: str                          # identifies the row (and what a selection refers to)
    cells: dict[str, Cell]            # by column key
    style: str = ""
    href: str | None = None           # where selecting the row leads


class PatternError(ValueError):
    pass


@dataclass
class Query:
    any: str = ""                                       # a pattern for every column
    columns: dict[str, str] = field(default_factory=dict)       # column key -> pattern
    mode: str = "fuzzy"
    sort: str | None = None
    descending: bool | None = None                      # None: the column's own default

    @property
    def active(self) -> bool:
        return bool(self.any or any(self.columns.values()))


@dataclass
class Result:
    rows: list[Row]
    total: int
    sort: str | None                                    # the column in force (None: given order)
    descending: bool
    error: str | None = None


# ---- patterns -------------------------------------------------------------------------------

def _fuzzy(pattern: str) -> Callable[[str], bool]:
    terms = pattern.lower().split()

    def has(term: str, text: str) -> bool:
        chars = iter(text)
        return all(c in chars for c in term)         # each char found after the previous one

    return lambda text: all(has(t, text.lower()) for t in terms)


def _glob(pattern: str) -> Callable[[str], bool]:
    pattern = pattern.lower()
    return lambda text: fnmatch.fnmatchcase(text.lower(), pattern)


def _regex(pattern: str) -> Callable[[str], bool]:
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as err:
        raise PatternError(f"bad regular expression {pattern!r}: {err}") from err
    return lambda text: rx.search(text) is not None


def matcher(mode: str, pattern: str) -> Callable[[str], bool]:
    """A function telling whether a cell's text matches PATTERN in MODE (PatternError if it is invalid)."""
    try:
        return {"fuzzy": _fuzzy, "glob": _glob, "regex": _regex}[mode](pattern)
    except KeyError:
        raise PatternError(f"unknown search mode {mode!r}") from None


# ---- reading a query from request arguments ---------------------------------------------------------

def param(prefix: str, name: str, column: str | None = None) -> str:
    """The argument name for NAME (q, mode, sort, dir) or a COLUMN's pattern, in the table PREFIX."""
    return f"{prefix}c_{column}" if column else f"{prefix}{name}"


def parse(args: Mapping[str, str], prefix: str, columns: list[Column]) -> Query:
    """The Query that ARGS ask of the table whose arguments start with PREFIX.  Bad values are
    ignored, so a hand-edited URL never breaks the page."""
    mode = args.get(param(prefix, "mode"), "fuzzy")
    keys = {c.key for c in columns}
    sort = args.get(param(prefix, "sort"))
    direction = args.get(param(prefix, "dir"))
    return Query(
        any=args.get(param(prefix, "q"), "").strip(),
        columns={c.key: v for c in columns if (v := args.get(param(prefix, "", c.key), "").strip())},
        mode=mode if mode in MODES else "fuzzy",
        sort=sort if sort in keys else None,
        descending={"asc": False, "desc": True}.get(direction))


def toggled(query: Query, columns: list[Column], key: str) -> tuple[str, str]:
    """The (sort, dir) arguments that a click on column KEY's heading asks for: the column's own
    order first, then the opposite one each time."""
    result = order(query, columns)
    col = next(c for c in columns if c.key == key)
    if result[0] == key:
        return key, "asc" if result[1] else "desc"
    return key, "desc" if col.default_descending else "asc"


def order(query: Query, columns: list[Column]) -> tuple[str | None, bool]:
    """(column, descending) in force: the requested one, else the first time column newest-first."""
    if query.sort:
        col = next(c for c in columns if c.key == query.sort)
        return col.key, col.default_descending if query.descending is None else query.descending
    for col in columns:
        if col.kind == TIME:
            return col.key, True
    return None, False


# ---- applying it -----------------------------------------------------------------------------------

def apply(rows: list[Row], columns: list[Column], query: Query) -> Result:
    """The rows that match QUERY, in its order.  An invalid pattern keeps every row and says why."""
    kinds = {c.key: c.kind for c in columns}
    sort, descending = order(query, columns)
    error = None
    kept = rows
    try:
        per_column = {k: matcher(query.mode, v) for k, v in query.columns.items() if v and k in kinds}
        anywhere = matcher(query.mode, query.any) if query.any else None
    except PatternError as err:
        per_column, anywhere, error = {}, None, str(err)
    if per_column or anywhere:
        def keep(row: Row) -> bool:
            if any(not m(row.cells[k].haystack()) for k, m in per_column.items() if k in row.cells):
                return False
            return anywhere is None or any(anywhere(c.haystack()) for c in row.cells.values())
        kept = [r for r in rows if keep(r)]
    if sort:
        kind = kinds[sort]
        have = [(r, v) for r in kept if (v := r.cells[sort].sort_value(kind)) is not None]
        lack = [r for r in kept if r.cells[sort].sort_value(kind) is None]
        have.sort(key=lambda rv: rv[1], reverse=descending)
        kept = [r for r, _ in have] + lack
    return Result(kept, len(rows), sort, descending, error)
