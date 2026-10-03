"""Static HTML digest (single self-contained file, inline CSS + SVG, no JS) plus summary.json."""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import shutil
import statistics
from pathlib import Path

from .config import Config
from .plan import aviasales_url, google_flights_url
from .stats import Analysis, Cell, best_of, median

e = html.escape
NB = " "


SYMBOLS = {"CZK": "Kč", "EUR": "€", "USD": "$", "GBP": "£", "PLN": "zł", "HUF": "Ft", "CHF": "CHF", "JPY": "¥"}
_currency = "CZK"


def set_currency(code: str) -> None:
    """Currency label used by money(); called at the start of render()/notify.build() with cfg.currency."""
    global _currency
    _currency = code.upper()


def money(v: float | None) -> str:
    return "–" if v is None else f"{v:,.0f}".replace(",", NB) + NB + SYMBOLS.get(_currency, _currency)


def vs_baseline(c: Cell) -> str:
    """Signed % vs. neighbour median: negative = cheaper than neighbours."""
    if c.pct is None:
        return "–"
    v = -c.pct
    return f"{v:+.0f} %".replace("-", "−")


def fdate(iso: str) -> str:
    d = dt.date.fromisoformat(iso)
    return f"{d:%a} {d.day} {d:%b}"


def age(now: dt.datetime, iso_ts: str | None) -> str:
    if not iso_ts:
        return "?"
    t = dt.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    h = (now - t).total_seconds() / 3600
    return f"{h:.0f} h ago" if h < 48 else f"{h / 24:.0f} d ago"


def status_text(c: Cell, now: dt.datetime | None = None) -> str:
    """Availability in words: what a live check said, or why the price can't be trusted yet. Shared with notify."""
    when = f", {age(now, c.check_at)}" if now and c.check_at else ""
    if c.live:
        return "live quote"
    if c.check == "ok" and not c.dead:
        return f"live-checked {money(c.check_price)}{when}"
    if c.check == "ok":
        return f"gone: live {money(c.check_price)}{when}"
    if c.check == "none":
        return f"gone: no fare found live{when}"
    if c.stale:
        return "stale"
    return "unverified"


def status_html(c: Cell, now: dt.datetime) -> str:
    cls = "ok" if c.confirmed else ("bad" if c.dead else "muted")
    mark = "✓ " if c.confirmed else ("✗ " if c.dead else "")
    return f"<span class='{cls}'>{mark}{e(status_text(c, now))}</span>"


def seen_text(c: Cell, now: dt.datetime) -> str:
    """When the price was last seen by the source (cached) or quoted (live): the honest age, not our fetch time."""
    return ("quoted " if c.live else "seen ") + age(now, c.price_seen_at)


def nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    span = max(hi - lo, 1.0)
    raw = span / n
    mag = 10 ** (len(str(int(raw))) - 1) if raw >= 1 else 1
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    t = (lo // step) * step
    out = []
    while t <= hi + step:
        out.append(t)
        t += step
    return [x for x in out if lo - step <= x <= hi + step]


# -- charts ------------------------------------------------------------------------------------
W, H, ML, MR, MT, MB = 760, 250, 62, 30, 12, 30


def _frame(ymin, ymax, xlabels: list[tuple[float, str]], xmin, xmax) -> tuple[str, callable, callable]:
    px = lambda x: ML + (x - xmin) / max(xmax - xmin, 1e-9) * (W - ML - MR)
    ticks = [t for t in nice_ticks(ymin, ymax) if ymin <= t <= ymax] or [ymin, ymax]
    py = lambda y: MT + (1 - (y - ymin) / max(ymax - ymin, 1e-9)) * (H - MT - MB)
    g = ['<g class="grid">']
    for t in ticks:
        g.append(f'<line x1="{ML}" x2="{W - MR}" y1="{py(t):.1f}" y2="{py(t):.1f}"/>'
                 f'<text class="ytick" x="{ML - 8}" y="{py(t) + 4:.1f}" text-anchor="end">{t:,.0f}'.replace(",", NB) + "</text>")
    for x, lab in xlabels:
        g.append(f'<text class="xtick" x="{px(x):.1f}" y="{H - 8}" text-anchor="middle">{e(lab)}</text>')
    g.append("</g>")
    return "".join(g), px, py


def strip_chart(cells: list[Cell], neighbour_days: int, pax: int) -> str:
    """One dot per departure date (cheapest cell that day), local-median line, outliers highlighted."""
    best: dict[str, Cell] = {}
    for c in cells:
        if c.depart not in best or c.price < best[c.depart].price:
            best[c.depart] = c
    dates = sorted(best)
    if not dates:
        return ""
    o = lambda s: dt.date.fromisoformat(s).toordinal()
    xmin, xmax = o(dates[0]), o(dates[-1])
    allp = [c.price for c in cells]
    local = []
    for d in dates:
        near = [c.price for c in cells if abs(o(c.depart) - o(d)) <= neighbour_days]
        local.append((o(d), median(near)))
    lo, hi = min(allp + [p for _, p in local]), max(allp + [p for _, p in local])
    pad = (hi - lo) * 0.08 or hi * 0.05
    ymin, ymax = max(0, lo - pad), hi + pad
    step = max(1, (xmax - xmin) // 6)
    xl = [(x, fdate(dt.date.fromordinal(x).isoformat())) for x in range(xmin, xmax + 1, step)]
    frame, px, py = _frame(ymin, ymax, xl, xmin - 0.5, xmax + 0.5)
    line = " ".join(f"{'M' if i == 0 else 'L'}{px(x):.1f},{py(p):.1f}" for i, (x, p) in enumerate(local))
    marks = []
    for d in dates:
        c = best[d]
        cls = "dot out" if c.flags else "dot"
        tip = (f"{fdate(c.depart)} → {fdate(c.ret)} ({c.days} d) · {money(c.price)} pp · {money(c.price * pax)} for {pax}"
               + (f" · {', '.join(c.flags)}" if c.flags else ""))
        r = 5 if c.flags else 4
        marks.append(f'<g><circle class="hit" cx="{px(o(d)):.1f}" cy="{py(c.price):.1f}" r="11"/>'
                     f'<circle class="{cls}" cx="{px(o(d)):.1f}" cy="{py(c.price):.1f}" r="{r}"/><title>{e(tip)}</title></g>')
    return (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Cheapest fare per departure date">{frame}'
            f'<path class="median" d="{line}"/>{"".join(marks)}</svg>')


def trend_chart(series: list[tuple]) -> str:
    """series rows: (obs_day, min, median, n)."""
    if not series:
        return ""
    o = lambda s: dt.date.fromisoformat(s).toordinal()
    xmin, xmax = o(series[0][0]), o(series[-1][0])
    if xmax == xmin:
        xmin, xmax = xmin - 1, xmax + 1
    vals = [v for r in series for v in (r[1], r[2])]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.1 or hi * 0.05
    ymin, ymax = max(0, lo - pad), hi + pad
    span = xmax - xmin
    step = max(1, span // 6)
    xl = [(x, fdate(dt.date.fromordinal(x).isoformat())) for x in range(xmin, xmax + 1, step)]
    frame, px, py = _frame(ymin, ymax, xl, xmin, xmax)
    out = [frame]
    for idx, cls in ((1, "series"), (2, "median")):
        pts = [(px(o(r[0])), py(r[idx])) for r in series]
        if len(pts) > 1:
            out.append(f'<path class="{cls}" d="' + " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(pts)) + '"/>')
        for r, (x, y) in zip(series, pts):
            label = "cheapest cell" if idx == 1 else "median cell"
            out.append(f'<g><circle class="hit" cx="{x:.1f}" cy="{y:.1f}" r="10"/>'
                       f'<circle class="dot {"" if idx == 1 else "med"}" cx="{x:.1f}" cy="{y:.1f}" r="4"/>'
                       f'<title>{e(fdate(r[0]))}: {label} {e(money(r[idx]))} pp ({r[3]} cells)</title></g>')
    return f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Price trend per collection day">{"".join(out)}</svg>'


# -- page --------------------------------------------------------------------------------------
CSS = """
:root{color-scheme:light;--bg:#fcfcfb;--card:#ffffff;--ink:#0b0b0b;--ink2:#52514e;--muted:#807f7a;--line:#e4e3df;
--s1:#2a78d6;--s2:#eb6834;--median:#8c8b85;--warn-bg:#fff4e0;--warn-ink:#6b4200;--good:#006300}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;--bg:#141413;--card:#1a1a19;
--ink:#fff;--ink2:#c3c2b7;--muted:#8f8e86;--line:#2e2e2b;--s1:#3987e5;--s2:#d95926;--median:#8f8e86;--warn-bg:#3a2b0d;--warn-ink:#f3d59a;--good:#4cc24c}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#141413;--card:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--muted:#8f8e86;--line:#2e2e2b;
--s1:#3987e5;--s2:#d95926;--median:#8f8e86;--warn-bg:#3a2b0d;--warn-ink:#f3d59a;--good:#4cc24c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 2px}h2{font-size:17px;margin:32px 0 10px}
h3{font-size:15px;margin:0}.sub{color:var(--ink2);margin:0 0 18px;font-size:13px}.muted{color:var(--muted)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:0 0 16px}
.banner{background:var(--warn-bg);color:var(--warn-ink);border-radius:10px;padding:10px 14px;margin:0 0 14px;font-size:14px}
.heroes{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px;margin:0 0 16px}.heroes .card{margin:0}
.hero .big{font-size:40px;margin:6px 0 8px;font-weight:650;line-height:1;letter-spacing:-.02em}.hero .lbl{color:var(--ink2);font-size:13px}
.hero .side{font-size:14px;color:var(--ink2)}
table{width:100%;border-collapse:collapse;font-size:13.5px}th{font-weight:600;color:var(--ink2);text-align:left;font-size:12px}
th,td{padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}.nw{white-space:nowrap}
.scroll{overflow-x:auto}a{color:var(--s1)}.tag{display:inline-block;font-size:11.5px;border:1px solid var(--line);border-radius:6px;padding:0 6px;margin:0 4px 2px 0;color:var(--ink2)}
.tag.urgent{border-color:var(--s2);color:var(--ink);font-weight:600}
.legend{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:12.5px;color:var(--ink2);margin:8px 0 2px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:-1px}
.legend i.ln{width:16px;height:2px;border-radius:1px;vertical-align:3px}
svg{width:100%;height:auto;display:block}.grid line{stroke:var(--line);stroke-width:1}.grid text{fill:var(--muted);font-size:11px}
.dot{fill:var(--s1);stroke:var(--card);stroke-width:2}.dot.out{fill:var(--s2)}.dot.med{fill:var(--median)}.hit{fill:transparent}
.median{fill:none;stroke:var(--median);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.series{fill:none;stroke:var(--s1);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.stats{display:flex;flex-wrap:wrap;gap:4px 20px;font-size:13.5px;color:var(--ink2);margin:4px 0 6px}.stats b{color:var(--ink)}
details summary{cursor:pointer;color:var(--ink2);font-size:13.5px;margin:6px 0}.ok{color:var(--good)}.bad{color:#d03b3b}
ul.deals{list-style:none;margin:0;padding:0}ul.deals li{padding:8px 0;border-bottom:1px solid var(--line)}
"""


def _cell_links(cfg: Config, c: Cell) -> str:
    r = cfg.route(c.route)
    if not r:
        return ""
    return (f'<a href="{e(google_flights_url(r, c.depart, c.ret, cfg.pax))}" rel="noopener noreferrer">Google Flights</a> · '
            f'<a href="{e(aviasales_url(r, c.depart, c.ret, cfg.pax))}" rel="noopener noreferrer">Aviasales</a>')


def seats_quoted(c: Cell) -> int:
    """How many seats the source priced at once (SerpApi: pax; cached sources: 1, so totals are 1-seat x pax)."""
    return int((c.extra or {}).get("seats_priced") or 1)


def total_note(c: Cell, pax: int) -> str:
    q = seats_quoted(c)
    return f"{money(c.price * pax)} for {pax}" + ("" if pax == 1 else
        f" · {pax} seats quoted" if q == pax else f" · 1 seat × {pax}, check {pax} seats")


def golden_best(a: Analysis) -> Cell | None:
    return best_of(a.cells, priority=1)


def _trip(c: Cell) -> str:
    return f"<span class='nw'>{fdate(c.depart)} → {fdate(c.ret)}</span> <span class='muted nw'>({c.days} d)</span>"


def _outlier_rows(cfg: Config, a: Analysis, cells: list[Cell]) -> str:
    rows = []
    for c in cells:
        why = "".join(f'<span class="tag{" urgent" if f == "urgent" else ""}">{e(f)}</span>' for f in c.flags)
        base = c.h_median if ("below its own history" in c.flags and c.h_median) else c.base
        detail = " ".join(x for x in (
            f"{c.stops} stop{'s' if c.stops != 1 else ''}" if c.stops is not None else "",
            e(c.airline) if c.airline else "") if x)
        rows.append(
            f"<tr><td>{_trip(c)}<br><span class='muted'>{e(c.route)} · {e(c.window)}</span></td>"
            f"<td class='n'><b>{money(c.price)}</b><br><span class='muted'>{total_note(c, cfg.pax)}</span></td>"
            f"<td class='n'>{money(base)}<br><span class='muted'>−{c.score:.0f} %</span></td>"
            f"<td>{why}<br><span class='muted'>{detail}</span></td>"
            f"<td>{e(c.source)}<br><span class='muted'>{e(seen_text(c, a.now))}</span><br>{status_html(c, a.now)}</td>"
            f"<td>{_cell_links(cfg, c)}</td></tr>")
    return "".join(rows)


def _calibration_card(a: Analysis, st: dict) -> str:
    cal = a.calibration
    if not cal:
        return ""
    cells = "".join(
        f"<td class='n'>{v:g}</td>" for v in cal["avg_flags_per_day"].values())
    heads = "".join(f"<th class='n'>≥{t} %</th>" for t in cal["avg_flags_per_day"])
    return (f'<div class="card scroll"><h3>Threshold check</h3><p class="muted" style="margin:4px 0 8px;font-size:13px">'
            f'Average number of trips per day that would have been flagged over the last {cal["days"]} collection days '
            f'(at ≥{st["z_threshold"]:.1f}σ), for different minimum price drops. Aim for a handful per day at most; '
            f'the current setting is ≥{st["min_drop_pct"]:.0f} %.</p>'
            f'<table><thead><tr><th>Min. drop vs. neighbours</th>{heads}</tr></thead>'
            f'<tbody><tr><td>flagged trips / day</td>{cells}</tr></tbody></table></div>')


def render(cfg: Config, a: Analysis) -> tuple[str, dict]:
    set_currency(cfg.currency)
    st, pax = cfg.stats, cfg.pax
    local_now = a.now.astimezone(cfg.tz)
    best = best_of(a.cells)
    max_days_data = a.collection_days
    stale, dead = sum(c.stale for c in a.cells), sum(c.dead for c in a.cells)
    parts: list[str] = []

    if "mock" in a.sources_seen:
        parts.append('<div class="banner"><b>SYNTHETIC DEMO DATA.</b> These are generated numbers, not real fares.</div>')
    if not a.cells:
        parts.append('<div class="banner">No current observations yet. Run <code>collect</code> first '
                     '(and check the run log at the bottom).</div>')
    elif a.phase == "learning":
        parts.append(
            f'<div class="banner"><b>Learning phase: day {max_days_data} of {st["learning_days"]}.</b> Every tracked trip is listed '
            f'below so you can see what the market looks like. From day {int(st["learning_days"]) + 1} this page shows only outliers '
            f'(≥{st["min_drop_pct"]:.0f} % and {st["z_threshold"]:.1f}σ below the baseline). Comparisons with each trip’s own history '
            f'start once it has {st["min_history_days"]} days of data.</div>')
        parts.append(_calibration_card(a, st))
    if stale or dead:
        parts.append(
            f'<div class="banner">Availability: <b>{stale} stale</b> cached price(s) (seen by the source more than '
            f'{st["max_cached_age_days"]:g} days ago) and <b>{dead} gone</b> (a live check found no such fare) are listed greyed '
            f'out for the record, but are never flagged, used in headline boxes, or sent to the phone. Only ✓ live-checked prices are announced.</div>')

    gold = golden_best(a)
    boxes: list[tuple[Cell | None, str]] = []
    for w in cfg.windows:                                   # one box per headline window, in config order
        if w.headline:
            boxes.append((best_of(a.cells, window=w.name), f"Best {w.name} (for {pax})"))
    if best and all(best is not c for c, _ in boxes):
        boxes.append((best, f"Cheapest trip overall (for {pax})"))
    hero_html = []
    for c, label in boxes:
        if c is None:
            hero_html.append(f'<div class="card hero"><div class="lbl">{e(label)}</div><div class="big muted">–</div>'
                             f'<div class="side">no current fares</div></div>')
            continue
        hero_html.append(
            f'<div class="card hero"><div class="lbl">{e(label)}</div>'
            f'<div class="big">{money(c.price * pax)}</div>'
            f'<div class="side">{money(c.price)} per person<br>'
            f'{_trip(c)} · {e(c.route)}<br><span class="muted">{e(c.window)} · {e(c.source)}, {e(seen_text(c, a.now))}</span><br>'
            f'{status_html(c, a.now)}<br><span class="muted">{_cell_links(cfg, c)}</span></div></div>')
    if hero_html:
        parts.append('<div class="heroes">' + "".join(hero_html) + "</div>")

    outs = a.outliers[: int(st["max_outliers"])]
    parts.append("<h2>Cheap outliers" + (" <span class=\"muted\" style=\"font-weight:400\">(provisional)</span>" if a.phase == "learning" else "") + "</h2>")
    if outs:
        parts.append('<div class="card scroll"><table><thead><tr><th>Trip</th><th class="n">Price (pp)</th><th class="n">Baseline</th>'
                     '<th>Why</th><th>Source · availability</th><th>Check</th></tr></thead><tbody>'
                     + _outlier_rows(cfg, a, outs) + "</tbody></table></div>")
    else:
        parts.append('<div class="card muted">Nothing unusual right now: no trip is ≥'
                     f'{st["min_drop_pct"]:.0f} % and {st["z_threshold"]:.1f}σ below its neighbours or its own history.</div>')

    # live verification (serpapi) vs cached price for the same cell
    live = [c for c in a.cells if c.source == "serpapi"]
    if live:
        cached = {(c.route, c.depart, c.ret): c for c in a.cells if c.source != "serpapi"}
        rows = []
        for c in sorted(live, key=lambda c: c.price):
            other = cached.get((c.route, c.depart, c.ret))
            ins = (c.extra or {}).get("insights") or {}
            rng = ins.get("typical_price_range")
            rows.append(f"<tr><td>{_trip(c)}<br><span class='muted'>{e(c.route)} · {e(c.window)}</span></td><td class='n'><b>{money(c.price)}</b></td>"
                        f"<td class='n'>{money(other.price) if other else '–'}</td>"
                        f"<td>{e(str(ins.get('price_level', '–')))}"
                        + (f" <span class='muted'>(typical {money(rng[0])}–{money(rng[1])})</span>" if rng and len(rng) == 2 else "")
                        + f"</td><td>{_cell_links(cfg, c)}</td></tr>")
        parts.append("<h2>Live check (Google Flights)</h2><div class='card scroll'><table><thead><tr><th>Trip</th>"
                     "<th class='n'>Live (pp)</th><th class='n'>Cached (pp)</th><th>Google says</th><th>Check</th></tr></thead><tbody>"
                     + "".join(rows) + "</tbody></table></div>")

    # per-window sections
    parts.append("<h2>Price levels by window</h2>")
    wins = [w for w in a.windows if w.source != "serpapi"]
    if not wins:
        parts.append('<div class="card muted">No data yet.</div>')
    for w in wins:
        cells = [c for c in a.cells if (c.source, c.route, c.window) == (w.source, w.route, w.window)]
        cfgw = next((x for x in cfg.windows if x.name == w.window), None)
        span = f"{fdate(cfgw.depart_from.isoformat())} – {fdate(cfgw.depart_to.isoformat())} {cfgw.depart_to.year}" if cfgw else ""
        ser = a.series.get((w.source, w.route, w.window), [])
        trend = trend_chart(ser)
        parts.append(
            f'<div class="card"><h3>{e(w.window)} <span class="muted">· {e(w.route)} · departures {e(span)} · {e(w.source)}</span></h3>'
            f'<div class="stats"><span>median <b>{money(w.median)}</b> pp</span><span>10th pct <b>{money(w.p10)}</b></span>'
            f'<span>cheapest <b>{money(w.minimum)}</b> ({fdate(w.best.depart)} → {fdate(w.best.ret)})</span>'
            f'<span>{w.n} trips · {w.outliers} flagged</span></div>'
            f'<div class="legend"><span><i style="background:var(--s1)"></i>cheapest trip per departure date</span>'
            f'<span><i style="background:var(--s2)"></i>cheap outlier</span>'
            f'<span><i class="ln" style="background:var(--median)"></i>median of trips departing within ±{st["neighbour_days"]} days</span></div>'
            f'{strip_chart(cells, int(st["neighbour_days"]), pax)}'
            + (f'<div class="legend" style="margin-top:14px"><span><i class="ln" style="background:var(--s1)"></i>cheapest trip</span>'
               f'<span><i class="ln" style="background:var(--median)"></i>median trip, by collection day</span></div>{trend}'
               if len(ser) > 1 else '<p class="muted" style="margin:12px 0 0;font-size:13px">The trend chart appears after the second collection day.</p>')
            + "</div>")

    # deals
    if a.deals:
        items = []
        for d in a.deals:
            title = (f'<a href="{e(d["url"])}" rel="noopener noreferrer">{e(d["title"])}</a>'
                     if str(d["url"]).startswith(("http://", "https://")) else e(d["title"]))
            items.append(f'<li>{title}<br><span class="muted">{e(d["feed"])} · {e((d["published"] or d["first_seen"])[:16])}'
                         + (f' · {e(d["summary"][:160])}…' if d["summary"] else "") + "</span></li>")
        parts.append(f'<h2>Deal feeds</h2><div class="card"><ul class="deals">{"".join(items)}</ul></div>')

    # full table (learning phase, or forced) + run log
    mode = str(cfg.output.get("full_table", "auto"))
    show_full = mode == "always" or (mode == "auto" and a.phase == "learning")
    if show_full:
        order = {w.name: i for i, w in enumerate(cfg.windows)}
        allc = sorted(a.cells, key=lambda c: (c.source, c.route, order.get(c.window, 99), c.depart, c.ret))
        rows = "".join(
            f"<tr{'' if c.current else ' class=muted'}><td>{_trip(c)}</td><td>{e(c.window)}</td><td class='n'>{money(c.price)}</td><td class='n'>{money(c.price * pax)}</td>"
            f"<td class='n'>{money(c.base)}</td><td class='n'>{vs_baseline(c)}</td>"
            f"<td>{e(c.source)}<br><span class='muted'>{e(seen_text(c, a.now))}</span></td><td>{status_html(c, a.now)}</td>"
            f"<td>{'' if c.stops is None else c.stops}</td><td>{e(', '.join(c.flags))}</td></tr>" for c in allc)
        parts.append(f'<h2>All trips</h2><details class="card" open><summary>{len(allc)} trips, by window and departure date'
                     f' ({len(allc) - stale - dead} current, {stale} stale, {dead} gone)</summary>'
                     f'<div class="scroll"><table><thead><tr><th>Trip</th><th>Window</th><th class="n">Per person</th><th class="n">For {pax}</th>'
                     f'<th class="n">Neighbour median</th><th class="n">vs. it</th><th>Source</th><th>Availability</th><th>Stops</th><th>Flags</th></tr></thead>'
                     f'<tbody>{rows}</tbody></table></div></details>')
    runs = "".join(
        f"<tr><td>{e(r['started_at'][:16].replace('T', ' '))}</td><td>{e(r['source'])}</td>"
        f"<td class='{'ok' if r['status'] == 'ok' else 'bad'}'>{e(r['status'])}</td><td class='n'>{r['requests']}</td>"
        f"<td class='n'>{r['offers']}</td><td>{e(r['error'] or '')}</td></tr>" for r in a.runs)
    parts.append(f'<details class="card"><summary>Run log (last {len(a.runs)})</summary><div class="scroll"><table><thead><tr>'
                 f'<th>Started (UTC)</th><th>Source</th><th>Status</th><th class="n">Requests</th><th class="n">Rows</th><th>Error</th></tr></thead>'
                 f'<tbody>{runs}</tbody></table></div></details>')
    parts.append('<p class="muted" style="font-size:12.5px">Prices are per person, round trip, in ' + e(cfg.currency) + '. Cached sources (Travelpayouts) show what '
                 'travellers recently searched, not a bookable quote: always confirm on Google Flights or the airline before buying. '
                 'The chart y-axis does not start at zero.</p>')

    robots = '<meta name="robots" content="noindex,nofollow">' if cfg.output.get("noindex", True) else ""
    doc = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           f'{robots}<title>{e(cfg.output["title"])}</title><style>{CSS}</style></head><body><main>'
           f'<h1>{e(cfg.output["title"])}</h1><p class="sub">{pax} travellers · {cfg.min_days}–{cfg.max_days}-day trips · '
           f'updated {local_now:%a %d %b %Y, %H:%M} ({e(str(cfg.tz))}) · {len(a.current)} current trips'
           + (f' ({stale} stale, {dead} gone)' if stale or dead else '') + f', {max_days_data} collection days</p>'
           + "".join(parts) + "</main></body></html>")

    summary = {
        "generated_at": a.now.astimezone(dt.timezone.utc).isoformat(),
        "currency": cfg.currency, "pax": pax, "collection_days": max_days_data, "trips_tracked": len(a.cells),
        "trips_current": len(a.current), "trips_stale": stale, "trips_gone": dead,
        "synthetic": "mock" in a.sources_seen,
        "phase": a.phase, "learning_days": int(st["learning_days"]), "calibration": a.calibration,
        "thresholds": {"z": st["z_threshold"], "min_drop_pct": st["min_drop_pct"], "urgent_pct": st["urgent_pct"]},
        "all_trips": [_cell_json(cfg, c) for c in sorted(a.cells, key=lambda c: (c.window, c.depart, c.ret))] if show_full else None,
        "report_url": (cfg.output.get("public_url") or "").rstrip("/") + "/index.html" if cfg.output.get("public_url") else None,
        "best": _cell_json(cfg, best) if best else None,
        "golden_best": _cell_json(cfg, gold) if gold else None,
        "golden_windows": [w.name for w in cfg.windows if w.priority == 1],
        "outliers": [_cell_json(cfg, c) for c in outs],
        "windows": [{"source": w.source, "route": w.route, "window": w.window, "n": w.n, "median_pp": round(w.median),
                     "p10_pp": round(w.p10), "min_pp": round(w.minimum), "outliers": w.outliers} for w in a.windows],
        "new_deals": [{"title": d["title"], "url": d["url"], "feed": d["feed"]} for d in a.deals[:10]],
        "last_runs": [{"source": r["source"], "status": r["status"], "started_at": r["started_at"], "error": r["error"]} for r in a.runs],
    }
    return doc, summary


def _cell_json(cfg: Config, c: Cell) -> dict:
    return {"route": c.route, "depart": c.depart, "return": c.ret, "days": c.days, "window": c.window,
            "price_pp": round(c.price), "price_total": round(c.price * cfg.pax), "seats_quoted": seats_quoted(c),
            "priority": c.priority, "source": c.source, "stops": c.stops,
            "airline": c.airline, "flags": c.flags, "urgent": c.urgent, "below_baseline_pct": round(c.score, 1),
            "price_seen_at": c.price_seen_at, "stale": c.stale, "gone": c.dead, "confirmed": c.confirmed,
            "live_check": c.check, "live_price_pp": round(c.check_price) if c.check_price is not None else None}


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def write(cfg: Config, a: Analysis, out_dir: Path | None = None) -> Path:
    out = out_dir or cfg.out_dir
    (out / "archive").mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o755)
    doc, summary = render(cfg, a)
    _atomic_write(out / "index.html", doc)
    _atomic_write(out / "summary.json", json.dumps(summary, indent=2, ensure_ascii=False))
    _atomic_write(out / "archive" / f"{a.today.isoformat()}.html", doc)
    keep = int(cfg.output.get("keep_archive_days", 30))
    for f in sorted((out / "archive").glob("*.html"))[:-keep] if keep > 0 else []:
        f.unlink()
    return out / "index.html"
