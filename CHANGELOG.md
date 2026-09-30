# Changelog

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
- Every offline frame carried a `DataFrame.attrs` list that pandas 3 deep-copied on each operation (a 12-minute demo
  audit became 2 minutes once removed); the data layer now strips `attrs` from anything it loads.
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
