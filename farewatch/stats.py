"""Baselines and outliers.

Two independent yardsticks per cell (source, route, depart, return):

* neighbours - vs other trips departing within +-N days *right now*. Works from day one and is immune to
  seasonality (sakura weeks are dear for everybody), because a cell is only compared with its neighbours.
* history    - vs the cell's own past daily minimums. Kicks in after `min_history_days` days of data.

Robust statistics only (median / MAD): one error fare must not drag its own baseline down.
"""
from __future__ import annotations

import datetime as dt
import json
import statistics
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from types import SimpleNamespace

from . import db
from .config import Config
from .plan import window_for

LIVE_SOURCES = {"serpapi"}                 # sources whose price is a live quote, not a cached sighting


def median(xs):
    return statistics.median(xs)


def robust_sigma(xs: list[float]) -> float:
    m = median(xs)
    mad = median([abs(x - m) for x in xs])
    return max(1.4826 * mad, 0.03 * m)          # floor: don't let a very tight sample make everything an outlier


def percentile(xs: list[float], p: float) -> float:
    s = sorted(xs)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


@dataclass
class Cell:
    source: str
    route: str
    depart: str
    ret: str
    days: int
    window: str
    price: float                      # per person, home currency, incl. route penalty
    raw_price: float
    stops: int | None
    airline: str | None
    duration_min: int | None
    obs_at: str
    extra: dict | None
    priority: int = 3                 # from the window: 1 = golden window
    base: float | None = None         # neighbour baseline (median)
    base_n: int = 0
    z: float | None = None
    pct: float | None = None          # % below baseline (positive = cheaper)
    h_median: float | None = None
    h_n: int = 0
    h_z: float | None = None
    h_pct: float | None = None
    h_min: float | None = None
    flags: list[str] = field(default_factory=list)
    # availability (set in analyze): is this price still something you could book?
    cached_at: str | None = None      # when the source itself last saw the price (cached sources); None = live quote
    live: bool = False                # the source is a live quote (serpapi): nothing to verify
    stale: bool = False               # cached price older than max_cached_age_days: shown, never flagged or announced
    check: str | None = None          # last live check within recheck_days: ok | none | error | None = not checked
    check_price: float | None = None  # live price pp incl. route penalty, when check == ok
    check_at: str | None = None
    dead: bool = False                # live check found nothing, or a price well above the cached one

    @property
    def score(self) -> float:
        return max([p for p in (self.pct, self.h_pct) if p is not None] or [0.0])

    @property
    def urgent(self) -> bool:
        return "urgent" in self.flags

    @property
    def confirmed(self) -> bool:
        """Safe to put on the phone: a live quote, or a cached price a live check agreed with."""
        return self.live or (self.check == "ok" and not self.dead)

    @property
    def current(self) -> bool:
        return not self.stale and not self.dead

    @property
    def price_seen_at(self) -> str | None:
        """When the price was last seen by whoever quoted it (not when we fetched it)."""
        return self.cached_at or self.obs_at


@dataclass
class WindowStat:
    source: str
    route: str
    window: str
    n: int
    median: float
    p10: float
    minimum: float
    best: Cell
    outliers: int


@dataclass
class Analysis:
    now: dt.datetime
    today: dt.date
    cells: list[Cell]
    windows: list[WindowStat]
    series: dict                      # (source, route, window) -> [(obs_day, min, median, n)]
    data_days: dict                   # source -> distinct observation days
    runs: list[dict]
    deals: list[dict]
    sources_seen: set
    collection_days: int = 0          # calendar day since the first run (day 1 = the first run's local date)
    phase: str = "learning"           # learning: show every trip | watching: outliers only
    calibration: dict | None = None   # avg "below neighbours" flags/day at several drop thresholds

    @property
    def outliers(self) -> list[Cell]:
        return sorted((c for c in self.cells if c.flags), key=lambda c: (c.priority, -c.score, c.price))

    @property
    def current(self) -> list[Cell]:
        """Cells whose price is still plausibly bookable (not stale, not found dead by a live check)."""
        return [c for c in self.cells if c.current]


def best_of(cells: list[Cell], priority: int | None = None, window: str | None = None) -> Cell | None:
    """Cheapest current cell (confirmed ones first), optionally within a window priority or a named window."""
    pool = [c for c in cells if c.current and (priority is None or c.priority == priority) and (window is None or c.window == window)]
    return min(pool, key=lambda c: (not c.confirmed, c.price)) if pool else None


def _age_days(now: dt.datetime, ts: str | None) -> float | None:
    if not ts:
        return None
    t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return (now - t).total_seconds() / 86400


def score_neighbours(group, st: dict) -> None:
    """Sets base/base_n/z/pct on every item (needs .depart .window .price) vs. trips departing within +-N days."""
    nd, min_n = int(st["neighbour_days"]), int(st["min_neighbours"])
    ords = sorted(((dt.date.fromisoformat(c.depart).toordinal(), c) for c in group), key=lambda t: t[0])
    keys = [o for o, _ in ords]
    for o, c in ords:
        near = [x.price for _, x in ords[bisect_left(keys, o - nd):bisect_right(keys, o + nd)] if x is not c]
        if len(near) < min_n:
            near = [x.price for x in group if x is not c and x.window == c.window]
        if len(near) >= min_n:
            c.base, c.base_n = median(near), len(near)
            c.z = (c.price - c.base) / robust_sigma(near)
            c.pct = 100 * (c.base - c.price) / c.base


CALIBRATION_THRESHOLDS = (10, 12, 15, 20, 25, 30)


def calibration(daily: dict, cfg: Config, days: int = 14) -> dict | None:
    """Replay the last `days` collection days: how many neighbour-outliers per day would each drop threshold have produced?"""
    st, z_thr = cfg.stats, float(cfg.stats["z_threshold"])
    obs_days = sorted({d for v in daily.values() for d in v})[-days:]
    if len(obs_days) < 3:
        return None
    counts = {t: 0 for t in CALIBRATION_THRESHOLDS}
    for d in obs_days:
        groups: dict[tuple, list] = defaultdict(list)
        for (src, route, dep, ret), vals in daily.items():
            if d in vals and dep > d:
                w = window_for(cfg, dt.date.fromisoformat(dep), dt.date.fromisoformat(ret))
                if w:
                    groups[(src, route)].append(SimpleNamespace(depart=dep, window=w.name, price=vals[d], base=None, base_n=0, z=None, pct=None))
        for g in groups.values():
            score_neighbours(g, st)
            for c in g:
                if c.z is not None and c.z <= -z_thr:
                    for t in CALIBRATION_THRESHOLDS:
                        counts[t] += c.pct >= t
    return {"days": len(obs_days), "avg_flags_per_day": {t: round(n / len(obs_days), 1) for t, n in counts.items()}}


def analyze(conn, cfg: Config, now: dt.datetime) -> Analysis:
    st = cfg.stats
    today = cfg.local_day(now)
    fresh_from = (today - dt.timedelta(days=int(st["fresh_days"]))).isoformat()
    hist_from = (today - dt.timedelta(days=int(st["history_days"]))).isoformat()
    penalty = {r.name: r.penalty_pp for r in cfg.routes}

    # daily minimum per cell (history + trend series)
    daily: dict[tuple, dict[str, float]] = defaultdict(dict)
    for r in conn.execute(
        """SELECT source, route, depart_date d, return_date r, obs_day, MIN(price_pp_czk) p FROM offers
           WHERE obs_day >= ? GROUP BY source, route, depart_date, return_date, obs_day""", (hist_from,)):
        daily[(r["source"], r["route"], r["d"], r["r"])][r["obs_day"]] = r["p"] + penalty.get(r["route"], 0.0)

    # latest observation per cell
    rows = conn.execute(
        """SELECT * FROM (
             SELECT *, ROW_NUMBER() OVER (PARTITION BY source, route, depart_date, return_date
                                          ORDER BY fetched_at DESC, price_pp_czk) rn
             FROM offers WHERE obs_day >= ? AND obs_day <= ? AND depart_date > ?) WHERE rn = 1""",
        (fresh_from, today.isoformat(), today.isoformat())).fetchall()
    cells: list[Cell] = []
    for r in rows:
        w = window_for(cfg, dt.date.fromisoformat(r["depart_date"]), dt.date.fromisoformat(r["return_date"]))
        if not w:
            continue
        pen = penalty.get(r["route"], 0.0)
        cells.append(Cell(
            source=r["source"], route=r["route"], depart=r["depart_date"], ret=r["return_date"],
            days=r["trip_days"], window=w.name, price=r["price_pp_czk"] + pen, raw_price=r["price_pp_czk"],
            stops=r["stops"], airline=r["airline"], duration_min=r["duration_min"], obs_at=r["fetched_at"],
            extra=json.loads(r["extra"]) if r["extra"] else None, priority=w.priority,
            cached_at=r["cached_at"], live=r["source"] in LIVE_SOURCES,
        ))

    # availability: stale cached prices, and what the live checks said about cached cells
    sp = cfg.source("serpapi")
    checks = db.latest_checks(conn, (today - dt.timedelta(days=int(sp.get("recheck_days", 3)))).isoformat())
    tolerance = 1 + float(sp.get("verify_tolerance_pct", 10)) / 100
    max_age = float(st["max_cached_age_days"])
    for c in cells:
        if c.live:
            continue
        age = _age_days(now, c.cached_at)
        c.stale = age is not None and age > max_age
        chk = checks.get((c.route, c.depart, c.ret))
        if chk is None:
            continue
        c.check, c.check_at = chk["status"], chk["checked_at"]
        if chk["status"] == "ok":
            c.check_price = chk["price_pp_czk"] + penalty.get(c.route, 0.0)
            c.dead = c.check_price > c.price * tolerance      # the cached fare is gone; the live price is its own serpapi cell
        elif chk["status"] == "none":
            c.dead = True

    by_group: dict[tuple, list[Cell]] = defaultdict(list)
    for c in cells:
        by_group[(c.source, c.route)].append(c)

    # stale/dead cells still serve as neighbours (the price level is real), but are never flagged themselves
    for group in by_group.values():
        score_neighbours(group, st)
        for c in group:
            z_thr, drop = cfg.thresholds(c.window)
            if c.current and c.z is not None and c.z <= -z_thr and c.pct >= drop:
                c.flags.append("below neighbours")

    for c in cells:
        if not c.current:
            continue
        past = [p for day, p in daily[(c.source, c.route, c.depart, c.ret)].items() if day < today.isoformat()]
        c.h_n = len(past)
        z_thr, drop = cfg.thresholds(c.window)
        if c.h_n >= int(st["min_history_days"]):
            c.h_median, c.h_min = median(past), min(past)
            c.h_z = (c.price - c.h_median) / robust_sigma(past)
            c.h_pct = 100 * (c.h_median - c.price) / c.h_median
            if c.h_z <= -z_thr and c.h_pct >= drop:
                c.flags.append("below its own history")
            if c.price < c.h_min - 1 and c.h_pct >= drop / 2:
                c.flags.append("new low")

    urgent = float(st["urgent_pct"])
    for c in cells:
        if c.flags and c.score >= urgent:
            c.flags.insert(0, "urgent")

    # per window
    groups: dict[tuple, list[Cell]] = defaultdict(list)
    for c in cells:
        groups[(c.source, c.route, c.window)].append(c)
    windows = []
    for (src, route, win), cs in sorted(groups.items()):
        prices = [c.price for c in cs]
        windows.append(WindowStat(src, route, win, len(cs), median(prices), percentile(prices, 0.10), min(prices),
                                  min(cs, key=lambda c: c.price), sum(1 for c in cs if c.flags)))

    # trend: per source/route/window/day, over cells still in the future on that day
    acc: dict[tuple, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for (src, route, d, ret), days in daily.items():
        w = window_for(cfg, dt.date.fromisoformat(d), dt.date.fromisoformat(ret))
        if not w:
            continue
        for day, p in days.items():
            if d > day:
                acc[(src, route, w.name)][day].append(p)
    series = {k: [(day, min(v), median(v), len(v)) for day, v in sorted(byday.items())] for k, byday in acc.items()}

    data_days = {r["source"]: r["n"] for r in conn.execute(
        "SELECT source, COUNT(DISTINCT obs_day) n FROM offers GROUP BY source")}
    runs = [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 12")]
    deals = [dict(r) for r in conn.execute("SELECT * FROM deals ORDER BY first_seen DESC, rowid DESC LIMIT 25")]
    first = conn.execute("SELECT MIN(started_at) FROM runs WHERE status != 'manual'").fetchone()[0]   # manual rows = budget bookkeeping, not real runs
    days_seen = (today - cfg.local_day(dt.datetime.fromisoformat(first.replace("Z", "+00:00")))).days + 1 if first else 0
    return Analysis(now, today, cells, windows, series, data_days, runs, deals, {c.source for c in cells},
                    collection_days=days_seen,
                    phase="learning" if days_seen <= int(st["learning_days"]) else "watching",
                    calibration=calibration(daily, cfg))
