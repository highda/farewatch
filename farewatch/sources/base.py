from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from ..config import Config
    from ..http import Http


class SourceError(Exception):
    """A source failed in a way worth recording (missing key, all requests failed, ...)."""


@dataclass
class Offer:
    """Cheapest known round trip for one cell, per person, from one source."""
    source: str
    route: str
    origin: str
    dest: str
    depart_date: str
    return_date: str
    price_pp: float
    currency: str = "CZK"
    stops: int | None = None
    airline: str | None = None
    duration_min: int | None = None
    cached_at: str | None = None           # when the source last saw this price (cached sources)
    extra: dict | None = None
    price_pp_czk: float = 0.0              # filled in by the pipeline
    trip_days: int = 0


@dataclass
class Context:
    cfg: "Config"
    conn: sqlite3.Connection
    http: "Http"
    now: dt.datetime
    log: Callable[[str], None]
    errors: int = 0                        # per-request failures inside the current source
    run_id: int | None = None              # the `runs` row of the source currently running
