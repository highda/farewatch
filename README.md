# farewatch: a personal fare watcher

Watches flight prices for the trips you describe (routes, departure windows, trip length), keeps a history in SQLite, flags
statistical outliers (cheap trips compared with their neighbours and their own history) and publishes a single static HTML page
plus a `summary.json`. Optional: live spot-checks via SerpApi (with a backup key), deal-feed matching, and an HTTP trigger
(`farewatch serve`) so a scheduler such as n8n can run it and forward the ready-made notification text.

Python ≥ 3.11, standard library only, or a small Docker image. Everything personal (routes, windows, currency, timezone,
budgets, output folder) lives in `config.toml`; secrets live in environment variables.

## Quick start

```bash
cp config.example.toml config.toml            # then edit: routes, windows, currency, timezone, output.dir, user_agent contact
cp .env.example .env && chmod 600 .env        # TRAVELPAYOUTS_TOKEN is the only required secret
python3 -m farewatch --config config.toml doctor    # preflight: tells you what's missing
python3 -m farewatch --config config.toml run       # collect + report
python3 -m farewatch demo --days 14                 # optional: synthetic data, to see the page before real data exists
python3 -m unittest discover -s tests               # tests
```

Docker/Portainer: `docker build -t farewatch:local .`, then deploy `docker-compose.yml` with the stack env variables listed in
`.env.example` (secrets, `FAREWATCH_DATA_DIR`, `FAREWATCH_OUT_DIR`). The container runs `farewatch serve`; `n8n/farewatch-workflow.json`
is an example schedule (08:00 + 16:00) that calls `POST /run` and forwards the notification to Nextcloud (edit URL, user and
credentials after importing).

Below: design notes. The concrete numbers and dates (Prague → Tokyo, spring 2027, CZK) come from the author's own setup and are
just the example configuration.

## 1. Goal and ground rules (example setup)

- Watch **PRG → Tokyo** (HND + NRT), **2 travellers, ~2-week trips** (12–16 days), round trip.
- Preferred: spring 2027 (sakura, ~mid-March → early April). Not required, so the net also covers autumn 2026,
  the early/late spring shoulders and post-Golden-Week May.
- **Phase 1, learn the market:** what does a normal 2-week trip cost per window?
  **Phase 2, observe:** 1–2 collections a day, flag statistical outliers, show them in a page.
- **Rules:** official APIs and public feeds only. No headless-browser scraping of Google/Skyscanner/Kayak, no
  getting around bot walls, low request volume (a few dozen a day), honest User-Agent, conditional GETs.
- Runs anywhere with Python ≥ 3.11 (stdlib only, no dependencies) or as a tiny Docker image.

## 2. Sources: what is viable (checked 2026-09-29)

| Source | Verdict | Role | Notes |
|---|---|---|---|
| **Travelpayouts / Aviasales Data API** | ✅ **primary** | discovery: price level + outliers | Free with an affiliate account (token in Profile → API token). Returns **cached** prices from recent user searches: cheap to query (`/v2/prices/latest`, `/v2/prices/month-matrix`, ~14 requests per run), but not bookable quotes and possibly sparse for some date pairs. Documented limit ~200 requests/h per IP; we use a fraction. Prices are per person. |
| **SerpApi `google_flights`** | ✅ **optional** | live spot-check of the cheapest cells + a few fixed anchors | A commercial API that returns Google Flights results as JSON, so we don't scrape Google ourselves. Free plan is small (SerpApi lists 250 searches/month; some pages say 100 for Google Flights, so check your dashboard); paid from $25/1000. Repeated identical searches are cached and free. Also returns Google's own "typical price range" and price level. Budget guard built in (`monthly_budget`). |
| **Deal feeds** | ✅ cheap extra | human-curated error fares / sales | `levneletenkyzprahy.cz/rss0k` (Prague-specific, works, verified: 20 items). Reddit r/FlightDeals `.rss` works but returned **HTTP 429** on a rapid second request, so it's off by default with a 24 h interval. Keyword filter uses stems because Czech declines (Tokio/Tokia/Tokiu). |
| Duffel | ⚪ later | live offers, real airline supply | Pay per search ($0.005 above a 1500:1 search:book ratio, so effectively every search is billable for a non-booking account), needs a live-mode account; carrier coverage for Asia unknown. Plugin slot exists (`sources/`), not implemented. |
| Amadeus Self-Service | ❌ | | Reported decommissioned 17 Jul 2026, no free tier. |
| Kiwi Tequila | ❌ | | Invite-only partner programme. |
| Skyscanner API | ❌ | | Commercial partners only. |
| fly4free.com, secretflying.com | ❌ | | Cloudflare challenge (HTTP 403) for non-browser clients; we do not work around it. fly4free.cz: no feed found. |
| Scraping Google Flights / Skyscanner / Kayak directly | ❌ | | Against their terms + bot walls. Excluded by the ground rules. |

Nothing in the tool depends on a single source: sources are plugins that return `Offer` rows.

**Untested against live APIs (no token was available while building):** the Travelpayouts and SerpApi parsers are
written from their documentation and covered by fixture tests only. Verify with `probe` first (§9).
The RSS path was tested against the live Czech feed.

## 3. Architecture

```
scheduler (n8n / cron)            ┌── sources/travelpayouts   (discovery: cached cells)
   │  farewatch run               ├── sources/serpapi         (verify: live check of cheapest cells + anchors)
   ▼                              ├── sources/feeds           (RSS deals, keyword filter, dedupe)
pipeline.collect ─────────────────┘
   │ normalise: home currency, drop out-of-scope cells / too many stops, cheapest offer per cell per source
   ▼
SQLite  data/farewatch.sqlite   (append-only `offers`, `runs`, `deals`, `http_cache`)
   ▼
stats.analyze  → neighbour + history baselines, robust outliers, window summaries, trend series
   ▼
report.write   → <output.dir>/index.html  (+ summary.json, archive/YYYY-MM-DD.html)
```

- **Cell** = (source, route, departure date, return date). One stored row = cheapest known price for that cell in one run.
- **Route** = origin + destination code. Alternative origins (VIE, MUC…) are just more routes, with an optional
  `penalty_pp` for the ground transfer so they compete fairly.
- **Windows** = named departure-date ranges. Trip length is one global range (`[trip]`).
- All prices stored **per person in the home currency** (`[general] currency`); totals = × `pax`. (Known simplification: a 2-seat quote can be dearer than
  2 × the 1-seat price when a fare bucket runs out. Check before booking.)
- Failures are isolated per source; every run is logged in `runs` and shown at the bottom of the page.

## 4. Request budget (defaults)

| Thing | Volume |
|---|---|
| Travelpayouts | routes × departure months (7 with the example windows) × 2 endpoints ≈ **14 requests/run**, 2 s apart |
| SerpApi | `verify_top_k` (2) per run + anchors; hard cap `monthly_budget` (90). With 2 runs/day it would run dry in ~3 weeks. Either run it once a day (`collect --source serpapi`) or lower `verify_top_k` |
| Feeds | one conditional GET per feed, at most every `min_interval_hours` (12 / 24) |

## 5. Scheduling (outside the code)

- Phase 1 (first ~2 weeks): 1 run/day is enough to see the price level. Phase 2: 2 runs/day.
- Cron equivalent (n8n *Schedule Trigger → Execute Command* works the same way):
  ```
  0 7  * * *  farewatch run                                          # everything + report
  0 18 * * *  farewatch collect --source travelpayouts && farewatch report
  ```
- Exit code **2** means no price source succeeded (feeds alone don't count), which is what a scheduler should alert on.
  stdout is one JSON line per command; logs go to stderr.
- Hourly polling is pointless (cached data changes slowly) and is the quickest way to get blocked.

## 6. Statistics and the outlier threshold (knobs in `[stats]`)

**Phases.** *Learning* = the first 14 calendar days counted from the first run's timestamp (`learning_days`; missed or failed days still count): the page and `summary.json` list **every** tracked trip
(by window and date) plus per-window medians, so you see the whole market. *Watching* = day 15 on: only outliers (plus window
medians and charts as context). `[output] full_table = always|never` overrides.

**Decision: an outlier is a trip that is ≥ 15 % below its baseline *and* ≥ 2.5 robust σ below it. ≥ 30 % is "urgent".**
- *Why a percentage:* prices scale with season, so a % is comparable across windows and meaningful in money: 15 % of ~25 k CZK is
  ~3.7 k per person, ~7.5 k for two. That's where a "deal" starts being worth a look. 30 %+ is error-fare territory.
- *Why also σ:* a % alone would fire constantly in noisy windows (peak week) and never in calm ones. The robust z-score
  (median/MAD, sigma floored at 3 % of the median) adapts to each window's own noise.
- These are opening values, chosen before seeing real data. The learning-phase page shows a **threshold check** table: how many
  trips per day *would* have been flagged at ≥10/12/15/20/25/30 % over the last 14 days. On day 14, pick the threshold that
  yields a handful of flags per day at most, and edit `min_drop_pct` (nothing else needs to change).

**Two independent yardsticks per cell**, robust so a single error fare can't drag its own baseline:
1. **Neighbours (from day 1).** Compare with the other trips *currently* departing within ±`neighbour_days` (5). Seasonality is
   absorbed: sakura week is expensive for everyone, so the question is only "this one vs. its neighbours". Falls back to the whole
   window if there are fewer than `min_neighbours` (4).
2. **History (after `min_history_days`, 5).** Compare with the cell's own past daily minimums.

Flags: **below neighbours**, **below its own history** (each needs the σ and % conditions), **new low** (below every past
observation and at least half of `min_drop_pct` under the historical median), **urgent** (≥ `urgent_pct` under baseline).
`summary.json` carries `urgent: true` so a notifier can treat these separately.

Expected weak spots: hundreds of correlated cells means the odd false positive is normal (raise `min_drop_pct` if noisy); cached
sources can jump when a sample appears or disappears; cells expire as dates pass, so a trend line mixes slightly different cell
sets.

Rough sanity numbers (my guesses, not measured): Prague–Tokyo return economy ~16–25 k CZK per person off-peak, 25–40 k around
sakura/Easter. The tool sets its own baseline; these only help judge whether the data looks plausible.

## 7. The web view

- `farewatch run` writes `index.html` (single file, inline CSS + SVG, no JS, no external requests, light/dark), `summary.json`
  (phase, thresholds, calibration, headline, outliers, windows, run status; `all_trips` during learning; ready for n8n) and a dated
  copy in `archive/`. Files are written atomically (temp + rename), so the web server never serves a half-written page.
- Learning phase: banner with the day counter, threshold check table, hero, *provisional* outliers, per-window cards, **all trips**
  table (open). Watching phase: hero, outliers, optional live-check table, per-window cards, deal feed hits, run log.
- **Where it lives:** `[output] dir = "/var/www/<site>/flights-<random>"` (your public web host). The host is public, so use an
  unguessable folder name (decided). `noindex` is on as well. The SQLite database stays in `data_dir`, **outside** the web root
  (`doctor` fails if it's inside).

## 8. Deployment (server)

Plain Python (≥ 3.11, `tzdata` recommended) or Docker:

```bash
cd farewatch-upload && cp config.example.toml config.toml
RAND=$(openssl rand -hex 8); echo "flights-$RAND"       # -> [output] dir = "/var/www/<site>/flights-<that>"
$EDITOR config.toml        # output.dir, user_agent contact, windows if you want to change them
printf 'TRAVELPAYOUTS_TOKEN=xxxx\n' > .env && chmod 600 .env       # optional: SERPAPI_KEY=... and SERPAPI_KEY_BACKUP=...
python3 -m farewatch --config config.toml doctor          # preflight: tells you what's missing, prints the public URL
python3 -m farewatch --config config.toml probe travelpayouts --month 2027-03    # see §9
python3 -m farewatch --config config.toml run

# docker variant (config.toml: data_dir="/data", output.dir="/out")
docker build -t farewatch .
docker run --rm --user $(id -u):$(id -g) --env-file .env \
  -v $PWD/data:/data -v /var/www/<site>/flights-XXXX:/out \
  farewatch --config /data/config.toml run
```

## 9. Setup checklist and tokens

**Tokens / accounts**
| What | Needed? | How |
|---|---|---|
| **Travelpayouts API token** | **Yes**, the only required one | Free affiliate account at travelpayouts.com, then Profile → API token. Put it in `.env` as `TRAVELPAYOUTS_TOKEN`. The sign-up may ask for a site or traffic description (their programme is built for affiliates; check their terms for personal use). Without it there are no prices, only deal-feed items. |
| SerpApi key | Optional (live Google Flights spot-checks) | serpapi.com free plan, then `SERPAPI_KEY` in `.env` and `[sources.serpapi] enabled = true`. Skip until the Travelpayouts data looks good. A second account's key in `SERPAPI_KEY_BACKUP` is used for the rest of a run once the first key is exhausted/rejected (401/403/429); `monthly_budget` is one total over both keys. |
| Feeds (RSS) | No token | The example uses a Czech deal feed; replace with feeds for your market. Reddit stays off. |
| Nextcloud app password | Later (notifications, not in this MVP) | Nextcloud → Settings → Security → app password. |

**Setup checklist, in order**
1. Get the Travelpayouts token.
2. Upload the folder; `cp config.example.toml config.toml`; set `output.dir` to `/var/www/<site>/flights-<random>` and put a real contact in
   `user_agent`.
3. `.env` with the token, `chmod 600`.
4. `doctor` until it shows no `FAIL`.
5. `probe travelpayouts --month 2027-03`, then also `--endpoint month_matrix`. Check how many raw rows come back and how many
   are 12–16-day round trips. **This decides whether cached data is dense enough**; if it's sparse, lean on SerpApi anchors and the
   feeds, or widen `[trip]`. Drop an endpoint from `endpoints` if it's empty or broken.
6. `run` twice, open the page, and compare a few window prices with your own Google Flights lookups for 2–3 date pairs.
7. Schedule it (§5): daily for the learning phase, twice daily afterwards if you like.
8. Day 14: read the threshold check table and adjust `min_drop_pct` if needed.

## 10. Later (wiggle room)

- **Notifications (done, 2026-09-30):** the `farewatch` container (`docker-compose.yml`, `farewatch serve`) is triggered by
  n8n (`n8n/farewatch-workflow.json`): `POST http://farewatch:8080/run` returns a ready `notification.nextcloud` payload that
  n8n forwards to Nextcloud `admin_notifications`. `farewatch/notify.py` decides: a daily digest during the learning phase,
  afterwards only trips not announced before (again if ≥ `[notify] redrop_pct` cheaper, or newly urgent). Urgent deals lead
  the subject. Sent state lives in the `notified` table. The Nextcloud iOS app ignores the link; the web UI follows it.
- More sources as plugins (Duffel; airline sale pages), open-jaw (Tokyo in / Osaka out) as extra routes, one-way
  splitting, baggage-included flag, a "how many days until the sakura peak" note, price-per-2 verification.
- Simple booking guidance from the data ("median has been flat for 14 days, this is the 8th percentile").
- Tuning `[stats]` per window (peak weeks are noisier).

## 11. Open questions

- Acceptable stops / airlines / layover length? Currently only `max_stops = 1`.
- Alternative origins worth a train ride (VIE, MUC, DRS)? Off by default in the example config.

## 12. Layout

```
japan-tickets/
  README.md  config.example.toml  Dockerfile  pyproject.toml
  farewatch/  cli.py config.py db.py http.py plan.py pipeline.py stats.py report.py
              sources/  base.py travelpayouts.py serpapi.py feeds.py mock.py
  tests/test_farewatch.py         # python3 -m unittest discover -s tests
```
`python3 -m farewatch demo --days 14` builds a **synthetic** dataset in a separate DB and renders a demo page (banner says so),
useful for seeing the report before any real data exists.


## Windows, headline boxes, deployment notes

- **Window matching:** a trip belongs to the first window (config order) whose departure range contains it. A window may add
  `overlap_from`/`overlap_to`: the whole trip (depart..return) must then also touch that range (used for `early-sakura` = any trip
  touching February 2027; list such windows before the ones they carve out of, e.g. `winter`). Overlaps between windows are resolved in
  the config, never in code.
- **Headline boxes:** each window with `headline = true` (priority-1 windows implicitly) gets a big price box at the top of the page, plus
  "cheapest overall".
- **Scheduling:** n8n workflow `n8n/farewatch-workflow.json` runs at 08:00 and 16:00 (Europe/Prague). `daily_budget` is a shared cap across
  both runs (12 needed: 2 x (anchors + `verify_top_k`)), the rest is headroom for manual runs.
- **Seeding used API quota:** to tell the budget counter about searches made elsewhere, insert a row into `runs` with
  `source='serpapi'`, `kind='verify'`, `requests=N`, `status='manual'`, `started_at` = any time this month before today. `manual` rows count
  toward `monthly_budget` but are ignored when the learning phase is dated.
- **Deployment:** Portainer stack from `docker-compose.yml`; stack env needs `TRAVELPAYOUTS_TOKEN`, `SERPAPI_KEY`, `SERPAPI_KEY_BACKUP`,
  `FAREWATCH_RUN_TOKEN` and `FAREWATCH_OUT_DIR` (the public web folder) and `FAREWATCH_DATA_DIR` (config + database folder), both kept out of the repo. The build is manual on the host:
  `docker build -t farewatch:local .`

## License

MIT, see `LICENSE`.
