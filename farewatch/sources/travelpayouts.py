"""Travelpayouts / Aviasales Data API: free, official, *cached* prices (what users searched recently).

Good for finding the price level and outliers across many date pairs with a handful of requests.
Not a live quote: always confirm with a live search before booking.
Token: free affiliate account -> Profile -> API token. Sent as X-Access-Token (never in the URL).
"""
from __future__ import annotations

from ..config import Route
from ..http import HttpError
from ..plan import departure_months
from .base import Context, Offer, SourceError

BASE = "https://api.travelpayouts.com"
ENDPOINTS = {
    # ~1000 cheapest round trips found in the last 48h for departures in the month
    "latest": ("/v2/prices/latest", lambda route, month, s: {
        "origin": route.origin, "destination": route.dest, "period_type": "month",
        "beginning_of_period": f"{month}-01", "one_way": "false", "limit": 1000, "sorting": "price",
    }),
    # cheapest round trip per departure day of the month
    "month_matrix": ("/v2/prices/month-matrix", lambda route, month, s: {
        "origin": route.origin, "destination": route.dest, "month": f"{month}-01",
    }),
}


def parse_rows(payload, route: Route, default_currency: str = "CZK") -> list[Offer]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    currency = str(payload.get("currency") or default_currency).upper()
    out = []
    for r in rows:
        try:
            dep, ret, price = str(r["depart_date"])[:10], str(r.get("return_date") or "")[:10], float(r["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if not ret or r.get("actual") is False:      # one-way or expired
            continue
        out.append(Offer(
            source="travelpayouts", route=route.name, origin=route.origin, dest=route.dest,
            depart_date=dep, return_date=ret, price_pp=price, currency=currency,
            stops=r.get("number_of_changes"), cached_at=r.get("found_at"),
        ))
    return out


class TravelpayoutsSource:
    name = "travelpayouts"
    kind = "discovery"

    def __init__(self, scfg: dict):
        self.scfg = scfg

    def request_params(self, endpoint: str, route: Route, month: str, currency: str = "CZK") -> tuple[str, dict]:
        path, build = ENDPOINTS[endpoint]
        params = build(route, month, self.scfg)
        params["currency"] = currency.lower()
        params["show_to_affiliates"] = "true" if self.scfg.get("show_to_affiliates", False) else "false"
        return BASE + path, params

    def collect(self, ctx: Context) -> list[Offer]:
        token = ctx.cfg.secret(self.scfg.get("token_env", "TRAVELPAYOUTS_TOKEN"))
        if not token:
            raise SourceError("missing Travelpayouts token (env TRAVELPAYOUTS_TOKEN)")
        endpoints = self.scfg.get("endpoints", ["latest", "month_matrix"])
        delay = float(self.scfg.get("min_delay_s", 2.0))
        offers: list[Offer] = []
        attempted = 0
        for route in ctx.cfg.active_routes:
            for month in departure_months(ctx.cfg):
                for ep in endpoints:
                    url, params = self.request_params(ep, route, month, ctx.cfg.currency)
                    attempted += 1
                    try:
                        payload = ctx.http.get_json(url, params, {"X-Access-Token": token}, min_delay=delay)
                    except HttpError as e:
                        ctx.errors += 1
                        ctx.log(f"travelpayouts {route.name} {month} {ep}: {e}")
                        continue
                    if isinstance(payload, dict) and payload.get("success") is False:
                        ctx.errors += 1
                        ctx.log(f"travelpayouts {route.name} {month} {ep}: API error {payload.get('error')}")
                        continue
                    offers += parse_rows(payload, route, ctx.cfg.currency)
        if attempted and ctx.errors >= attempted:
            raise SourceError("every Travelpayouts request failed")
        return offers
