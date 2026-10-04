"""Local SQLite event store: index, dedupe, version vectors, cursors.

The store is never synced.  It holds every event a node knows about and
is the cross-process notification point: readers poll for rowids above
their cursor.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    rowid   INTEGER PRIMARY KEY,
    origin  TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    id      TEXT NOT NULL,
    ts      REAL NOT NULL,
    type    TEXT NOT NULL,
    subject TEXT,
    scope   TEXT NOT NULL,
    json    TEXT NOT NULL,
    UNIQUE (origin, scope, seq)
);
CREATE INDEX IF NOT EXISTS events_type ON events (type);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, offset INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
-- Per stream, every seq <= floor was pruned and counts as present.
CREATE TABLE IF NOT EXISTS floors (origin TEXT, scope TEXT, seq INTEGER NOT NULL,
                                   PRIMARY KEY (origin, scope));
"""


def stream_key(origin: str, scope: str) -> str:
    """Events are sequenced per (origin, scope) stream."""
    return f"{origin}/{scope}"


def event_ts(ev: dict) -> float:
    try:
        return datetime.fromisoformat(ev["time"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return 0.0


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(_SCHEMA)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self):
        """Exclusive-write transaction; serializes seq allocation across processes."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def insert(self, ev: dict) -> bool:
        """Insert EV, return True if new.

        False if (origin, scope, seq) is already known or was pruned.
        """
        origin, scope, seq = ev["origin"], ev.get("scope", "all"), ev["seq"]
        cur = self.db.execute(
            "INSERT OR IGNORE INTO events (origin, seq, id, ts, type, subject, scope, json)"
            " SELECT ?, ?, ?, ?, ?, ?, ?, ? WHERE NOT EXISTS"
            " (SELECT 1 FROM floors WHERE origin = ? AND scope = ? AND seq >= ?)",
            (origin, seq, ev["id"], event_ts(ev), ev["type"], ev.get("subject"), scope,
             json.dumps(ev, separators=(",", ":"), ensure_ascii=False), origin, scope, seq))
        return cur.rowcount == 1

    def floors(self) -> dict[tuple[str, str], int]:
        return {(o, sc): f for o, sc, f in self.db.execute("SELECT origin, scope, seq FROM floors")}

    def floor(self, origin: str, scope: str) -> int:
        row = self.db.execute("SELECT seq FROM floors WHERE origin = ? AND scope = ?",
                              (origin, scope)).fetchone()
        return row[0] if row else 0

    def max_seq(self, origin: str, scope: str) -> int:
        row = self.db.execute("SELECT MAX(seq) FROM events WHERE origin = ? AND scope = ?",
                              (origin, scope)).fetchone()
        return max(row[0] or 0, self.floor(origin, scope))

    def prune(self, before: float) -> int:
        """Delete, per stream, every event up to the last one older than BEFORE.

        Raises each stream's floor so pruned events count as present (no
        re-replication) and are never inserted again.  Returns the number deleted.
        """
        deleted = 0
        rows = self.db.execute("SELECT origin, scope, MAX(seq) FROM events WHERE ts < ?"
                               " GROUP BY origin, scope", (before,)).fetchall()
        for origin, scope, top in rows:
            self.db.execute("INSERT INTO floors (origin, scope, seq) VALUES (?, ?, ?)"
                            " ON CONFLICT (origin, scope) DO UPDATE SET seq = MAX(seq, excluded.seq)",
                            (origin, scope, top))
            deleted += self.db.execute("DELETE FROM events WHERE origin = ? AND scope = ? AND seq <= ?",
                                       (origin, scope, top)).rowcount
        return deleted

    def gaps(self) -> dict[str, list[tuple[int, int]]]:
        """Per stream, missing seq ranges (inclusive) between its floor and its highest seq."""
        out = {}
        floors = self.floors()
        for origin, scope in self.streams():
            expect = floors.get((origin, scope), 0) + 1
            missing = []
            for (seq,) in self.db.execute("SELECT seq FROM events WHERE origin = ? AND scope = ?"
                                          " ORDER BY seq", (origin, scope)):
                if seq > expect:
                    missing.append((expect, seq - 1))
                expect = seq + 1
            if missing:
                out[stream_key(origin, scope)] = missing
        return out

    def max_rowid(self) -> int:
        return self.db.execute("SELECT COALESCE(MAX(rowid), 0) FROM events").fetchone()[0]

    def version_vector(self) -> dict[str, int]:
        """Map stream key "origin/scope" to the highest S such that seqs 1..S are all present.

        Seqs at or below a stream's prune floor count as present.
        """
        rows = self.db.execute("""
            SELECT e.origin, e.scope, MIN(e.seq), m.lo FROM events e
            JOIN (SELECT origin, scope, MIN(seq) AS lo FROM events GROUP BY origin, scope) m
              ON m.origin = e.origin AND m.scope = e.scope
            WHERE NOT EXISTS (SELECT 1 FROM events f WHERE f.origin = e.origin
                              AND f.scope = e.scope AND f.seq = e.seq + 1)
            GROUP BY e.origin, e.scope""").fetchall()
        floors = self.floors()
        vv = {stream_key(o, sc): f for (o, sc), f in floors.items()}
        for o, sc, end, lo in rows:
            f = floors.get((o, sc), 0)
            vv[stream_key(o, sc)] = end if lo <= f + 1 else f
        return vv

    def streams(self) -> list[tuple[str, str]]:
        return self.db.execute("SELECT DISTINCT origin, scope FROM events").fetchall()

    def stream_after(self, origin: str, scope: str, seq: int, upto: int) -> Iterator[dict]:
        """Events of one stream with seq above SEQ and rowid at most UPTO, in seq order."""
        rows = self.db.execute(
            "SELECT json FROM events WHERE origin = ? AND scope = ? AND seq > ? AND rowid <= ?"
            " ORDER BY seq", (origin, scope, seq, upto)).fetchall()
        for (text,) in rows:
            yield json.loads(text)

    def query(self, types: list[str] | None = None, origin: str | None = None,
              since: float | None = None, after: int = 0, limit: int | None = None,
              ) -> Iterator[tuple[int, dict]]:
        """Yield (rowid, event) in rowid order matching the filters.

        TYPES are glob patterns (sqlite GLOB, case sensitive), any may match.
        """
        where, args = ["rowid > ?"], [after]
        if types:
            where.append("(" + " OR ".join("type GLOB ?" for _ in types) + ")")
            args.extend(types)
        if origin:
            where.append("origin = ?")
            args.append(origin)
        if since is not None:
            where.append("ts >= ?")
            args.append(since)
        sql = f"SELECT rowid, json FROM events WHERE {' AND '.join(where)} ORDER BY rowid"
        if limit:
            sql += f" LIMIT {int(limit)}"
        for rowid, text in self.db.execute(sql, args).fetchall():
            yield rowid, json.loads(text)

    def file_offset(self, path: Path) -> int:
        row = self.db.execute("SELECT offset FROM files WHERE path = ?", (str(path),)).fetchone()
        return row[0] if row else 0

    def set_file_offset(self, path: Path, offset: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO files (path, offset) VALUES (?, ?)",
                        (str(path), offset))

    def forget_file(self, path: Path) -> None:
        self.db.execute("DELETE FROM files WHERE path = ?", (str(path),))

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
