"""Search plan: which (route, departure, return) cells are in scope."""
from __future__ import annotations

import datetime as dt
from typing import Iterator
from urllib.parse import quote

from .config import Config, Route, Window


def _matches(w: Window, depart: dt.date, ret: dt.date | None) -> bool:
    if not w.depart_from <= depart <= w.depart_to:
        return False
    if w.overlap_from is None or w.overlap_to is None:
        return True
    return ret is not None and depart <= w.overlap_to and ret >= w.overlap_from   # trip touches the range


def window_for(cfg: Config, depart: dt.date, ret: dt.date | None = None) -> Window | None:
    """First window the trip belongs to. `ret` is needed to match windows with an overlap range."""
    return next((w for w in cfg.windows if _matches(w, depart, ret)), None)


def trip_days(depart: dt.date | str, ret: dt.date | str) -> int:
    d, r = (dt.date.fromisoformat(str(x)) for x in (depart, ret))
    return (r - d).days


def in_scope(cfg: Config, depart: str, ret: str) -> bool:
    d, r = dt.date.fromisoformat(depart), dt.date.fromisoformat(ret)
    return window_for(cfg, d, r) is not None and cfg.min_days <= trip_days(depart, ret) <= cfg.max_days


def departure_months(cfg: Config) -> list[str]:
    months: set[str] = set()
    for w in cfg.windows:
        d = w.depart_from.replace(day=1)
        while d <= w.depart_to:
            months.add(d.strftime("%Y-%m"))
            d = (d + dt.timedelta(days=32)).replace(day=1)
    return sorted(months)


def cells(cfg: Config, after: dt.date) -> Iterator[tuple[dt.date, dt.date]]:
    """Every (depart, return) pair inside a window, departing strictly after `after`."""
    seen: set[tuple[dt.date, dt.date]] = set()
    for w in cfg.windows:
        d = max(w.depart_from, after + dt.timedelta(days=1))
        while d <= w.depart_to:
            for n in range(cfg.min_days, cfg.max_days + 1):
                cell = (d, d + dt.timedelta(days=n))
                if cell not in seen and window_for(cfg, *cell) is not None:
                    seen.add(cell)
                    yield cell
            d += dt.timedelta(days=1)


# -- deep links for humans (no scraping; these just open the search) ---------------------------
def google_flights_url(route: Route, depart: str, ret: str, pax: int) -> str:
    q = f"Flights from {route.origin} to {route.dest_name} on {depart} through {ret} for {pax} adults"
    return "https://www.google.com/travel/flights?q=" + quote(q)


def aviasales_url(route: Route, depart: str, ret: str, pax: int) -> str:
    d, r = dt.date.fromisoformat(depart), dt.date.fromisoformat(ret)
    return f"https://www.aviasales.com/search/{route.origin}{d:%d%m}{route.dest}{r:%d%m}{pax}"
