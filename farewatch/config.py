"""Config: one TOML file, secrets from env vars, relative paths resolve against the config file."""
from __future__ import annotations

import copy
import datetime as dt
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULTS: dict = {
    "general": {
        "pax": 2,
        "currency": "CZK",
        "timezone": "UTC",
        "data_dir": "data",
        "user_agent": "farewatch/0.1 (personal fare watcher; contact: you@example.com)",
    },
    "output": {"dir": "out", "public_url": "", "title": "Fare watcher", "keep_archive_days": 30, "noindex": True,
               "full_table": "auto"},  # auto = only during the learning phase | always | never
    "trip": {"min_days": 12, "max_days": 16, "target_days": 14, "max_stops": 1},
    "stats": {
        "neighbour_days": 5,      # compare a trip with others departing within +-N days
        "min_neighbours": 4,      # fewer than this -> fall back to the whole window
        "z_threshold": 2.5,       # robust z-score needed to call something an outlier
        "min_drop_pct": 15.0,     # ...and it must also be at least this % below the baseline
        "urgent_pct": 30.0,       # this far below baseline = "urgent" (error-fare territory)
        "learning_days": 14,      # days since the first run; until then the page shows every trip
        "fresh_days": 3,          # a cell counts as "current" if observed within N days
        "max_cached_age_days": 2, # ...and a cached price (Travelpayouts `found_at`) older than this is "stale": shown, never flagged or announced
        "min_history_days": 5,    # days of history a cell needs before history-based flags apply
        "history_days": 60,
        "max_outliers": 15,
    },
    "notify": {
        "daily_digest": "learning",   # learning = daily status message during the learning phase | always | never
        "redrop_pct": 5.0,            # re-announce a trip only if it got this % cheaper than when last announced
    },
    "fx": {},                     # home-currency value of 1 unit, e.g. EUR = 24.4 (only if a source can't answer in `currency`)
    "sources": {},
    "feeds": [],
}


@dataclass(frozen=True)
class Route:
    name: str
    origin: str
    dest: str
    dest_name: str
    penalty_pp: float                      # in the home currency (`currency`), added to the price
    enabled: bool


@dataclass(frozen=True)
class Window:
    name: str
    depart_from: dt.date
    depart_to: dt.date
    priority: int = 3                      # 1 = the golden window, 2 = preferred, 3 = normal. Sorts outliers + headlines
    min_drop_pct: float | None = None      # per-window overrides of [stats]; looser for the windows you want most
    z_threshold: float | None = None
    # optional: the trip (depart..return) must also touch this date range. First matching window wins, so list it before the ones it carves out of
    overlap_from: dt.date | None = None
    overlap_to: dt.date | None = None
    headline: bool = False                 # own big price box at the top of the page (golden windows always get one)


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _date(v) -> dt.date:
    return v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v))


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))


class Config:
    def __init__(self, raw: dict, base_dir: Path):
        self.raw = _merge(DEFAULTS, raw)
        self.base_dir = base_dir
        g, t = self.raw["general"], self.raw["trip"]
        self.pax: int = int(g["pax"])
        self.currency: str = g["currency"].upper()
        self.user_agent: str = g["user_agent"]
        self.min_days, self.max_days = int(t["min_days"]), int(t["max_days"])
        self.target_days, self.max_stops = int(t["target_days"]), int(t["max_stops"])
        self.stats: dict = self.raw["stats"]
        self.output: dict = self.raw["output"]
        self.notify: dict = self.raw["notify"]
        self.feeds: list[dict] = [f for f in self.raw["feeds"] if f.get("enabled", True)]
        try:
            self.tz = ZoneInfo(g["timezone"])
        except ZoneInfoNotFoundError:  # slim containers without tzdata
            self.tz = dt.timezone.utc
        self.routes: list[Route] = [
            Route(
                name=r.get("name") or f'{r["origin"]}-{r["dest"]}',
                origin=r["origin"].upper(),
                dest=r["dest"].upper(),
                dest_name=r.get("dest_name", r["dest"]),
                penalty_pp=float(r.get("penalty_pp", r.get("penalty_czk_pp", 0))),   # old key name still accepted
                enabled=bool(r.get("enabled", True)),
            )
            for r in self.raw.get("routes", [])
        ]
        self.windows: list[Window] = [
            Window(w["name"], _date(w["depart_from"]), _date(w["depart_to"]), int(w.get("priority", 3)),
                   float(w["min_drop_pct"]) if "min_drop_pct" in w else None,
                   float(w["z_threshold"]) if "z_threshold" in w else None,
                   _date(w["overlap_from"]) if "overlap_from" in w else None,
                   _date(w["overlap_to"]) if "overlap_to" in w else None,
                   bool(w.get("headline", int(w.get("priority", 3)) == 1)))
            for w in self.raw.get("windows", [])
        ]

    # -- paths -------------------------------------------------------------
    def path(self, p: str | Path) -> Path:
        p = Path(p).expanduser()
        return p if p.is_absolute() else (self.base_dir / p)

    @property
    def db_path(self) -> Path:
        return self.path(self.raw["general"]["data_dir"]) / "farewatch.sqlite"

    @property
    def out_dir(self) -> Path:
        return self.path(self.output["dir"])

    # -- helpers -----------------------------------------------------------
    @property
    def active_routes(self) -> list[Route]:
        return [r for r in self.routes if r.enabled]

    def window(self, name: str) -> Window | None:
        return next((w for w in self.windows if w.name == name), None)

    def thresholds(self, window: str) -> tuple[float, float]:
        """(z_threshold, min_drop_pct) for a window, falling back to [stats]."""
        w = self.window(window)
        z = w.z_threshold if w and w.z_threshold is not None else float(self.stats["z_threshold"])
        drop = w.min_drop_pct if w and w.min_drop_pct is not None else float(self.stats["min_drop_pct"])
        return z, drop

    def route(self, name: str) -> Route | None:
        return next((r for r in self.routes if r.name == name), None)

    def source(self, name: str) -> dict:
        return self.raw["sources"].get(name, {})

    def secret(self, env_name: str) -> str | None:
        return os.environ.get(env_name) or None

    def to_home(self, amount: float, currency: str) -> float | None:
        """Amount in the home currency (`[general] currency`), via `[fx]` rates for anything else."""
        cur = currency.upper()
        if cur == self.currency:
            return float(amount)
        rate = self.raw["fx"].get(cur)
        return float(amount) * float(rate) if rate else None

    def local_day(self, now: dt.datetime) -> dt.date:
        return now.astimezone(self.tz).date()

    def with_overrides(self, patch: dict) -> "Config":
        return Config(_merge(self.raw, patch), self.base_dir)


def load(path: str | Path) -> Config:
    p = Path(path).expanduser().resolve()
    with p.open("rb") as f:
        raw = tomllib.load(f)
    _load_dotenv(p.parent / ".env")
    return Config(raw, p.parent)
