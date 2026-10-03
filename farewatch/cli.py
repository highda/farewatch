"""farewatch CLI. Scheduling and notifications live outside (n8n / cron); this is collect + report (+ an HTTP trigger)."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
from pathlib import Path

from . import db, notify, pipeline, report, stats
from .config import Config, load
from .http import Http, HttpError
from .sources import serpapi, travelpayouts
from .sources.base import Context, SourceError
from .verify import remaining_budget

HERE = Path(__file__).resolve().parent.parent


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _load(args) -> Config:
    p = Path(args.config)
    if not p.is_file():
        sys.exit(f"config not found: {p}  (run `python -m farewatch init` to create one)")
    return load(p)


def cmd_init(args) -> int:
    dest = Path(args.config)
    example = HERE / "config.example.toml"
    if not dest.exists():
        shutil.copy(example, dest)
        _log(f"wrote {dest}")
    cfg = load(dest)
    db.connect(cfg.db_path).close()
    _log(f"database: {cfg.db_path}\noutput:   {cfg.out_dir}")
    return 0


def _run_report(cfg: Config, conn, now, out: Path | None) -> Path:
    a = stats.analyze(conn, cfg, now)
    return report.write(cfg, a, out)


def _exit_code(results: list[dict]) -> int:
    """2 when no price source succeeded (feeds alone don't count as data), so the scheduler can alert."""
    prices = [r for r in results if r["source"] != "feeds"]
    return 2 if prices and all(r["status"] == "error" for r in prices) else 0


def cmd_collect(args) -> int:
    cfg = _load(args)
    conn = db.connect(cfg.db_path)
    results = pipeline.collect(cfg, conn, only=set(args.source) if args.source else None)
    print(json.dumps({"results": results}))
    return _exit_code(results)


def cmd_report(args) -> int:
    cfg = _load(args)
    conn = db.connect(cfg.db_path)
    path = _run_report(cfg, conn, dt.datetime.now(dt.timezone.utc), Path(args.out) if args.out else None)
    print(json.dumps({"report": str(path)}))
    return 0


def _collect_and_report(cfg: Config, out: Path | None = None) -> tuple[dict, int]:
    conn = db.connect(cfg.db_path)
    try:
        results = pipeline.collect(cfg, conn)
        now = dt.datetime.now(dt.timezone.utc)
        path = _run_report(cfg, conn, now, out)
        a = stats.analyze(conn, cfg, now)
        note = notify.build(cfg, a, conn)
    finally:
        conn.close()
    return ({"results": results, "report": str(path), "outliers": len(a.outliers),
             "summary": str(path.parent / "summary.json"), "notification": note}, _exit_code(results))


def cmd_run(args) -> int:
    res, code = _collect_and_report(_load(args), Path(args.out) if args.out else None)
    print(json.dumps(res))
    return code


def cmd_serve(args) -> int:
    """HTTP trigger for n8n: POST /run = collect + report, returns the fresh summary.json. GET /summary, GET /health.
    Config is re-read on every run, so edits to config.toml apply without a restart."""
    import hmac
    import os
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    _load(args)                                  # fail fast on a bad config
    token = os.environ.get("FAREWATCH_RUN_TOKEN") or None
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorised(self) -> bool:
            if token and not hmac.compare_digest(self.headers.get("X-Farewatch-Token", ""), token):
                self._send(401, {"error": "bad or missing X-Farewatch-Token"})
                return False
            return True

        def _summary(self, cfg: Config) -> dict | None:
            p = cfg.out_dir / "summary.json"
            return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None

        def do_GET(self) -> None:
            if self.path == "/health":
                return self._send(200, {"ok": True, "running": lock.locked()})
            if self.path == "/summary":
                if self._authorised():
                    s = self._summary(_load(args))
                    self._send(200, s) if s else self._send(404, {"error": "no summary.json yet"})
                return
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/run":
                return self._send(404, {"error": "not found"})
            if not self._authorised():
                return
            if not lock.acquire(blocking=False):
                return self._send(409, {"error": "a run is already in progress"})
            try:
                cfg = _load(args)
                res, code = _collect_and_report(cfg)
                _log(f"run finished: exit {code}, {res['outliers']} outlier(s)")
                self._send(200, {"ok": code == 0, "exit_code": code, "notification": res.pop("notification"),
                                 "run": res, "summary": self._summary(cfg)})
            except Exception as e:                # keep serving; n8n sees the error
                _log(f"run failed: {e!r}")
                self._send(500, {"ok": False, "error": repr(e)})
            finally:
                lock.release()

        def log_message(self, fmt: str, *a) -> None:
            _log(f"{self.address_string()} {fmt % a}")

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    _log(f"farewatch serve on {args.host}:{args.port} (token {'required' if token else 'not set'})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_status(args) -> int:
    cfg = _load(args)
    conn = db.connect(cfg.db_path)
    for r in conn.execute("SELECT source, COUNT(*) n, COUNT(DISTINCT obs_day) days, MIN(obs_day) a, MAX(obs_day) b FROM offers GROUP BY source"):
        print(f"{r['source']:14} {r['n']:>7} rows  {r['days']:>3} days  {r['a']} .. {r['b']}")
    print(f"{'deals':14} {conn.execute('SELECT COUNT(*) FROM deals').fetchone()[0]:>7} rows")
    for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 8"):
        print(f"  run {r['id']:>4} {r['started_at']} {r['source']:14} {r['status']:8} req={r['requests']} rows={r['offers']} {r['error'] or ''}")
    return 0


def cmd_probe(args) -> int:
    """One real request, raw JSON to stdout: use it on the server to verify a source before trusting the parser."""
    cfg = _load(args)
    http = Http(cfg.user_agent)
    conn = db.connect(":memory:")
    ctx = Context(cfg, conn, http, dt.datetime.now(dt.timezone.utc), _log)
    route = next((r for r in cfg.active_routes if r.name == args.route), None) or cfg.active_routes[0]
    try:
        if args.source == "travelpayouts":
            scfg = cfg.source("travelpayouts")
            token = cfg.secret(scfg.get("token_env", "TRAVELPAYOUTS_TOKEN")) or sys.exit("TRAVELPAYOUTS_TOKEN not set")
            src = travelpayouts.TravelpayoutsSource(scfg)
            url, params = src.request_params(args.endpoint, route, args.month, cfg.currency)
            payload = http.get_json(url, params, {"X-Access-Token": token})
            rows = payload.get("data") if isinstance(payload, dict) else None
            parsed = travelpayouts.parse_rows(payload, route, cfg.currency)
            _log(f"{len(rows) if isinstance(rows, list) else '?'} raw rows, {len(parsed)} round-trip rows parsed")
        else:
            scfg = cfg.source("serpapi")
            key = cfg.secret(scfg.get("api_key_env", "SERPAPI_KEY")) or sys.exit("SERPAPI_KEY not set")
            dep, ret = args.dates.split(":")
            payload = http.get_json(serpapi.URL, serpapi.search_params(
                key, route, dep, ret, cfg.pax, cfg.max_stops, scfg.get("arrival_ids", {}), cfg.currency,
                args.gl or str(scfg.get("gl", "cz"))))
            payload.pop("search_parameters", None)
            parsed = serpapi.parse_response(payload, route, dep, ret, cfg.pax, cfg.currency)
            _log(f"parsed {len(parsed)} offer(s): {[(o.price_pp, o.stops, o.airline) for o in parsed]}")
    except HttpError as e:
        sys.exit(str(e))
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    print(text[: args.max_chars] + ("\n... (truncated)" if len(text) > args.max_chars else ""))
    return 0


def cmd_gl_check(args) -> int:
    """Does the point-of-sale country change the price? Prices the anchors once per `gl` and prints a table.
    Costs len(countries) x len(anchors) live searches, taken from the SerpApi budget (logged as a `manual` run)."""
    cfg = _load(args)
    scfg = cfg.source("serpapi")
    key = cfg.secret(scfg.get("api_key_env", "SERPAPI_KEY")) or sys.exit("SERPAPI_KEY not set")
    countries = [c.strip().lower() for c in args.countries.split(",") if c.strip()]
    pairs = [a.split(":") for a in (args.dates or scfg.get("anchors", []))]
    if not pairs:
        sys.exit("no anchors in [sources.serpapi] and no --dates given")
    route = next((r for r in cfg.active_routes if r.name == args.route), None) or cfg.active_routes[0]
    need = len(countries) * len(pairs)
    now = dt.datetime.now(dt.timezone.utc)
    conn = db.connect(cfg.db_path)
    http = Http(cfg.user_agent)
    ctx = Context(cfg, conn, http, now, _log)
    left = remaining_budget(ctx, scfg)
    if need > left and not args.force:
        sys.exit(f"needs {need} searches, only {left} left in the budget today/this month (use --force to override)")
    run_id = db.start_run(conn, "serpapi", "verify", now)
    prices: dict[str, dict[str, float | None]] = {}
    errors = 0
    try:
        for gl in countries:
            for dep, ret in pairs:
                try:
                    payload = http.get_json(serpapi.URL, serpapi.search_params(
                        key, route, dep, ret, cfg.pax, cfg.max_stops, scfg.get("arrival_ids", {}), cfg.currency, gl),
                        min_delay=float(scfg.get("min_delay_s", 3.0)), retry_429=False)
                    found = serpapi.parse_response(payload, route, dep, ret, cfg.pax, cfg.currency)
                    prices.setdefault(f"{dep}:{ret}", {})[gl] = found[0].price_pp if found else None
                except (HttpError, SourceError) as e:
                    errors += 1
                    _log(f"gl={gl} {dep}->{ret}: {e}")
                    prices.setdefault(f"{dep}:{ret}", {})[gl] = None
    finally:
        db.finish_run(conn, run_id, dt.datetime.now(dt.timezone.utc), status="manual", requests=http.requests, offers=0,
                      error=f"gl-check {','.join(countries)}" + (f", {errors} errors" if errors else ""))
    base = countries[0]
    print(f"{route.name}, {cfg.pax} pax, per person in {cfg.currency}; % = vs gl={base}")
    print("trip".ljust(23) + "".join(g.rjust(18) for g in countries))
    for trip, row in prices.items():
        ref = row.get(base)
        cells = []
        for g in countries:
            p = row.get(g)
            pct = f" ({(p - ref) / ref * 100:+.1f}%)" if p is not None and ref and g != base else ""
            cells.append((f"{p:,.0f}{pct}" if p is not None else "–").rjust(18))
        print(trip.ljust(23) + "".join(cells))
    print(f"{http.requests} searches used; {left - http.requests} left in the budget")
    return 1 if errors else 0


def cmd_doctor(args) -> int:
    """Preflight: config sanity, secrets, output folder. Run it after editing config.toml / .env."""
    cfg = _load(args)
    bad = 0

    def line(level: str, msg: str) -> None:
        nonlocal bad
        bad += level == "FAIL"
        print(f"[{level:4}] {msg}")

    if sys.version_info < (3, 11):
        line("FAIL", f"Python {sys.version.split()[0]}: need >= 3.11")
    else:
        line("ok", f"Python {sys.version.split()[0]}, timezone {cfg.tz}")
    line("ok" if cfg.active_routes else "FAIL", f"routes: {', '.join(r.name for r in cfg.active_routes) or 'none enabled'}")
    today = dt.datetime.now(cfg.tz).date()
    live_windows = [w for w in cfg.windows if w.depart_to > today]
    line("ok" if live_windows else "FAIL", f"windows: {len(live_windows)} still ahead of today ({len(cfg.windows)} configured)")
    if "example.com" in cfg.user_agent:
        line("WARN", "general.user_agent still has the placeholder contact address")
    for name, key_env, default in (("travelpayouts", "token_env", "TRAVELPAYOUTS_TOKEN"), ("serpapi", "api_key_env", "SERPAPI_KEY")):
        sc = cfg.source(name)
        if not sc.get("enabled"):
            print(f"[skip] {name}: disabled")
        elif cfg.secret(sc.get(key_env, default)):
            line("ok", f"{name}: {sc.get(key_env, default)} is set")
        else:
            line("FAIL", f"{name}: enabled but {sc.get(key_env, default)} is not set (env or .env next to config.toml)")
    sp = cfg.source("serpapi")
    if sp.get("enabled"):
        bk = sp.get("api_key_backup_env", "SERPAPI_KEY_BACKUP")
        print(f"[ok  ] serpapi: backup key {bk} is set" if cfg.secret(bk) else f"[info] serpapi: no backup key ({bk} not set)")
    if cfg.source("mock").get("enabled"):
        line("WARN", "sources.mock is enabled: SYNTHETIC prices will be mixed into real data")
    tp = cfg.source("travelpayouts")
    if tp.get("enabled"):
        from .plan import departure_months
        n = len(cfg.active_routes) * len(departure_months(cfg)) * len(tp.get("endpoints", ["latest", "month_matrix"]))
        line("ok", f"travelpayouts: ~{n} requests per run")
    if sp.get("enabled"):
        anchors, k = len(sp.get("anchors", [])), int(sp.get("verify_top_k", 2))
        per_day = anchors + 2 * k                       # anchors once a day, up to k candidates per run, 2 runs a day
        monthly = int(sp.get("monthly_budget", 90))
        line("ok" if per_day * 31 <= monthly else "WARN",
             f"serpapi: up to {per_day} searches/day with 2 runs ({anchors} anchors once a day + {k} candidates/run) "
             f"= {per_day * 31}/month vs monthly_budget {monthly}; gl={sp.get('gl', 'cz')}")
    line("ok", f"feeds: {len(cfg.feeds)} enabled")
    out = cfg.out_dir
    try:
        out.mkdir(parents=True, exist_ok=True)
        probe = out / ".write-test"
        probe.write_text("x")
        probe.unlink()
        line("ok", f"output dir writable: {out}")
    except OSError as e:
        line("FAIL", f"output dir not writable: {out} ({e})")
    # in the container the web folder is bind-mounted at /out, so judge the public name from public_url when set
    public = (cfg.output.get("public_url") or "").rstrip("/").rsplit("/", 1)[-1] or out.name
    if public and len(public) < 16 and public not in ("out", "."):
        line("WARN", f"output folder name '{public}' is short/guessable; use a random suffix (e.g. flights-$(openssl rand -hex 8))")
    if public == "out":
        line("WARN", "output dir is the default 'out'; set [output] dir to the web folder with a random name")
    if cfg.db_path.parent == out or out in cfg.db_path.parents:
        line("FAIL", "the database is inside the output folder (it would be public)")
    if cfg.output.get("public_url"):
        print(f"       public URL: {cfg.output['public_url']}")
    return 1 if bad else 0


def cmd_demo(args) -> int:
    """Backfill N days of SYNTHETIC prices into a separate database and render a demo report."""
    base = _load(args)
    demo_dir = base.path(args.dir)
    cfg = base.with_overrides({
        "general": {"data_dir": str(demo_dir)},
        "output": {"dir": str(demo_dir / "out"), "title": base.output["title"] + " (demo)"},
        "sources": {"mock": {"enabled": True}, "travelpayouts": {"enabled": False}, "serpapi": {"enabled": False}},
        "feeds": [],
    })
    if cfg.db_path.exists():
        cfg.db_path.unlink()
    conn = db.connect(cfg.db_path)
    end = dt.datetime.now(dt.timezone.utc).replace(hour=6, minute=0, second=0, microsecond=0)
    for i in range(args.days, -1, -1):
        pipeline.collect(cfg, conn, now=end - dt.timedelta(days=i), log=lambda m: None)
    path = _run_report(cfg, conn, end, None)
    print(json.dumps({"report": str(path)}))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="farewatch", description=__doc__)
    ap.add_argument("--config", default="config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="write config.toml from the example + create the database").set_defaults(fn=cmd_init)
    p = sub.add_parser("collect", help="run enabled sources and store observations")
    p.add_argument("--source", action="append", help="limit to a source (repeatable): travelpayouts|serpapi|mock|feeds")
    p.set_defaults(fn=cmd_collect)
    p = sub.add_parser("report", help="analyse stored data and write index.html + summary.json")
    p.add_argument("--out", help="override output directory")
    p.set_defaults(fn=cmd_report)
    p = sub.add_parser("run", help="collect + report (what the scheduler calls)")
    p.add_argument("--out", help="override output directory")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("serve", help="HTTP trigger for n8n: POST /run, GET /summary, GET /health")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    p.set_defaults(fn=cmd_serve)
    sub.add_parser("status", help="what is in the database").set_defaults(fn=cmd_status)
    sub.add_parser("doctor", help="preflight check of config, secrets and output folder").set_defaults(fn=cmd_doctor)
    p = sub.add_parser("probe", help="one live request, print raw JSON (verify a source works)")
    p.add_argument("source", choices=["travelpayouts", "serpapi"])
    p.add_argument("--route")
    p.add_argument("--endpoint", default="latest", choices=["latest", "month_matrix"])
    p.add_argument("--month", default="2027-03")
    p.add_argument("--dates", default="2027-03-25:2027-04-08")
    p.add_argument("--gl", help="serpapi: point-of-sale country (default: [sources.serpapi] gl, or cz)")
    p.add_argument("--max-chars", type=int, default=4000)
    p.set_defaults(fn=cmd_probe)
    p = sub.add_parser("gl-check", help="serpapi: price the anchors from several countries (gl) and compare; spends budget")
    p.add_argument("--countries", default="cz,pl,in,tr", help="comma-separated gl codes; the first is the reference")
    p.add_argument("--dates", action="append", help="DEP:RET (repeatable); default: the configured anchors")
    p.add_argument("--route")
    p.add_argument("--force", action="store_true", help="ignore the daily/monthly budget check")
    p.set_defaults(fn=cmd_gl_check)
    p = sub.add_parser("demo", help="synthetic data demo (separate DB + output)")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--dir", default="data/demo")
    p.set_defaults(fn=cmd_demo)
    args = ap.parse_args(argv)
    return args.fn(args)
