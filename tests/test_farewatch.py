import datetime as dt
import tempfile
import unittest
from pathlib import Path

from farewatch import db, notify, pipeline, plan, report, stats
from farewatch.config import DEFAULTS, Config
from farewatch.sources import feeds
from farewatch.sources.base import Offer, SourceError
from farewatch.sources.serpapi import parse_response
from farewatch.sources.travelpayouts import parse_rows

RAW = {
    "general": {"timezone": "UTC"},
    "routes": [{"name": "PRG-TYO", "origin": "PRG", "dest": "TYO"}],
    "windows": [{"name": "w", "depart_from": "2027-03-18", "depart_to": "2027-04-05"}],
}


def cfg(extra=None):
    raw = {**RAW, **(extra or {})}
    return Config(raw, Path("."))


NOW = dt.datetime(2026, 10, 1, 6, 0, tzinfo=dt.timezone.utc)


class SerpApiFailover(unittest.TestCase):
    def _run(self, responses, env):
        from types import SimpleNamespace
        from farewatch.http import HttpError
        from farewatch.sources.serpapi import SerpApiSource
        c = cfg({"sources": {"serpapi": {"enabled": True}}})
        c.secret = lambda name: env.get(name)
        used = []

        class H:
            def get_json(self, url, params, min_delay=None, retry_429=True):
                used.append(params["api_key"])
                r = responses[params["api_key"]]
                if isinstance(r, Exception):
                    raise r
                return r
        conn = db.connect(":memory:")
        ctx = SimpleNamespace(cfg=c, http=H(), errors=0, log=lambda m: None, conn=conn, now=NOW, run_id=None)
        ok = {"best_flights": [{"price": 40000, "flights": [{"airline": "X"}], "layovers": []}]}
        responses = {k: (ok if v == "ok" else v) for k, v in responses.items()}
        out = SerpApiSource(c.source("serpapi")).collect(ctx, [(c.routes[0], "2027-03-20", "2027-04-03")] * 2)
        self.checks = [tuple(r) for r in conn.execute("SELECT status, price_pp_czk FROM checks ORDER BY id")]
        return out, used, ctx.errors, HttpError

    def test_switches_to_backup_when_primary_exhausted(self):
        from farewatch.http import HttpError
        out, used, errs, _ = self._run({"A": HttpError(429, "u"), "B": "ok"}, {"SERPAPI_KEY": "A", "SERPAPI_KEY_BACKUP": "B"})
        self.assertEqual(used, ["A", "B", "B"])         # primary tried once, then the backup for the rest of the run
        self.assertEqual((len(out), errs), (2, 0))
        self.assertEqual(self.checks, [("ok", 20000.0), ("ok", 20000.0)])   # per person, 2 pax; every answer is a check row

    def test_no_backup_counts_errors(self):
        from farewatch.http import HttpError
        with self.assertRaises(SourceError):
            self._run({"A": HttpError(429, "u")}, {"SERPAPI_KEY": "A"})

    def test_no_results_is_a_none_check(self):
        out, _, errs, _ = self._run({"A": {"error": "Google Flights hasn't returned any results for this query."}}, {"SERPAPI_KEY": "A"})
        self.assertEqual((out, errs), ([], 0))
        self.assertEqual(self.checks, [("none", None), ("none", None)])

    def test_gl_hl_currency_pinned(self):
        from farewatch.sources.serpapi import search_params
        p = search_params("k", cfg().routes[0], "2027-03-20", "2027-04-03", 2, 1, {}, "czk", "PL")
        self.assertEqual((p["gl"], p["hl"], p["currency"], p["arrival_id"]), ("pl", "en", "CZK", "HND,NRT"))
        self.assertEqual(search_params("k", cfg().routes[0], "a", "b", 1, 1, {})["gl"], "cz")


class ManualRuns(unittest.TestCase):
    def test_manual_budget_row_does_not_date_the_learning_phase(self):
        from farewatch import db
        conn = db.connect(Path(":memory:")) if hasattr(db, "connect") else None
        self.assertIsNotNone(conn)
        conn.execute("INSERT INTO runs(started_at, source, kind, requests, status) VALUES ('2026-10-01T00:00:00Z','serpapi','verify',14,'manual')")
        self.assertEqual(db.requests_this_month(conn, "serpapi", NOW), 14)          # counts toward the budget...
        first = conn.execute("SELECT MIN(started_at) FROM runs WHERE status != 'manual'").fetchone()[0]
        self.assertIsNone(first)                                                    # ...but is not "day 1"


class Currency(unittest.TestCase):
    def test_home_currency_and_formatting(self):
        from farewatch import report
        c = cfg({"general": {"timezone": "UTC", "currency": "EUR"}, "fx": {"CZK": 0.04}})
        self.assertEqual(c.to_home(100, "eur"), 100.0)
        self.assertAlmostEqual(c.to_home(1000, "CZK"), 40.0)
        self.assertIsNone(c.to_home(5, "USD"))                       # no rate configured
        report.set_currency(c.currency)
        try:
            self.assertEqual(report.money(1234.4), "1\u00a0234\u00a0€")
        finally:
            report.set_currency("CZK")
        self.assertEqual(c.routes[0].penalty_pp, 0)

    def test_old_penalty_key_still_works(self):
        c = cfg({"routes": [{"name": "A-B", "origin": "A", "dest": "B", "penalty_czk_pp": 700}]})
        self.assertEqual(c.routes[0].penalty_pp, 700)


class Plan(unittest.TestCase):
    def test_scope_and_cells(self):
        c = cfg()
        self.assertTrue(plan.in_scope(c, "2027-03-20", "2027-04-03"))       # 14 d, in window
        self.assertFalse(plan.in_scope(c, "2027-03-20", "2027-03-25"))      # too short
        self.assertFalse(plan.in_scope(c, "2027-05-20", "2027-06-03"))      # outside window
        n = sum(1 for _ in plan.cells(c, dt.date(2026, 10, 1)))
        self.assertEqual(n, 19 * 5)
        self.assertEqual(plan.departure_months(c), ["2027-03", "2027-04"])

    def test_overlap_window_wins_over_departure_window(self):
        c = cfg({"windows": [
            {"name": "early", "depart_from": "2027-01-16", "depart_to": "2027-02-28",
             "overlap_from": "2027-02-01", "overlap_to": "2027-02-28"},
            {"name": "winter", "depart_from": "2027-01-06", "depart_to": "2027-02-28"}]})
        win = lambda d, r: plan.window_for(c, dt.date.fromisoformat(d), dt.date.fromisoformat(r)).name
        self.assertEqual(win("2027-01-20", "2027-02-02"), "early")     # returns inside February
        self.assertEqual(win("2027-02-27", "2027-03-11"), "early")     # departs on the last day of February
        self.assertEqual(win("2027-01-16", "2027-01-30"), "winter")    # back before February
        self.assertEqual(win("2027-01-08", "2027-01-22"), "winter")
        cells = list(plan.cells(c, dt.date(2026, 10, 1)))
        self.assertEqual(len(cells), len(set(cells)))                  # overlapping windows don't double-scan


class Parsers(unittest.TestCase):
    def test_travelpayouts(self):
        route = cfg().routes[0]
        payload = {"success": True, "currency": "czk", "data": [
            {"depart_date": "2027-03-25", "return_date": "2027-04-08", "value": 21990, "number_of_changes": 1,
             "found_at": "2026-09-30T10:00:00", "actual": True},
            {"depart_date": "2027-03-26", "return_date": "", "value": 9000},                    # one-way
            {"depart_date": "2027-03-27", "return_date": "2027-04-10", "value": 1, "actual": False},  # expired
            {"nonsense": True},
        ]}
        offers = parse_rows(payload, route)
        self.assertEqual(len(offers), 1)
        self.assertEqual((offers[0].price_pp, offers[0].currency, offers[0].stops), (21990.0, "CZK", 1))

    def test_serpapi(self):
        route = cfg().routes[0]
        payload = {"best_flights": [{"price": 24000, "total_duration": 900, "layovers": [{}],
                                     "flights": [{"airline": "Finnair"}, {"airline": "Finnair"}]}],
                   "other_flights": [{"price": 26100, "flights": []}],
                   "price_insights": {"lowest_price": 24000, "price_level": "low", "typical_price_range": [25000, 32000]}}
        o = parse_response(payload, route, "2027-03-25", "2027-04-08")[0]
        self.assertEqual((o.price_pp, o.stops, o.airline), (24000.0, 1, "Finnair"))
        self.assertEqual(o.extra["insights"]["price_level"], "low")
        self.assertEqual(parse_response({"error": "Google Flights hasn't returned any results for this query."}, route, "a", "b"), [])
        with self.assertRaises(SourceError):
            parse_response({"error": "Invalid API key"}, route, "a", "b")
        # priced for 2 seats: per-person = total / 2, insights scaled too
        o2 = parse_response({"best_flights": [{"price": 59870, "layovers": [{}], "flights": []}],
                             "price_insights": {"typical_price_range": [47500, 78000]}}, route, "a", "b", pax=2)[0]
        self.assertEqual((o2.price_pp, o2.extra["seats_priced"], o2.extra["total"]), (29935.0, 2, 59870.0))
        self.assertEqual(o2.extra["insights"]["typical_price_range"], [23750, 39000])

    def test_feeds(self):
        rss = b"""<?xml version="1.0"?><rss version="2.0"><channel>
          <item><title>Levn\xc3\xa9 letenky do Tokia za 15 990 K\xc4\x8d</title><link>https://x.test/1</link>
          <description><![CDATA[Z Prahy do <b>Tokia</b>]]></description><pubDate>2026-09-29 10:00:00</pubDate></item>
          <item><title>Florencie za 1490</title><link>https://x.test/2</link></item></channel></rss>"""
        items = feeds.parse_feed(rss)
        self.assertEqual(len(items), 2)
        hits = [i for i in items if feeds.matches(i, ["toki", "japon"])]
        self.assertEqual([i["url"] for i in hits], ["https://x.test/1"])
        atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>t3_a</id><title>[PRG] to Tokyo</title>
          <link href="https://r.test/a"/><updated>2026-09-29T10:00:00+00:00</updated><content type="html">&lt;p&gt;deal&lt;/p&gt;</content></entry></feed>"""
        self.assertEqual(feeds.parse_feed(atom)[0]["summary"], "deal")


class Stats(unittest.TestCase):
    def _store(self, conn, prices: dict, day: dt.date, source="travelpayouts"):
        offers = [Offer(source, "PRG-TYO", "PRG", "TYO", dep, ret, p, price_pp_czk=p, trip_days=14, stops=1)
                  for (dep, ret), p in prices.items()]
        run = db.start_run(conn, source, "discovery", NOW)
        db.insert_offers(conn, run, offers, NOW, day)

    def test_neighbour_outlier_and_history(self):
        c, conn = cfg(), db.connect(":memory:")
        start = dt.date(2027, 3, 18)
        cells = [((start + dt.timedelta(days=i)).isoformat(), (start + dt.timedelta(days=i + 14)).isoformat()) for i in range(19)]
        # 6 earlier days of a steady 25k market, then today with one cell at 15k
        for d in range(6, 0, -1):
            self._store(conn, {k: 25000 + 100 * (i % 3) for i, k in enumerate(cells)}, dt.date(2026, 10, 1) - dt.timedelta(days=d))
        today = {k: 25000 + 100 * (i % 3) for i, k in enumerate(cells)}
        today[cells[9]] = 15000
        self._store(conn, today, dt.date(2026, 10, 1))
        a = stats.analyze(conn, c, NOW)
        out = a.outliers
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].depart, cells[9][0])
        self.assertIn("below neighbours", out[0].flags)
        self.assertIn("below its own history", out[0].flags)
        self.assertGreater(out[0].score, 35)
        self.assertEqual(a.windows[0].n, 19)

    def test_window_priority_and_looser_thresholds(self):
        start = dt.date(2027, 3, 18)
        cells = [((start + dt.timedelta(days=i)).isoformat(), (start + dt.timedelta(days=i + 14)).isoformat()) for i in range(19)]
        prices = {k: 25000 + 100 * (i % 3) for i, k in enumerate(cells)}
        prices[cells[9]] = 22500                       # ~10 % below neighbours: not an outlier at the default 15 %
        base_w = {"name": "w", "depart_from": "2027-03-18", "depart_to": "2027-04-05"}
        conn = db.connect(":memory:")
        self._store(conn, prices, dt.date(2026, 10, 1))
        self.assertEqual(stats.analyze(conn, cfg(), NOW).outliers, [])
        loose = cfg({"windows": [{**base_w, "priority": 1, "min_drop_pct": 8.0, "z_threshold": 1.5}]})
        out = stats.analyze(conn, loose, NOW).outliers
        self.assertEqual([(c.depart, c.priority) for c in out], [(cells[9][0], 1)])

    def test_no_false_positives_on_smooth_seasonal_curve(self):
        c, conn = cfg(), db.connect(":memory:")
        start = dt.date(2027, 3, 18)
        prices = {}
        for i in range(19):    # steady climb into peak: neighbours explain it
            prices[((start + dt.timedelta(days=i)).isoformat(), (start + dt.timedelta(days=i + 14)).isoformat())] = 20000 + 500 * i
        self._store(conn, prices, dt.date(2026, 10, 1))
        self.assertEqual(stats.analyze(conn, c, NOW).outliers, [])


class Availability(unittest.TestCase):
    """Stale cached prices and live checks: what may be flagged, shown in headline boxes and sent to the phone."""
    def setUp(self):
        self.cells = [((dt.date(2027, 3, 18) + dt.timedelta(days=i)).isoformat(),
                       (dt.date(2027, 3, 18) + dt.timedelta(days=i + 14)).isoformat()) for i in range(19)]
        self.c = cfg({"sources": {"serpapi": {"enabled": True, "recheck_days": 3, "verify_tolerance_pct": 10,
                                               "anchors": ["2027-03-20:2027-04-03"], "verify_top_k": 2, "monthly_budget": 100}}})
        self.conn = db.connect(":memory:")
        self.deal = self.cells[9]

    def _store(self, cached_at=None, deal=15000):
        prices = {k: 25000 + 100 * (i % 3) for i, k in enumerate(self.cells)}
        prices[self.deal] = deal
        offers = [Offer("travelpayouts", "PRG-TYO", "PRG", "TYO", dep, ret, p, price_pp_czk=p, trip_days=14, stops=1,
                        cached_at=cached_at) for (dep, ret), p in prices.items()]
        run = db.start_run(self.conn, "travelpayouts", "discovery", NOW)
        db.insert_offers(self.conn, run, offers, NOW, NOW.date())

    def _deal(self, a):
        return next(c for c in a.cells if (c.depart, c.ret) == self.deal)

    def test_unverified_deal_is_flagged_but_not_announced(self):
        self._store(cached_at="2026-09-30T20:00:00")                 # seen 1.4 days ago: fresh enough
        a = stats.analyze(self.conn, self.c, NOW)
        d = self._deal(a)
        self.assertTrue(d.flags and d.current and not d.confirmed and d.check is None)
        self.assertEqual(len(a.outliers), 1)
        self.assertEqual(notify.new_outliers(self.c, a, self.conn), [])            # waits for its live check
        self.assertEqual([(c.depart, c.ret) for c in notify.candidates(self.c, a, self.conn)], [self.deal])
        n = notify.build(self.c, a, self.conn)                                    # digest still goes, quoting it as unverified
        self.assertTrue(n["digest"]); self.assertEqual(n["new_outliers"], 0)
        self.assertIn("unverified", n["nextcloud"]["message"])
        self.assertIsNone(db.notified_get(self.conn, f"trip:PRG-TYO:{self.deal[0]}:{self.deal[1]}"))   # not marked: still pending

    def test_stale_price_is_listed_but_never_flagged(self):
        self._store(cached_at="2026-09-25T10:00:00")                 # seen 6 days ago
        a = stats.analyze(self.conn, self.c, NOW)
        d = self._deal(a)
        self.assertTrue(d.stale and not d.current and not d.flags)
        self.assertEqual(a.outliers, [])
        self.assertEqual(notify.candidates(self.c, a, self.conn), [])             # no live search wasted on it
        self.assertEqual(len(a.cells), 19); self.assertEqual(len(a.current), 0)
        doc, summary = report.render(self.c, a)
        self.assertIn("19 stale", doc)
        self.assertTrue(all(t["stale"] for t in summary["all_trips"]))

    def test_live_check_confirms_then_announces(self):
        self._store(cached_at="2026-09-30T20:00:00")
        db.insert_check(self.conn, None, NOW, NOW.date(), "PRG-TYO", *self.deal, "ok", 15400.0)   # +2.7 %: fine
        a = stats.analyze(self.conn, self.c, NOW)
        d = self._deal(a)
        self.assertTrue(d.confirmed and d.check == "ok" and d.check_price == 15400.0)
        n = notify.build(self.c, a, self.conn)
        self.assertEqual(n["new_outliers"], 1)
        self.assertIn("live-checked 15", n["nextcloud"]["message"])
        self.assertEqual(notify.candidates(self.c, a, self.conn), [])             # nothing left to verify

    def test_live_check_finds_it_gone(self):
        self._store(cached_at="2026-09-30T20:00:00")
        db.insert_check(self.conn, None, NOW, NOW.date(), "PRG-TYO", *self.deal, "none")
        a = stats.analyze(self.conn, self.c, NOW)
        d = self._deal(a)
        self.assertTrue(d.dead and not d.flags and not d.confirmed)
        self.assertEqual(a.outliers, [])
        self.assertFalse(notify.build(self.c, a, self.conn)["new_outliers"])
        # headline boxes skip it: the cheapest *current* trip is a 25k one
        self.assertGreaterEqual(stats.best_of(a.cells).price, 25000)
        doc, _ = report.render(self.c, a)
        self.assertIn("no fare found live", doc)

    def test_live_price_well_above_cached_means_gone(self):
        self._store(cached_at="2026-09-30T20:00:00")
        db.insert_check(self.conn, None, NOW, NOW.date(), "PRG-TYO", *self.deal, "ok", 19500.0)   # +30 %
        d = self._deal(stats.analyze(self.conn, self.c, NOW))
        self.assertTrue(d.dead and d.check == "ok")
        self.assertIn("gone: live 19", report.status_text(d))

    def test_failed_check_keeps_cell_pending_and_does_not_hide_an_earlier_answer(self):
        self._store(cached_at="2026-09-30T20:00:00")
        db.insert_check(self.conn, None, NOW - dt.timedelta(hours=5), NOW.date(), "PRG-TYO", *self.deal, "ok", 15000.0)
        db.insert_check(self.conn, None, NOW, NOW.date(), "PRG-TYO", *self.deal, "error", None, "HTTP 500")
        self.assertTrue(self._deal(stats.analyze(self.conn, self.c, NOW)).confirmed)
        conn2 = db.connect(":memory:"); self.conn = conn2
        self._store(cached_at="2026-09-30T20:00:00")
        db.insert_check(conn2, None, NOW, NOW.date(), "PRG-TYO", *self.deal, "error", None, "HTTP 500")
        d = self._deal(stats.analyze(conn2, self.c, NOW))
        self.assertTrue(d.check == "error" and d.current and not d.confirmed and not d.dead)

    def test_pick_targets_anchors_once_a_day_then_candidates_within_budget(self):
        from types import SimpleNamespace
        from farewatch.verify import pick_targets
        self._store(cached_at="2026-09-30T20:00:00")
        scfg = self.c.source("serpapi")
        ctx = SimpleNamespace(cfg=self.c, conn=self.conn, now=NOW)
        t = [(r.name, d, r_) for r, d, r_ in pick_targets(ctx, scfg)]
        self.assertEqual(t, [("PRG-TYO", "2027-03-20", "2027-04-03"), ("PRG-TYO", *self.deal)])   # anchor + the deal
        db.insert_check(self.conn, None, NOW, NOW.date(), "PRG-TYO", "2027-03-20", "2027-04-03", "ok", 25000.0)
        db.insert_check(self.conn, None, NOW, NOW.date(), "PRG-TYO", *self.deal, "ok", 15000.0)
        later = SimpleNamespace(cfg=self.c, conn=self.conn, now=NOW + dt.timedelta(hours=8))                # the 16:00 run
        self.assertEqual(pick_targets(later, scfg), [])                                                  # anchor done today, deal checked
        # budget: a manual row eats the month
        self.conn.execute("INSERT INTO runs(started_at, source, kind, requests, status) VALUES ('2026-10-01T00:00:00Z','serpapi','verify',100,'manual')")
        self.assertEqual(pick_targets(SimpleNamespace(cfg=self.c, conn=self.conn, now=NOW + dt.timedelta(days=1)), scfg), [])


class Phases(unittest.TestCase):
    def _run_days(self, n, tmp):
        c = cfg({"sources": {"mock": {"enabled": True}}})
        conn = db.connect(Path(tmp) / "p.sqlite")
        for i in range(n - 1, -1, -1):
            pipeline.collect(c, conn, now=NOW - dt.timedelta(days=i), log=lambda m: None)
        return c, conn

    def test_learning_then_watching(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, conn = self._run_days(14, tmp)
            a = stats.analyze(conn, c, NOW)
            self.assertEqual((a.collection_days, a.phase), (14, "learning"))
            doc, summary = report.render(c, a)
            self.assertIn("Learning phase", doc)
            self.assertIn("<h2>All trips</h2>", doc)
            self.assertEqual(len(summary["all_trips"]), len(a.cells))
            self.assertIsNotNone(a.calibration)
            # day 15 flips to outliers-only
            pipeline.collect(c, conn, now=NOW + dt.timedelta(days=1), log=lambda m: None)
            a = stats.analyze(conn, c, NOW + dt.timedelta(days=1))
            self.assertEqual(a.phase, "watching")
            doc, summary = report.render(c, a)
            self.assertNotIn("<h2>All trips</h2>", doc)
            self.assertNotIn("Learning phase", doc)
            self.assertIsNone(summary["all_trips"])

    def test_phase_counts_calendar_days_from_first_run(self):
        # two runs 14 days apart: only 2 days of data, but day 15 since the first run -> watching
        with tempfile.TemporaryDirectory() as tmp:
            c = cfg({"sources": {"mock": {"enabled": True}}})
            conn = db.connect(Path(tmp) / "g.sqlite")
            pipeline.collect(c, conn, now=NOW - dt.timedelta(days=14), log=lambda m: None)
            a = stats.analyze(conn, c, NOW - dt.timedelta(days=1))
            self.assertEqual((a.collection_days, a.phase), (14, "learning"))
            pipeline.collect(c, conn, now=NOW, log=lambda m: None)
            a = stats.analyze(conn, c, NOW)
            self.assertEqual((a.collection_days, a.phase), (15, "watching"))

    def test_calibration_is_monotonic_and_urgent_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            c, conn = self._run_days(10, tmp)
            a = stats.analyze(conn, c, NOW)
            vals = list(a.calibration["avg_flags_per_day"].values())
            self.assertEqual(vals, sorted(vals, reverse=True))
            for cell in a.cells:
                if cell.urgent:
                    self.assertGreaterEqual(cell.score, c.stats["urgent_pct"])


class Pipeline(unittest.TestCase):
    def test_normalise_drops_and_dedupes(self):
        c = cfg()
        base = dict(source="s", route="PRG-TYO", origin="PRG", dest="TYO")
        offers = [
            Offer(**base, depart_date="2027-03-25", return_date="2027-04-08", price_pp=30000, stops=1),
            Offer(**base, depart_date="2027-03-25", return_date="2027-04-08", price_pp=25000, stops=1),   # cheaper wins
            Offer(**base, depart_date="2027-03-25", return_date="2027-04-08", price_pp=10000, stops=3),   # too many stops
            Offer(**base, depart_date="2027-03-25", return_date="2027-04-09", price_pp=500, currency="EUR"),  # no fx
            Offer(**base, depart_date="2027-08-01", return_date="2027-08-15", price_pp=9000),             # out of window
        ]
        out = pipeline.normalise(c, offers, lambda m: None)
        self.assertEqual([(o.price_pp_czk, o.trip_days) for o in out], [(25000.0, 14)])


class Report(unittest.TestCase):
    def test_render_escapes_and_writes(self):
        c = cfg({"sources": {"mock": {"enabled": True}}})
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "t.sqlite")
            pipeline.collect(c, conn, now=NOW, log=lambda m: None)
            conn.execute("INSERT INTO deals(guid,feed,title,url,published,first_seen,summary) VALUES "
                         "('g','f','<script>alert(1)</script>','javascript:alert(1)','','2026-10-01','')")
            conn.commit()
            out = report.write(c, stats.analyze(conn, c, NOW), Path(tmp) / "out")
            text = out.read_text()
            self.assertIn("SYNTHETIC", text)
            self.assertNotIn("<script>alert", text)
            self.assertNotIn('href="javascript:', text)          # non-http links are dropped by the feed parser, and escaped here
            self.assertTrue((out.parent / "summary.json").exists())
            self.assertTrue(any((out.parent / "archive").glob("*.html")))


class Notify(unittest.TestCase):
    CELLS = [((dt.date(2027, 3, 18) + dt.timedelta(days=i)).isoformat(),
              (dt.date(2027, 3, 18) + dt.timedelta(days=i + 14)).isoformat()) for i in range(19)]

    def _day(self, conn, c, n: int, deal: float | None):
        prices = {k: 25000 + 100 * (i % 3) for i, k in enumerate(self.CELLS)}
        if deal:
            prices[self.CELLS[9]] = deal
        Stats._store(self, conn, prices, dt.date(2026, 10, 1) + dt.timedelta(days=n))
        return stats.analyze(conn, c, NOW + dt.timedelta(days=n))

    def test_digest_daily_and_deals_once(self):
        c = cfg({"output": {"public_url": "https://x.example/f/"}})
        conn = db.connect(":memory:")
        n = notify.build(c, self._day(conn, c, 0, 15000), conn)
        self.assertTrue(n["send"] and n["digest"])
        self.assertEqual(n["new_outliers"], 1)
        nc = n["nextcloud"]
        self.assertIn("−", nc["subject"]); self.assertTrue(nc["subject"].endswith("{report}"))
        self.assertEqual(nc["messageParameters"]["report"]["link"], "https://x.example/f/index.html")
        self.assertFalse(notify.build(c, stats.analyze(conn, c, NOW), conn)["send"])     # 2nd run same day: silent
        n = notify.build(c, self._day(conn, c, 1, 15000), conn)                          # next day, same deal
        self.assertEqual((n["send"], n["digest"], n["new_outliers"]), (True, True, 0))
        self.assertTrue(n["nextcloud"]["subject"].startswith("🌸 Day"))
        n = notify.build(c, self._day(conn, c, 2, 13500), conn)                          # 10 % cheaper again
        self.assertEqual(n["new_outliers"], 1)

    def test_digest_twice_a_day_during_learning(self):
        c = cfg({"general": {**DEFAULTS["general"], "timezone": "UTC"}})
        conn = db.connect(":memory:")
        a = self._day(conn, c, 0, None)
        am = dt.datetime.combine(a.today, dt.time(8, 0), dt.timezone.utc)
        pm = am.replace(hour=16)
        self.assertTrue(notify.build(c, stats.analyze(conn, c, am), conn)["digest"])
        self.assertFalse(notify.build(c, stats.analyze(conn, c, am), conn)["send"])       # same half of the day: silent
        self.assertTrue(notify.build(c, stats.analyze(conn, c, pm), conn)["digest"])

    def test_after_learning_only_deals(self):
        c = cfg({"stats": {**DEFAULTS["stats"], "learning_days": 1}})
        conn = db.connect(":memory:")
        self._day(conn, c, 0, None)
        a = self._day(conn, c, 1, None)
        self.assertEqual(a.phase, "watching")
        self.assertFalse(notify.build(c, a, conn)["send"])
        self.assertEqual(notify.build(c, self._day(conn, c, 2, 15000), conn)["new_outliers"], 1)


if __name__ == "__main__":
    unittest.main()
