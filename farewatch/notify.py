"""What to tell the phone after a run. farewatch decides and words it; n8n only forwards `nextcloud` to
Nextcloud's admin_notifications API.

- Each trip (route + dates, any source) is announced once, and again only if it gets `redrop_pct` cheaper
  than when it was last announced, or turns urgent.
- `daily_digest`: learning (default) = one status message per morning/afternoon run during the learning phase, even with no deals;
  always = every day; never = deals only. A digest goes out at most once per half of the day (the 08:00 and the
  16:00 run each get one; extra manual runs stay silent).
- Marking happens when the notification is built (at-most-once): a failed Nextcloud call is not retried.
- Only *confirmed* cells are announced or quoted in the digest: live quotes, or cached prices that a live check
  (SerpApi, see verify.py) agreed with. Stale or dead cached prices never reach the phone; an unverified one waits
  for its check (it stays unannounced, so it is picked up as soon as the budget allows). Without a live source
  configured there is nothing to confirm against, and cached prices go out as before, marked "unverified".
"""
from __future__ import annotations

from . import db
from .config import Config
from .report import fdate, money, set_currency, status_text
from .stats import Analysis, Cell, best_of

MAX_LISTED = 5
SUBJECT_MAX = 250                      # Nextcloud caps the subject at 255 chars


def _trip_key(c: Cell) -> str:
    return f"trip:{c.route}:{c.depart}:{c.ret}"


def _line(c: Cell, pax: int) -> str:
    pct = f", −{c.score:.0f} % vs baseline" if c.flags else ""
    return (f"{c.window}: {fdate(c.depart)} → {fdate(c.ret)} ({c.days} d), {c.route}: {money(c.price)} pp, "
            f"{money(c.price * pax)} for {pax}{pct} [{c.source}, {status_text(c)}]")


def verification_possible(cfg: Config) -> bool:
    return bool(cfg.source("serpapi").get("enabled"))


def _announceable(cfg: Config, c: Cell) -> bool:
    return c.confirmed or not verification_possible(cfg)


def new_outliers(cfg: Config, a: Analysis, conn, confirmed_only: bool = True) -> list[Cell]:
    """Outliers not announced yet (or `redrop_pct` cheaper / newly urgent since). With `confirmed_only` (the
    default) unverified cached cells are left out: they are not announced *and* not marked, so they get their
    live check and go out on a later run."""
    redrop = float(cfg.notify["redrop_pct"])
    seen: set[str] = set()
    out = []
    for c in a.outliers:                        # sorted: golden window first, then biggest drop; stale/dead already excluded
        key = _trip_key(c)
        if key in seen:                         # same trip from a second source: keep the first (better ranked)
            continue
        seen.add(key)
        if confirmed_only and not _announceable(cfg, c):
            continue
        prev = db.notified_get(conn, key)
        if prev is None or c.price <= prev["price_pp"] * (1 - redrop / 100) or (c.urgent and not prev["urgent"]):
            out.append(c)
    return out


def candidates(cfg: Config, a: Analysis, conn) -> list[Cell]:
    """Cached cells worth a live search before the next notification, best first: the outliers it would announce,
    then the golden-window best and the cheapest overall (quoted in the digest). Only unverified, current cells."""
    pool = new_outliers(cfg, a, conn, confirmed_only=False)
    pool += [c for c in (best_of(a.cells, priority=1), best_of(a.cells)) if c]
    seen: set[str] = set()
    out = []
    for c in pool:
        key = _trip_key(c)
        if key not in seen and c.current and not c.live and c.check is None:
            seen.add(key)
            out.append(c)
    return out


def build(cfg: Config, a: Analysis, conn, mark: bool = True) -> dict:
    set_currency(cfg.currency)
    pax, report_url = cfg.pax, _report_url(cfg)
    fresh = new_outliers(cfg, a, conn)
    mode = cfg.notify["daily_digest"]
    digest_key = f"digest:{a.today.isoformat()}"
    digest_key += ":am" if a.now.astimezone(cfg.tz).hour < 12 else ":pm"
    current = a.current
    want_digest = (mode == "always" or (mode == "learning" and a.phase == "learning")) and bool(current)
    digest = want_digest and db.notified_get(conn, digest_key) is None
    if not fresh and not digest:
        return {"send": False, "reason": "nothing new", "new_outliers": 0}

    gold, best = best_of(a.cells, priority=1), best_of(a.cells)
    days = a.collection_days
    demo = "[DEMO] " if "mock" in a.sources_seen else ""
    if fresh:
        head = next((c for c in fresh if c.urgent), fresh[0])
        icon = "🚨 URGENT" if head.urgent else ("🌸" if head.priority == 1 else "✈️")
        more = f" (+{len(fresh) - 1} more)" if len(fresh) > 1 else ""
        subject = (f"{icon} {head.window} {money(head.price)} pp, −{head.score:.0f} % · "
                   f"{fdate(head.depart)} → {fdate(head.ret)}, {head.route}{more}")
    else:
        c = gold or best
        subject = (f"🌸 Day {days}/{cfg.stats['learning_days']} · best {c.window} {money(c.price)} pp · "
                   f"{fdate(c.depart)} → {fdate(c.ret)}, {c.route}")
    subject = demo + subject[:SUBJECT_MAX - len(demo) - 10] + " · {report}"

    lines = []
    if fresh:
        lines.append(f"New deal{'s' if len(fresh) > 1 else ''}:")
        lines += [f"• {'URGENT ' if c.urgent else ''}{_line(c, pax)}" for c in fresh[:MAX_LISTED]]
        if len(fresh) > MAX_LISTED:
            lines.append(f"• …and {len(fresh) - MAX_LISTED} more on the page")
    if digest:
        phase = (f"Learning day {days} of {cfg.stats['learning_days']}" if a.phase == "learning" else f"Day {days}")
        gone = len(a.cells) - len(current)
        lines.append(f"{phase} · {len(current)} current trips" + (f" ({gone} stale/gone hidden)" if gone else "")
                     + f" · {len(a.outliers)} outlier(s) on the page")
        if gold:
            lines.append(f"Best golden-window trip: {_line(gold, pax)}")
        if best is not gold:
            lines.append(f"Cheapest overall: {_line(best, pax)}")
    lines.append("Report: {report}")

    if mark:
        at = db.iso(a.now)
        for c in fresh:
            db.notified_set(conn, _trip_key(c), c.price, c.urgent, at)
        if digest:
            db.notified_set(conn, digest_key, None, False, at)
        conn.commit()

    param = {"report": {"type": "highlight", "id": "farewatch", "name": "open report", "link": report_url or ""}}
    return {
        "send": True, "urgent": any(c.urgent for c in fresh), "new_outliers": len(fresh), "digest": digest,
        "nextcloud": {"subject": subject, "subjectParameters": param,
                      "message": "\n".join(lines), "messageParameters": param},
    }


def _report_url(cfg: Config) -> str | None:
    base = (cfg.output.get("public_url") or "").rstrip("/")
    return base + "/index.html" if base else None
