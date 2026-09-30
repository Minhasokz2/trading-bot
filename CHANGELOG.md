# Changelog

## v6.2 — 2026-09-30 — the chart, and a web app you would actually use

- **Every audit now draws its opportunity on the coin's chart.** `audit/chart.py` + `audit/static/chart.js`: an
  interactive candlestick chart (the last 500 candles of the audited timeframe, volume, EMA 20/50/200, confirmed
  swing pivots) with the trade plan on it — entry zone, stop and targets as dashed levels and as reward / risk
  boxes right of the last candle, forming patterns with their trigger and invalidation levels, completed bearish
  patterns, candlestick names and the strategy signal marker — and an opportunity panel: verdict, score, plan
  with distances and R multiples, signals (approved or why blocked), patterns, regime. Scroll to zoom, drag to
  pan, hover for OHLCV. No dependency and no CDN: a canvas renderer of our own, so it works from a `file://`
  report and under the web app's strict CSP. Written as `<report>_chart.html` next to every report (linked from
  the markdown, the dashboard and the CLI output) and embedded in the report page of the web app; the chart data
  is in the report JSON (`chart`).
- **Web app redesigned** (`audit/webapp.py`, `audit/static/app.js`): an app shell with navigation and a light /
  dark switch; a home page with the audit form up front, the latest audit per coin as cards (verdict, score,
  plan, links to report and chart), status tiles, schedule and jobs; a reports page that lists audits with
  verdict, score and approved signals (from `audits.csv`, no JSON is opened), a filter box and links to chart /
  JSON / tearsheet; a report page with a summary card (verdict, plan, signals, re-run button), the chart, then
  the report; a live job page with the pipeline stage, elapsed time, error and report / chart buttons; the
  dashboard inside the app; relative times everywhere.
- Security kept: no inline script except one hashed theme snippet; raw HTML in a report is rendered as text;
  stored chart pages are re-rendered from the report JSON with the current renderer (or, without JSON, served
  with a policy that allows exactly the script they carry).
- `patterns.scan` reports the start index of a forming pattern (drawn as the level's start on the chart).
- Tests: `tests/test_chart.py` (chart data, page, CSP hash, and — when Node + Playwright + Chromium are
  present — the chart drawn in a real browser with the plan, patterns and signals in the panel).

## v6.1.1 — 2026-09-30 — first live audit crashed

- **Fix: every first audit of a coin on a fresh install died in the regime step** with
  `TypeError: loop of ufunc does not support argument 0 of type float which has no callable log method`
  (`np.log(df["close"])`). The kline cache started from an empty, untyped DataFrame and pandas 3 no longer
  ignores that in `concat`, so every column of the first fetch came back as `object`. The data layer now
  types every frame it returns (`normalise_frame`), never concatenates with an empty frame, and repairs
  cache files written by the broken version.
- The cache refill after a long pause is contiguous (it used to stop after 5000 candles and could leave a
  hole); a cache that fell too far behind is rebuilt instead.
- Point-in-time fetches (`--as-of`) return the full number of candles asked for (they were one short).
- The scanner's 24h-ticker list falls back to the public REST endpoint if the SDK's answer is not a list.
- Tests: `tests/test_live_client.py` runs the real `BinanceClient` (pagination, cache, forming-candle rule,
  replay view, ticker list, the full audit and the CLI incl. `--scan`) against a stand-in for the Binance
  SDK — the code path a hosted audit takes, which nothing exercised before.

## v6.1 — 2026-09-30 — hosting

- **Fix: "Cross-site request refused" on every form in a real browser.** The pages were served with `Referrer-Policy: no-referrer`, which makes browsers send `Origin: null` on their own form posts, and the CSRF check refused that. The check now trusts the browser's `Sec-Fetch-Site` header (Origin only as a fallback) and the policy is `same-origin`.

- **Web app** (`audit/webapp.py`, FastAPI): login (HTTP Basic, constant-time compare, failed-login throttle), a form to audit a
  coin or scan the market, live job log, rendered reports, the dashboard, a Connectivity page that tells you whether the server's
  region can reach Binance (HTTP 451 = wrong region), `/healthz`. Cross-site POSTs are refused, reports are served with a strict
  Content-Security-Policy, files are addressed by exact name only.
- **Job runner and scheduler** (`audit/jobs.py`): every audit is a subprocess (an out-of-memory kill cannot take the site down),
  jobs survive restarts, cancel and timeouts work, schedules run on candle closes and never overlap themselves, old reports are pruned.
- **Deployment**: `Dockerfile` (unprivileged user, disk ownership fixed at start), `render.yaml`, `.dockerignore`, pinned
  `requirements-web-lock.txt` (resolved on Python 3.12), `requirements` split into core / web / full, `docker/smoke_test.sh`,
  `DEPLOY.md`, local launchers (`SERVE.bat`, `linux/serve.sh`, `mac/12 Web app`), a CI job that builds the image and smoke-tests it.
- `COIN_AUDIT_DATA_DIR` moves reports, logs and caches onto a persistent disk; `settings.toml` and `events.csv` can be overridden there.
- **Fix: audits now exit non-zero when an audit or scan reports an error** (they used to print the error and exit 0, so a scheduler
  or job runner showed success). Failed jobs show the reason, with a region hint for HTTP 451.
- Measured, replacing my earlier guesses: a full audit is ≈ 12 s of CPU and ≈ 500 MB of memory; the earlier "2 minutes" was
  measured while other test jobs shared the CPUs.
- Tests: 70+ new (runner, scheduler, web security, deployment-file consistency, real demo audit through the web routes).

## v6 — 2026-09-30 — "audit the auditor"

Everything below runs offline on synthetic data in the test-suite; live endpoints are unchanged.

### Validation (the core)
- **12 gates instead of 10.** G10 = probability of backtest overfitting (CSCV, Bailey et al. 2017) < 0.5;
  G11 = deflated Sharpe ratio (Bailey & López de Prado 2014) ≥ 0.90 given the number of parameter sets
  tried, skew and kurtosis. Minimum track-record length reported per strategy.
- **Library-wide false-discovery control**: Benjamini-Hochberg q-values across all 27 strategies (report + note).
- Overfitting statistics per strategy in every report: trials, PBO, P(loss OOS), IS→OOS slope, PSR, DSR.
- Pattern strategies now REQUIRE breakout volume ≥ 1.2× (the 1.0× "no filter" grid value is gone).

### Detector fixes found by the new planted-pattern tests
- Falling wedge / channel: a later touch re-forms the pattern instead of being discarded (dedup by touch set).
- Range box: each pivot event re-arms the breakout setup (identical levels no longer suppress it).
- Trendline break: a lower low after the second high (normal in a downtrend) no longer kills the setup;
  every later confirmed pivot low re-arms the line with an updated stop.
- V-bottom: a bottom is armed only after the first up-bar, never mid-decline.
- Bull flag: a pole four bars before the newest candle could form a flag in the backtest but not live.

### Sizing and execution
- `audit/sizing.py`: Kelly from each strategy's own walk-forward trades (bootstrap 20th percentile,
  half-Kelly, capped by status), volatility targeting, linear-book slippage at YOUR position size,
  the largest order that keeps the backtest's slippage assumption, portfolio heat and correlation clusters.
- Signals carry price, ATR, stop, target, meta decision, size multiplier and Kelly risk.

### Regime
- Two-state Markov-switching volatility model (statsmodels, filtered probabilities = no lookahead) can veto
  dip / momentum / range modules; HAR-RV next-day volatility forecast in the volatility check.

### Meta-labeler
- Uniqueness sample weights, isotonic calibration, López de Prado bet sizing, split-conformal
  ACCEPT / ABSTAIN / REJECT, population-stability drift check that switches the model off.

### New modes
- `--scan N` market scanner with ranking and a correlation-aware allocation (`audit/portfolio.py`), parallel `--jobs`.
- `--loop` watch mode (re-run after every candle close), `--notify` (Discord / Telegram via env vars),
  `--dashboard` static HTML dashboard, `--as-of DATE` point-in-time replay, `--offline DIR`, `--demo`,
  `--strategies` filter, local kline cache (`audit/cache/klines`).
- `--review` now also grades every logged signal and keeps `logs/track_record.json`; a strategy whose live
  hit rate is significantly below its backtest gets its new signals BLOCKED (feedback loop).
- `research/verdict_backtest.py`: replays the whole audit at many past dates and grades the verdicts.

### Bugs fixed on the way
- Formation / indicator caches were keyed by `id(df)`; Python recycles object ids, so auditing several coins in one
  process (or the test-suite) could serve another frame's formations. Entries now keep a reference and check identity.
- Every offline frame carried a `DataFrame.attrs` list that pandas 3 deep-copied on each operation (indicator calculation on
  3000 candles dropped from 1.6 s to 0.7 s once removed); the data layer now strips `attrs` from anything it loads.
- `review()` grading and the signal log migrate older CSV headers instead of misaligning columns.

### Engineering
- `tests/` (pytest, offline): backtester mechanics, 15 planted patterns × volume/no-volume, candlesticks,
  lookahead self-test for all 27 strategies + a deliberately leaking strategy, gates, overfitting statistics,
  offline client / point-in-time cuts, regime alignment, Markov + HAR-RV, meta-labeler blocks, sizing,
  notify / portfolio / dashboard, full audit → report → review, CLI, verdict replay.
- `audit/settings.toml` for costs, gates, risk, weights, scan and notify settings.
- GitHub Actions workflow, `.gitignore`, `pyproject.toml`, MIT licence, new launchers on all three platforms.
- pandas 3 / numpy 2 compatibility.

## v5 and earlier
See README "Version history".
