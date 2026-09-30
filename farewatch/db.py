"""SQLite storage. Append-only observations; everything else is derived at report time."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

from .sources.base import Offer

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
  source TEXT NOT NULL, kind TEXT NOT NULL,           -- discovery | verify | feed
  requests INTEGER DEFAULT 0, offers INTEGER DEFAULT 0,
  status TEXT DEFAULT 'running', error TEXT
);
CREATE TABLE IF NOT EXISTS offers(
  id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES runs(id),
  source TEXT NOT NULL, fetched_at TEXT NOT NULL, obs_day TEXT NOT NULL,   -- obs_day = local date
  route TEXT NOT NULL, origin TEXT, dest TEXT,
  depart_date TEXT NOT NULL, return_date TEXT NOT NULL, trip_days INTEGER,
  price_pp REAL, currency TEXT, price_pp_czk REAL NOT NULL,                -- per person; _czk = HOME currency (`[general] currency`), legacy column name
  stops INTEGER, airline TEXT, duration_min INTEGER, cached_at TEXT, extra TEXT
);
CREATE INDEX IF NOT EXISTS offers_cell ON offers(source, route, depart_date, return_date, obs_day);
CREATE INDEX IF NOT EXISTS offers_day ON offers(obs_day);
CREATE TABLE IF NOT EXISTS deals(
  guid TEXT PRIMARY KEY, feed TEXT, title TEXT, url TEXT, published TEXT, first_seen TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS notified(          -- what the phone was already told (notify.py)
  key TEXT PRIMARY KEY, price_pp REAL, urgent INTEGER DEFAULT 0, at TEXT
);
CREATE TABLE IF NOT EXISTS http_cache(
  url TEXT PRIMARY KEY, etag TEXT, last_modified TEXT, fetched_at TEXT
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def iso(now: dt.datetime) -> str:
    return now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def start_run(conn, source: str, kind: str, now: dt.datetime) -> int:
    cur = conn.execute("INSERT INTO runs(started_at, source, kind) VALUES (?,?,?)", (iso(now), source, kind))
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id: int, now: dt.datetime, *, status: str, requests: int, offers: int, error: str | None = None):
    conn.execute(
        "UPDATE runs SET finished_at=?, status=?, requests=?, offers=?, error=? WHERE id=?",
        (iso(now), status, requests, offers, (error or "")[:500] or None, run_id),
    )
    conn.commit()


def insert_offers(conn, run_id: int, offers: list[Offer], now: dt.datetime, obs_day: dt.date) -> int:
    rows = [
        (
            run_id, o.source, iso(now), obs_day.isoformat(), o.route, o.origin, o.dest,
            o.depart_date, o.return_date, o.trip_days, o.price_pp, o.currency, o.price_pp_czk,
            o.stops, o.airline, o.duration_min, o.cached_at, json.dumps(o.extra) if o.extra else None,
        )
        for o in offers
    ]
    conn.executemany(
        """INSERT INTO offers(run_id, source, fetched_at, obs_day, route, origin, dest, depart_date, return_date,
             trip_days, price_pp, currency, price_pp_czk, stops, airline, duration_min, cached_at, extra)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()
    return len(rows)


def requests_this_month(conn, source: str, now: dt.datetime) -> int:
    start = now.astimezone(dt.timezone.utc).strftime("%Y-%m-01T00:00:00Z")
    row = conn.execute(
        "SELECT COALESCE(SUM(requests),0) n FROM runs WHERE source=? AND started_at>=?", (source, start)
    ).fetchone()
    return int(row["n"])


def requests_since(conn, source: str, since: dt.datetime) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(requests),0) n FROM runs WHERE source=? AND started_at>=?", (source, iso(since))
    ).fetchone()
    return int(row["n"])


def notified_get(conn, key: str):
    return conn.execute("SELECT * FROM notified WHERE key=?", (key,)).fetchone()


def notified_set(conn, key: str, price_pp: float | None, urgent: bool, at: str) -> None:
    conn.execute("INSERT OR REPLACE INTO notified(key, price_pp, urgent, at) VALUES (?,?,?,?)", (key, price_pp, int(urgent), at))
