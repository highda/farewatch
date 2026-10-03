"""Collect: run enabled sources -> normalise -> keep the cheapest offer per cell -> store."""
from __future__ import annotations

import datetime as dt
import sys
from typing import Callable

from . import db, plan
from .config import Config
from .http import Http
from .sources import DISCOVERY, VERIFY, feeds
from .sources.base import Context, Offer, SourceError
from .verify import pick_targets


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def normalise(cfg: Config, offers: list[Offer], log: Callable[[str], None]) -> list[Offer]:
    """Convert to the home currency, drop out-of-scope cells / too many stops, keep the cheapest offer per (source, cell)."""
    best: dict[tuple, Offer] = {}
    dropped_fx = 0
    for o in offers:
        if not plan.in_scope(cfg, o.depart_date, o.return_date):
            continue
        if o.stops is not None and o.stops > cfg.max_stops:
            continue
        home = cfg.to_home(o.price_pp, o.currency)
        if home is None:
            dropped_fx += 1
            continue
        o.price_pp_czk, o.trip_days = round(home, 2), plan.trip_days(o.depart_date, o.return_date)
        key = (o.source, o.route, o.depart_date, o.return_date)
        if key not in best or o.price_pp_czk < best[key].price_pp_czk:
            best[key] = o
    if dropped_fx:
        log(f"dropped {dropped_fx} offers in a currency with no [fx] rate configured")
    return list(best.values())


def collect(cfg: Config, conn, now: dt.datetime | None = None, only: set[str] | None = None,
            log: Callable[[str], None] = _stderr, http: Http | None = None) -> list[dict]:
    """Returns one result dict per source run: {source, status, requests, offers, error}."""
    now = now or dt.datetime.now(dt.timezone.utc)
    http = http or Http(cfg.user_agent)
    ctx = Context(cfg, conn, http, now, log)
    obs_day = cfg.local_day(now)
    results: list[dict] = []

    def run_source(name: str, kind: str, fn: Callable[[], list[Offer]]) -> None:
        run_id = db.start_run(conn, name, kind, now)
        ctx.errors, ctx.run_id, before = 0, run_id, http.requests
        try:
            offers = normalise(cfg, fn(), log)
            n = db.insert_offers(conn, run_id, offers, now, obs_day)
            status, err = ("partial" if ctx.errors else "ok"), None
        except SourceError as e:
            n, status, err = 0, "error", str(e)
            log(f"{name}: {e}")
        except Exception as e:  # noqa: BLE001 - one broken source must not stop the others
            n, status, err = 0, "error", f"{type(e).__name__}: {e}"
            log(f"{name}: unexpected {err}")
        req = http.requests - before
        db.finish_run(conn, run_id, dt.datetime.now(dt.timezone.utc), status=status, requests=req, offers=n, error=err)
        results.append({"source": name, "status": status, "requests": req, "offers": n, "error": err})
        log(f"{name}: {status}, {req} requests, {n} offers" + (f" ({err})" if err else ""))

    wanted = lambda n: (only is None or n in only) and cfg.source(n).get("enabled", False)
    for name, cls in DISCOVERY.items():
        if wanted(name):
            src = cls(cfg.source(name))
            run_source(name, src.kind, lambda src=src: src.collect(ctx))
    for name, cls in VERIFY.items():
        if wanted(name):
            src = cls(cfg.source(name))
            targets = pick_targets(ctx, cfg.source(name))
            if not targets:
                log(f"{name}: nothing to verify (no fresh cells, or monthly budget used)")
                continue
            run_source(name, src.kind, lambda src=src, t=targets: src.collect(ctx, t))
    if cfg.feeds and (only is None or "feeds" in only):
        run_id = db.start_run(conn, "feeds", "feed", now)
        ctx.errors = 0
        try:
            req, new = feeds.run(ctx)
            status, err = ("partial" if ctx.errors else "ok"), None
        except Exception as e:  # noqa: BLE001
            req, new, status, err = 0, 0, "error", f"{type(e).__name__}: {e}"
        db.finish_run(conn, run_id, dt.datetime.now(dt.timezone.utc), status=status, requests=req, offers=new, error=err)
        results.append({"source": "feeds", "status": status, "requests": req, "offers": new, "error": err})
        log(f"feeds: {status}, {req} requests, {new} new deals")
    return results
