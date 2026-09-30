"""SYNTHETIC prices for demos and tests. Never enable next to real sources in the same database."""
from __future__ import annotations

import datetime as dt
import hashlib
import math
import random

from ..plan import cells
from .base import Context, Offer


def _rng(*parts) -> random.Random:
    return random.Random(int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16))


def synth_price(route: str, depart: dt.date, ret: dt.date, obs_day: dt.date, base: float) -> float:
    # seasonal bump around the sakura peak + weekday effect + persistent per-cell offset + daily noise
    peak = dt.date(depart.year, 3, 29)
    season = 1 + 0.32 * math.exp(-(((depart - peak).days) / 12) ** 2)
    weekday = {0: 1.0, 1: 0.97, 2: 0.96, 3: 1.02, 4: 1.06, 5: 1.03, 6: 1.04}[depart.weekday()]
    cell = _rng("cell", route, depart, ret).gauss(0, 0.035)
    day = _rng("day", route, depart, ret, obs_day).gauss(0, 0.02)
    p = base * season * weekday * (1 + cell + day)
    if _rng("dip", route, depart, ret, obs_day).random() < 0.012:      # rare planted dip -> exercises outlier logic
        p *= 0.62
    return round(p / 10) * 10


class MockSource:
    name = "mock"
    kind = "discovery"

    def __init__(self, scfg: dict):
        self.scfg = scfg

    def collect(self, ctx: Context) -> list[Offer]:
        today = ctx.cfg.local_day(ctx.now)
        base = float(self.scfg.get("base_price_czk", 23500))
        out = []
        for route in ctx.cfg.active_routes:
            for dep, ret in cells(ctx.cfg, after=today):
                out.append(Offer(
                    source="mock", route=route.name, origin=route.origin, dest=route.dest,
                    depart_date=dep.isoformat(), return_date=ret.isoformat(),
                    price_pp=synth_price(route.name, dep, ret, today, base), stops=1, airline="SYNTHETIC",
                ))
        return out
