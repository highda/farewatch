"""What to tell the phone after a run. farewatch decides and words it; n8n only forwards `nextcloud` to
Nextcloud's admin_notifications API.

- Each trip (route + dates, any source) is announced once, and again only if it gets `redrop_pct` cheaper
  than when it was last announced, or turns urgent.
- `daily_digest`: learning (default) = one status message per day during the learning phase, even with no deals;
  always = a digest at the morning and the afternoon run (one per half of the day); never = deals only.
  In learning mode several runs a day still send at most one digest.
- Marking happens when the notification is built (at-most-once): a failed Nextcloud call is not retried.
"""
from __future__ import annotations

from . import db
from .config import Config
from .report import fdate, golden_best, money, set_currency
from .stats import Analysis, Cell

MAX_LISTED = 5
SUBJECT_MAX = 250                      # Nextcloud caps the subject at 255 chars


def _trip_key(c: Cell) -> str:
    return f"trip:{c.route}:{c.depart}:{c.ret}"


def _line(c: Cell, pax: int) -> str:
    pct = f", −{c.score:.0f} % vs baseline" if c.flags else ""
    return (f"{c.window}: {fdate(c.depart)} → {fdate(c.ret)} ({c.days} d), {c.route}: {money(c.price)} pp, "
            f"{money(c.price * pax)} for {pax}{pct} [{c.source}]")


def new_outliers(cfg: Config, a: Analysis, conn) -> list[Cell]:
    redrop = float(cfg.notify["redrop_pct"])
    seen: set[str] = set()
    out = []
    for c in a.outliers:                        # sorted: golden window first, then biggest drop
        key = _trip_key(c)
        if key in seen:                         # same trip from a second source: keep the first (better ranked)
            continue
        seen.add(key)
        prev = db.notified_get(conn, key)
        if prev is None or c.price <= prev["price_pp"] * (1 - redrop / 100) or (c.urgent and not prev["urgent"]):
            out.append(c)
    return out


def build(cfg: Config, a: Analysis, conn, mark: bool = True) -> dict:
    set_currency(cfg.currency)
    pax, report_url = cfg.pax, _report_url(cfg)
    fresh = new_outliers(cfg, a, conn)
    mode = cfg.notify["daily_digest"]
    digest_key = f"digest:{a.today.isoformat()}"
    if mode == "always":                       # twice a day: 08:00 and 16:00 runs each get one
        digest_key += ":am" if a.now.astimezone(cfg.tz).hour < 12 else ":pm"
    want_digest = (mode == "always" or (mode == "learning" and a.phase == "learning")) and bool(a.cells)
    digest = want_digest and db.notified_get(conn, digest_key) is None
    if not fresh and not digest:
        return {"send": False, "reason": "nothing new", "new_outliers": 0}

    gold = golden_best(a)
    days = a.collection_days
    demo = "[DEMO] " if "mock" in a.sources_seen else ""
    if fresh:
        head = next((c for c in fresh if c.urgent), fresh[0])
        icon = "🚨 URGENT" if head.urgent else ("🌸" if head.priority == 1 else "✈️")
        more = f" (+{len(fresh) - 1} more)" if len(fresh) > 1 else ""
        subject = (f"{icon} {head.window} {money(head.price)} pp, −{head.score:.0f} % · "
                   f"{fdate(head.depart)} → {fdate(head.ret)}, {head.route}{more}")
    else:
        c = gold or min(a.cells, key=lambda x: x.price)
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
        best = min(a.cells, key=lambda x: x.price)
        phase = (f"Learning day {days} of {cfg.stats['learning_days']}" if a.phase == "learning" else f"Day {days}")
        lines.append(f"{phase} · {len(a.cells)} trips tracked · {len(a.outliers)} outlier(s) on the page")
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
