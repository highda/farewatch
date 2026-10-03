"""Which cells get a live (SerpApi) search this run.

Live searches are the scarce resource (2 x 250 free searches a month), so they go where they change a decision:

1. anchors   - fixed sakura date pairs for a consistent live series; at most once per local day (not per run).
2. candidates - cached cells the next notification would mention (new outliers, the golden-window best, the cheapest
                overall) that have no live check within `recheck_days`. Nothing unverified reaches the phone, and
                nothing already judged is searched again for `recheck_days`.

Caps: `verify_top_k` candidates per run, `daily_budget` per local day, `monthly_budget` per calendar month (both keys together).
"""
from __future__ import annotations

import datetime as dt

from . import db, notify, stats
from .config import Route
from .sources.base import Context

Target = tuple[Route, str, str]


def remaining_budget(ctx: Context, scfg: dict) -> int:
    today = ctx.cfg.local_day(ctx.now)
    remaining = int(scfg.get("monthly_budget", 90)) - db.requests_this_month(ctx.conn, "serpapi", ctx.now)
    if scfg.get("daily_budget"):
        day_start = dt.datetime.combine(today, dt.time(), ctx.cfg.tz)
        remaining = min(remaining, int(scfg["daily_budget"]) - db.requests_since(ctx.conn, "serpapi", day_start))
    return max(remaining, 0)


def pick_targets(ctx: Context, scfg: dict) -> list[Target]:
    remaining = remaining_budget(ctx, scfg)
    if remaining <= 0:
        return []
    today = ctx.cfg.local_day(ctx.now)
    routes = {r.name: r for r in ctx.cfg.active_routes}
    if not routes:
        return []
    recheck_from = (today - dt.timedelta(days=int(scfg.get("recheck_days", 3)))).isoformat()
    checked = db.latest_checks(ctx.conn, recheck_from)
    done_today = {k for k, r in checked.items() if r["obs_day"] == today.isoformat() and r["status"] != "error"}

    targets: list[Target] = []
    first = next(iter(routes.values()))
    for a in scfg.get("anchors", []):                # "2027-03-25:2027-04-08", on the first active route
        dep, ret = a.split(":")
        if (first.name, dep, ret) not in done_today:
            targets.append((first, dep, ret))

    a = stats.analyze(ctx.conn, ctx.cfg, ctx.now)
    picked = 0
    for c in notify.candidates(ctx.cfg, a, ctx.conn):
        key = (c.route, c.depart, c.ret)
        if c.route in routes and key not in checked and picked < int(scfg.get("verify_top_k", 2)):
            targets.append((routes[c.route], c.depart, c.ret))
            picked += 1

    seen, out = set(), []
    for t in targets:
        if (t[0].name, t[1], t[2]) not in seen:
            seen.add((t[0].name, t[1], t[2]))
            out.append(t)
    return out[:remaining]
