"""SerpApi `google_flights`: a paid/free-tier API that returns Google Flights results as JSON.

We use it sparingly as a *live check* (free plan ~100 Google Flights searches/month): re-price the
cheapest cells the cached source found, plus a few fixed anchor date pairs for a consistent series.
Prices are requested for `pax` adults, so the quote is for that many seats at once (a 2-seat price can be more
than 2x the 1-seat price when a fare bucket runs low: seen live, 59 870 vs 2 x 29 089). The stored per-person
price is total / pax; `extra.seats_priced` records it.
"""
from __future__ import annotations

from .. import db
from ..config import Route
from ..http import HttpError
from .base import Context, Offer, SourceError

URL = "https://serpapi.com/search.json"
STOPS_PARAM = {0: 1, 1: 2, 2: 3}       # max stops -> SerpApi `stops` (1 nonstop, 2 <=1 stop, 3 <=2 stops)
# metro/city codes -> airport lists for Google Flights; add yours via [sources.serpapi] arrival_ids
DEFAULT_ARRIVALS = {"TYO": "HND,NRT", "OSA": "KIX,ITM"}


def search_params(key: str, route: Route, depart: str, ret: str, pax: int, max_stops: int, arrival_ids: dict,
                  currency: str = "CZK", gl: str = "cz") -> dict:
    """`hl`/`gl`/`currency` are always pinned: Google Flights prices shift with the country edition, so an unpinned
    series would mix geographies. `gl` = the point-of-sale country (`[sources.serpapi] gl`, default cz)."""
    arrivals = {**DEFAULT_ARRIVALS, **arrival_ids}
    return {
        "engine": "google_flights", "api_key": key, "departure_id": route.origin,
        "arrival_id": arrivals.get(route.dest, route.dest), "outbound_date": depart, "return_date": ret,
        "type": 1, "adults": pax, "currency": currency.upper(), "hl": "en", "gl": gl.lower(),
        "stops": STOPS_PARAM.get(max_stops, 0),
    }


def parse_response(payload: dict, route: Route, depart: str, ret: str, pax: int = 1, currency: str = "CZK") -> list[Offer]:
    err = payload.get("error")
    if err:
        if "hasn't returned any results" in str(err):
            return []
        raise SourceError(f"SerpApi: {err}")
    flights = (payload.get("best_flights") or []) + (payload.get("other_flights") or [])
    priced = [f for f in flights if isinstance(f.get("price"), (int, float))]
    if not priced:
        return []
    best = min(priced, key=lambda f: f["price"])
    airlines = []
    for leg in best.get("flights") or []:
        if leg.get("airline") and leg["airline"] not in airlines:
            airlines.append(leg["airline"])
    ins = payload.get("price_insights") or {}
    per = lambda v: round(v / pax) if isinstance(v, (int, float)) else v
    extra = {"lowest_price": per(ins.get("lowest_price")), "price_level": ins.get("price_level"),
             "typical_price_range": [per(v) for v in ins["typical_price_range"]] if ins.get("typical_price_range") else None}
    extra = {k: v for k, v in extra.items() if v is not None}
    return [Offer(
        source="serpapi", route=route.name, origin=route.origin, dest=route.dest,
        depart_date=depart, return_date=ret, price_pp=float(best["price"]) / pax, currency=currency.upper(),
        stops=len(best.get("layovers") or []), airline=", ".join(airlines) or None,
        duration_min=best.get("total_duration"),
        extra={"seats_priced": pax, "total": float(best["price"]), **({"insights": extra} if extra else {})},
    )]


def key_unusable(e: Exception) -> bool:
    """The key itself is the problem (quota used up, revoked, invalid), so retrying it is pointless; try the backup."""
    if isinstance(e, HttpError):
        return e.status in (401, 403, 429)
    text = str(e).lower()
    return any(w in text for w in ("run out of searches", "out of searches", "invalid api key", "quota"))


def api_keys(cfg, scfg: dict) -> list[str]:
    """Primary key first, then the backup (env named by `api_key_backup_env`, default SERPAPI_KEY_BACKUP)."""
    names = [scfg.get("api_key_env", "SERPAPI_KEY"), scfg.get("api_key_backup_env", "SERPAPI_KEY_BACKUP")]
    keys: list[str] = []
    for n in names:
        k = cfg.secret(n)
        if k and k not in keys:
            keys.append(k)
    return keys


class SerpApiSource:
    name = "serpapi"
    kind = "verify"

    def __init__(self, scfg: dict):
        self.scfg = scfg

    def collect(self, ctx: Context, targets: list[tuple[Route, str, str]]) -> list[Offer]:
        """One live search per target. Every answer is also written to `checks`, including "no fare found" and
        request errors, so a cached cell can later be judged as confirmed / dead / unverified (stats.analyze)."""
        keys = api_keys(ctx.cfg, self.scfg)
        if not keys:
            raise SourceError("missing SerpApi key (env SERPAPI_KEY)")
        k = 0                                        # index of the key in use; moves on when one is exhausted/rejected, for the rest of the run
        offers: list[Offer] = []
        obs_day = ctx.cfg.local_day(ctx.now)
        for route, depart, ret in targets:
            while True:
                params = search_params(keys[k], route, depart, ret, ctx.cfg.pax, ctx.cfg.max_stops, self.scfg.get("arrival_ids", {}),
                                       ctx.cfg.currency, str(self.scfg.get("gl", "cz")))
                try:
                    payload = ctx.http.get_json(URL, params, min_delay=float(self.scfg.get("min_delay_s", 3.0)), retry_429=False)
                    found = parse_response(payload, route, depart, ret, ctx.cfg.pax, ctx.cfg.currency)
                    offers += found
                    home = ctx.cfg.to_home(found[0].price_pp, found[0].currency) if found else None
                    db.insert_check(ctx.conn, ctx.run_id, ctx.now, obs_day, route.name, depart, ret,
                                    "ok" if home is not None else "none", home)
                    break
                except (HttpError, SourceError) as e:
                    if key_unusable(e) and k + 1 < len(keys):
                        ctx.log(f"serpapi key {k + 1} unusable ({e}); switching to key {k + 2}")
                        k += 1
                        continue                     # same cell again with the next key
                    ctx.errors += 1
                    ctx.log(f"serpapi {route.name} {depart}->{ret}: {e}")
                    db.insert_check(ctx.conn, ctx.run_id, ctx.now, obs_day, route.name, depart, ret, "error", None, str(e))
                    break
        if targets and ctx.errors >= len(targets):
            raise SourceError("every SerpApi request failed")
        return offers
