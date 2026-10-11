"""Human-readable views of events: a one-line summary and a sectioned description.

Nothing here draws anything.  ``summary`` and ``describe`` return plain structures (``Line``,
``Section``) that carry a *style name* (``ok``, ``warn``, ``fail``, ``dim``, ``key``) and no
colours, so the same text feeds the terminal UI, a plain-text rendering (``to_text``) and tests.

Each event type may have its own formatter, registered with ``@formatter("type")``; anything
else gets the generic one, which lays out the envelope and the ``data`` fields.  A type's
formatter knows its schema (see ``messnet/schemas``) and shows what matters first: the
outcome, then where, how much and how long.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

OK, WARN, FAIL, DIM, KEY = "ok", "warn", "fail", "dim", "key"
_RESULT_STYLE = {"ok": OK, "up": OK, "started": OK, "primed": OK, "partial": WARN, "unknown": WARN,
                 "fail": FAIL, "down": FAIL}


@dataclass
class Line:
    label: str
    value: str
    style: str = ""


@dataclass
class Section:
    """A titled group: LINES (label, value), or a table of ROWS under COLUMNS (ROW_STYLES parallel)."""
    title: str
    lines: list[Line] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    row_styles: list[str] = field(default_factory=list)


@dataclass
class Summary:
    text: str
    style: str = ""


# ---- small helpers ------------------------------------------------------------------------

def parse_time(text) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return when if when.tzinfo else when.astimezone()


def human_bytes(n) -> str:
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def human_duration(seconds) -> str:
    """90 -> '1m30s', 0.7 -> '0.7s', 90000 -> '1d1h'."""
    s = float(seconds)
    if s < 10:
        return f"{s:.1f}s"
    s = int(round(s))
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes, s = divmod(s, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{s:02d}s" if minutes else f"{s}s"


def age(when: datetime | None, now: datetime | None = None) -> str:
    """'12s ago', '5m ago', '3h ago', '2d ago'; '-' when WHEN is unknown."""
    if when is None:
        return "-"
    seconds = ((now or datetime.now(timezone.utc)) - when).total_seconds()
    if seconds < -1:
        return "in the future"
    seconds = max(0, int(seconds))
    for limit, unit, size in ((60, "s", 1), (3600, "m", 60), (86400, "h", 3600)):
        if seconds < limit:
            return f"{seconds // size}{unit} ago"
    return f"{seconds // 86400}d ago"


def local_time(when: datetime | None) -> str:
    return when.astimezone().strftime("%Y-%m-%d %H:%M:%S") if when else "-"


def short(text, n: int = 8) -> str:
    return str(text)[:n]


def clip(text, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def result_style(result) -> str:
    return _RESULT_STYLE.get(str(result), "")


def _tail(url: str, n: int = 40) -> str:
    """A repository name shortened from the left, keeping the end that tells repositories apart."""
    return url if len(url) <= n else "…" + url[-(n - 1):]


# ---- registry -----------------------------------------------------------------------------

Formatter = tuple[Callable[[dict], Summary], Callable[[dict, datetime], list[Section]]]
_FORMATTERS: dict[str, Formatter] = {}


def formatter(type_: str):
    """Register a (summary, sections) pair of functions for events of TYPE_."""
    def register(pair: Formatter) -> Formatter:
        _FORMATTERS[type_] = pair
        return pair
    return register


def has_formatter(type_: str) -> bool:
    return type_ in _FORMATTERS


def summary(ev: dict) -> Summary:
    """One line saying what the event is about, styled by its outcome."""
    pair = _FORMATTERS.get(ev.get("type", ""))
    if pair:
        try:
            return pair[0](ev)
        except (KeyError, TypeError, ValueError, AttributeError):
            pass                                   # a malformed event gets the generic treatment
    return _generic_summary(ev)


def describe(ev: dict, now: datetime | None = None) -> list[Section]:
    """The event laid out for reading: the type's own sections, then the envelope."""
    now = now or datetime.now(timezone.utc)
    pair = _FORMATTERS.get(ev.get("type", ""))
    body = None
    if pair:
        try:
            body = pair[1](ev, now)
        except (KeyError, TypeError, ValueError, AttributeError):
            body = None
    if body is None:
        body = _generic_sections(ev)
    return [*body, _envelope(ev, now)]


def to_text(sections: list[Section], width: int = 78) -> str:
    """A plain-text rendering of SECTIONS."""
    out: list[str] = []
    for sec in sections:
        out.append(sec.title.upper())
        pad = max((len(line.label) for line in sec.lines), default=0)
        out += [f"  {line.label:<{pad}}  {line.value}" for line in sec.lines]
        if sec.columns:
            widths = [max(len(str(r[i])) for r in [sec.columns, *sec.rows]) for i in range(len(sec.columns))]
            for row in [sec.columns, *sec.rows]:
                out.append("  " + "  ".join(f"{str(c):<{w}}" for c, w in zip(row, widths, strict=False)).rstrip())
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# ---- the envelope, and events without a formatter --------------------------------------------

def _envelope(ev: dict, now: datetime) -> Section:
    when = parse_time(ev.get("time"))
    lines = [Line("time", f"{local_time(when)}  ({age(when, now)})"),
             Line("host", str(ev.get("origin", "?"))),
             Line("type", str(ev.get("type", "?"))),
             Line("subject", str(ev.get("subject") or "—"), DIM if not ev.get("subject") else ""),
             Line("source", str(ev.get("source", "?"))),
             Line("scope", str(ev.get("scope", "all"))),
             Line("seq", str(ev.get("seq", "?"))),
             Line("id", str(ev.get("id", "?")), DIM)]
    return Section("Event", lines)


def _scalar(value) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _flat(value, prefix: str = "") -> list[tuple[str, str]]:
    """Nested data as 'a.b' / 'a[0]' paths with scalar values."""
    if isinstance(value, dict):
        out: list[tuple[str, str]] = []
        for k, v in value.items():
            out += _flat(v, f"{prefix}.{k}" if prefix else str(k))
        return out or [(prefix, "{}")]
    if isinstance(value, list):
        if all(not isinstance(v, (dict, list)) for v in value):
            return [(prefix, ", ".join(_scalar(v) for v in value) or "[]")]
        out = []
        for i, v in enumerate(value):
            out += _flat(v, f"{prefix}[{i}]")
        return out
    return [(prefix, _scalar(value))]


def _generic_sections(ev: dict) -> list[Section]:
    if "data" not in ev:
        return [Section("Data", [Line("", "(no data)", DIM)])]
    data = ev["data"]
    if isinstance(data, dict):
        lines = [Line(k or "data", v, result_style(v) if k in ("result", "state", "status") else "")
                 for k, v in _flat(data)]
    else:
        lines = [Line("data", v) for _, v in _flat(data)]
    return [Section("Data", lines)]


def _generic_summary(ev: dict) -> Summary:
    data = ev.get("data")
    if isinstance(data, dict):
        style = ""
        parts = []
        for k, v in data.items():
            if isinstance(v, (dict, list)):
                continue
            parts.append(f"{k}={_scalar(v)}")
            if k in ("result", "state", "status"):
                style = result_style(v)
        return Summary(clip(" ".join(parts), 200) or "—", style)
    return Summary(clip(_scalar(data), 200) if data is not None else "—")


# ---- restic ----------------------------------------------------------------------------------

def _repo_where(d: dict) -> str:
    return f"{_tail(str(d.get('repository', '?')))} ({d.get('location', '?')})"


def _commit_summary(ev: dict) -> Summary:
    d = ev["data"]
    result = d["result"]
    style = result_style(result)
    if result == "fail":
        return Summary(f"fail: {clip(d.get('error', 'unknown error'), 120)}", style)
    if d.get("method") == "copy":
        n = d.get("snapshots_copied", 0)
        what = f"copied {n} snapshot{'s' if n != 1 else ''} to {_tail(str(d['repository']), 30)}"
        if d.get("total_bytes_processed"):
            what += f", {human_bytes(d['total_bytes_processed'])}"
    else:
        what = f"{d.get('total_files_processed', '?')} files, {human_bytes(d.get('total_bytes_processed', 0))}"
        if "data_added" in d:
            what += f" (+{human_bytes(d['data_added'])})"
        if d.get("snapshot_id"):
            what += f" → {short(d['snapshot_id'])}"
    when = f" in {human_duration(d['duration'])}" if "duration" in d else ""
    flag = f" — {d.get('error_count', '?')} unreadable" if result == "partial" else ""
    return Summary(f"{result}: {what}{when}{flag}", style)


def _commit_sections(ev: dict, now: datetime) -> list[Section]:
    d = ev["data"]
    result = d["result"]
    out = [Section("Outcome", [Line("result", result, result_style(result))])]
    if d.get("error"):
        out[0].lines.append(Line("error", d["error"], FAIL if result == "fail" else WARN))
    if "error_count" in d:
        out[0].lines.append(Line("unreadable", f"{d['error_count']} item(s)", WARN))
    if "exit_code" in d and d["exit_code"]:
        out[0].lines.append(Line("exit code", str(d["exit_code"]), DIM))
    where = Section("Where", [Line("method", {"backup": "backed up a file system",
                                              "copy": "copied snapshots from another repository"}.get(
                                                  d.get("method"), str(d.get("method")))),
                              Line("repository", d.get("repository", "?")),
                              Line("location", d.get("location", "?"))])
    if d.get("source_repository"):
        where.lines.append(Line("from", d["source_repository"]))
    if d.get("paths"):
        where.lines.append(Line("paths", ", ".join(d["paths"])))
    if d.get("snapshot_id"):
        where.lines.append(Line("snapshot", d["snapshot_id"]))
    out.append(where)
    if "files_new" in d:
        out.append(Section("Files", [
            Line("files", f"{d['files_new']} new, {d.get('files_changed', 0)} changed, {d.get('files_unmodified', 0)} unchanged"),
            Line("directories", f"{d.get('dirs_new', 0)} new, {d.get('dirs_changed', 0)} changed, {d.get('dirs_unmodified', 0)} unchanged")]))
    data = Section("Data")
    for key, label in (("total_files_processed", "files in snapshot"), ):
        if key in d:
            data.lines.append(Line(label, str(d[key])))
    if "total_bytes_processed" in d:
        data.lines.append(Line("size", human_bytes(d["total_bytes_processed"])))
    if "data_added" in d:
        packed = f" ({human_bytes(d['data_added_packed'])} stored)" if "data_added_packed" in d else ""
        data.lines.append(Line("added", f"{human_bytes(d['data_added'])}{packed}"))
    if "data_blobs" in d:
        data.lines.append(Line("blobs", f"{d['data_blobs']} data, {d.get('tree_blobs', 0)} tree", DIM))
    if data.lines:
        out.append(data)
    timing = Section("Timing")
    if "duration" in d:
        timing.lines.append(Line("took", human_duration(d["duration"])))
    for key in ("started", "ended"):
        if key in d:
            timing.lines.append(Line(key, local_time(parse_time(d[key]))))
    if d.get("restic_version"):
        timing.lines.append(Line("restic", d["restic_version"], DIM))
    if timing.lines:
        out.append(timing)
    if d.get("snapshots"):
        out.append(Section(f"Snapshots copied ({d.get('snapshots_copied', len(d['snapshots']))})",
                           columns=["new", "from", "packs"],
                           rows=[[short(s.get("id", "?")), short(s.get("source_id", "")), str(s.get("packs", ""))]
                                 for s in d["snapshots"]]))
    return out


formatter("restic.commit.v1")((_commit_summary, _commit_sections))


def _password_lines(prime: dict, now: datetime) -> list[Line]:
    lines = []
    for name, p in prime.items():
        if not p.get("primed"):
            lines.append(Line(name, "NOT PRIMED" + (f" ({p['error']})" if p.get("error") else ""), FAIL))
            continue
        left = p.get("ttl_remaining")
        text = "primed"
        style = OK
        if left is not None:
            text += f", expires in {human_duration(left)}"
            style = WARN if left < 86400 else OK
        if p.get("expires"):
            text += f"  ({local_time(parse_time(p['expires']))})"
        lines.append(Line(name, text, style))
    return lines or [Line("", "(no password cache reported)", DIM)]


def _outcome_rows(group: dict, now: datetime) -> tuple[list[list[str]], list[str]]:
    rows, styles = [], []
    for stream, o in sorted(group.items()):
        what = ""
        if o.get("method") == "copy":
            what = f"copied {o.get('snapshots_copied', 0)} → {_tail(str(o.get('repository', '')), 28)}"
        elif o.get("snapshot_id"):
            what = f"{o.get('total_files_processed', '?')} files, {human_bytes(o.get('total_bytes_processed', 0))} → {short(o['snapshot_id'])}"
        elif "snapshots_removed" in o:
            what = f"removed {o['snapshots_removed']}"
        if o.get("error"):
            what = clip(o["error"], 50)
        rows.append([stream, o.get("result", "?"), age(parse_time(o.get("time")), now), what])
        styles.append(result_style(o.get("result")))
    return rows, styles


def _status_summary(ev: dict) -> Summary:
    d = ev["data"]
    parts = [f"{k} {v.get('result', '?')}" for k, v in sorted(d.get("commits", {}).items())]
    pw = d.get("prime", {})
    parts.append("password " + (", ".join(f"{k} {'ok' if v.get('primed') else 'NOT PRIMED'}" for k, v in pw.items()) or "?"))
    style = result_style(d["result"])
    if not all(v.get("primed") for v in pw.values()):
        style = style if style == FAIL else WARN
    return Summary(f"{d['result']}: " + "; ".join(parts), style)


def _status_sections(ev: dict, now: datetime) -> list[Section]:
    d = ev["data"]
    out = [Section("Backups", [Line("overall", d["result"], result_style(d["result"]))])]
    if d.get("commits"):
        rows, styles = _outcome_rows(d["commits"], now)
        out.append(Section("Latest commit per stream", columns=["stream", "result", "when", "what"], rows=rows, row_styles=styles))
    if d.get("forget"):
        rows, styles = _outcome_rows(d["forget"], now)
        out.append(Section("Latest retention per stream", columns=["stream", "result", "when", "what"], rows=rows, row_styles=styles))
    out.append(Section("Password cache", _password_lines(d.get("prime", {}), now)))
    return out


formatter("restic.status.v1")((_status_summary, _status_sections))


def _prime_summary(ev: dict) -> Summary:
    d = ev["data"]
    if d["result"] != "ok":
        return Summary(f"fail: {clip(d.get('error', ''), 120)}", FAIL)
    return Summary(f"ok: {ev.get('subject', '?')} password cached for {human_duration(d['ttl'])}, "
                   f"expires {local_time(parse_time(d['expires']))}", OK)


def _prime_sections(ev: dict, now: datetime) -> list[Section]:
    d = ev["data"]
    lines = [Line("which", str(ev.get("subject", "?"))), Line("result", d["result"], result_style(d["result"]))]
    if d["result"] == "ok":
        expires = parse_time(d["expires"])
        lines += [Line("valid for", human_duration(d["ttl"])),
                  Line("expires", f"{local_time(expires)}  (in {human_duration(max(0, (expires - now).total_seconds()))})"
                       if expires and expires > now else f"{local_time(expires)}  (expired)",
                       OK if expires and expires > now else FAIL)]
    else:
        lines.append(Line("error", d.get("error", "?"), FAIL))
    return [Section("Password priming", lines)]


formatter("restic.prime.v1")((_prime_summary, _prime_sections))


def _forget_summary(ev: dict) -> Summary:
    d = ev["data"]
    if d["result"] != "ok":
        return Summary(f"fail: {clip(d.get('error', ''), 120)}", FAIL)
    return Summary(f"ok: kept {d.get('snapshots_kept', '?')}, removed {d.get('snapshots_removed', '?')} snapshots"
                   + (f" in {human_duration(d['duration'])}" if "duration" in d else ""), OK)


def _forget_sections(ev: dict, now: datetime) -> list[Section]:
    d = ev["data"]
    lines = [Line("result", d["result"], result_style(d["result"]))]
    if d.get("error"):
        lines.append(Line("error", d["error"], FAIL))
    if "snapshots_kept" in d:
        lines += [Line("kept", str(d["snapshots_kept"])), Line("removed", str(d.get("snapshots_removed", 0)))]
    if d.get("keep_policy"):
        lines.append(Line("policy", ", ".join(f"{v} {k}" for k, v in d["keep_policy"].items())))
    if "prune" in d:
        lines.append(Line("pruned", "yes" if d["prune"] else "no"))
    where = Section("Where", [Line("repository", d.get("repository", "?")), Line("location", d.get("location", "?"))])
    timing = Section("Timing", [Line("took", human_duration(d["duration"]))] if "duration" in d else [])
    return [Section("Retention", lines), where, *([timing] if timing.lines else [])]


formatter("restic.forget.v1")((_forget_summary, _forget_sections))


# ---- messnet itself --------------------------------------------------------------------------

def _link_summary(ev: dict) -> Summary:
    d = ev["data"]
    text = f"{d['state']} with {ev.get('subject', '?')}"
    if d.get("link"):
        text += f" via {d['link']}"
    if d["state"] == "down" and "sent" in d:
        text += f" (sent {d['sent']}, received {d.get('received', 0)})"
    if d.get("error"):
        text += f": {clip(d['error'], 80)}"
    return Summary(text, result_style(d["state"]))


def _link_sections(ev: dict, now: datetime) -> list[Section]:
    d = ev["data"]
    lines = [Line("peer", str(ev.get("subject", "?"))), Line("state", d["state"], result_style(d["state"])),
             Line("link", d.get("link") or "—"), Line("scope", d.get("scope", "?"))]
    if "sent" in d:
        lines += [Line("events sent", str(d["sent"])), Line("events received", str(d.get("received", 0)))]
    if d.get("error"):
        lines.append(Line("error", d["error"], FAIL))
    return [Section("Link", lines)]


formatter("messnet.link.v1")((_link_summary, _link_sections))
