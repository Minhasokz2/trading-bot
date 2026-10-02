# Coin Audit Bot v7 — Binance Spot strategy research bot

Everything here is free and open source. No API keys, no real orders: the audit is **read-only** and both
Freqtrade bots run in **dry-run** (paper trading). Research tool, not financial advice.

You type a coin (`SOL`), pick a timeframe (15m / 1h / 4h / 1d / all), and the bot audits whether a spot
LONG setup is worth taking: a 0–100 score, a verdict (FAVORABLE ≥ 70 · WATCHLIST 55–70 · NEUTRAL 40–55 ·
AVOID < 40), a trade plan sized for your account, and a full report. v6 adds a market scanner, a watch
loop with notifications, a dashboard, point-in-time replays that grade the bot's own verdicts, and a
much more hostile validation (12 gates, overfitting statistics, live-vs-backtest drift detection).

**New in v7:** every audit opens with a **trader brief** — what to do now, the setup in one paragraph, the plan with your
position size, what would make it a buy, what would kill it, which strategies really have an edge here — and the chart
**keeps streaming live** after the analysis (Binance's public stream, server fallback), re-reading the plan against the
live price. The web app is a console: overview, coin pages, an editable watchlist that drives the scheduler, settings.

**New in v6 — start here**

| Command | What it does |
|---|---|
| `DEMO.bat` · `./linux/demo.sh` · `mac/10 Demo (offline)` | Runs the whole pipeline on synthetic data with no network. Proves the install works. |
| `TEST.bat` · `./linux/test.sh` · `mac/9 Self-test (offline)` | The offline test-suite (planted chart patterns, lookahead checks, backtester maths, full audits). |
| `SCAN.bat 40` · `./linux/scan.sh 40` · `mac/6 Scan the market` | Audits the 40 most traded coins, ranks them, suggests a correlation-aware allocation. |
| `WATCH.bat SOL ARB --tf 4h --notify` · `./linux/watch.sh` · `mac/7 Watch mode` | Re-audits after every candle close, pushes approved signals to Discord/Telegram, rebuilds the dashboard. |
| `DASHBOARD.bat` · `./linux/dashboard.sh` · `mac/8 Dashboard` | Static HTML dashboard of the latest audit per coin (`audit/reports/dashboard.html`). |
| `AUDIT.bat SOL --as-of 2026-06-01` | What would the audit have said on that date? (only candles closed by then are visible) |
| `RESEARCH_VERDICT_BACKTEST.bat SOL` · `./linux/research_verdict_backtest.sh SOL` · `mac/11` | Replays the whole audit every 7 days over 6 months and grades the verdicts it gave ("audit the auditor"). |
| `AUDIT.bat --review` | Grades past audits **and every logged signal**; builds the live track record that later audits use. |

| `SERVE.bat` · `./linux/serve.sh` · `mac/12 Web app` | The console on your own machine (`http://127.0.0.1:10000`): overview, audit any coin, coin pages with the brief and the live chart, watchlist, settings, reports, jobs. This is what gets hosted online. |
| `tradingview/coin_audit_library.pine` | **TradingView indicator (v7.2):** all 27 strategies with their exits, the pivot/pattern engine and bearish blockers, the coin and market regimes, a paper-trading backtest of every strategy on the chart with validation gates, the bot's score and verdict, the trade plan and what to do now, in a themed dashboard with tooltips, drawings and alerts — see [tradingview/README.md](tradingview/README.md). |
| `tradingview/INDICATOR_PROMPT.md` | **The master prompt:** every strategy, rule, gate, score and UI element of the indicator in full detail — read it to understand the chart, or give it to an AI / Pine developer to rebuild, extend or port the indicator. |
| every audit → `audit/reports/<SYMBOL>_<tf>_<time>_chart.html` | **The chart (v6.2):** the candles the audit looked at with the opportunity drawn on them — entry zone, stop, targets as levels and reward/risk boxes, forming patterns with trigger and invalidation, signal markers, EMAs, pivots — plus the verdict, plan and signals next to it. Opens in any browser, no internet needed. |
| `docker/smoke_test.sh URL PASSWORD` | Checks a running web app (local, Docker or hosted): login wall, data-source reachability, a full demo audit. |

**Host it online:** see [DEPLOY.md](DEPLOY.md) — a Render Blueprint (`render.yaml`) + `Dockerfile` give you a private website
with a job queue, a candle-close scheduler for your watchlist, optional Discord/Telegram alerts and a persistent disk.
It explains why Render fits and Vercel does not, what it costs in memory (≈ 500 MB per audit), and the region trap (Binance refuses the US).

See [CHANGELOG.md](CHANGELOG.md) for the complete v6 list and the detector bugs the new tests caught.

## What's inside

| Part | What it does | Built on |
|---|---|---|
| `audit/` | You name a coin, it audits the setup: score, verdict, trade plan, report, tearsheet | binance-connector-python (`binance-sdk-spot`), pandas-ta-classic, LightGBM, statsmodels, quantstats |
| `audit/audit.py --scan` | Market scanner + portfolio allocation (`portfolio.py`, `sizing.py`) | same |
| `research/download_history.py` | Official bulk history with checksums, raw vs clean files | binance/binance-public-data |
| `research/sweep_vectorbt.py` | 200+ parameter sets in seconds, picked on old data, scored on unseen data | polakowo/vectorbt |
| `research/verdict_backtest.py` | Point-in-time replay of the whole audit, graded | — |
| `freqtrade/` | 9 dry-run strategies (Trend, Donchian, Pullback, SMA Offset, BinCluc, Elliot, Confluence, EMA cross, FreqAI LightGBM) | freqtrade + TA-Lib, Docker image `stable_freqai` |
| `audit/webapp.py` · `jobs.py` · `serve.py` · `Dockerfile` · `render.yaml` | Hosted web app: login, run form, job queue + scheduler, report viewer, dashboard, connectivity check | FastAPI, uvicorn |
| `tests/` | Offline test-suite on synthetic data (see "Testing") | pytest |

## Setup

**Windows:** install Python 3.12 from python.org (tick "Add to PATH"), double-click `1_INSTALL.bat`, then `DEMO.bat`.

**Linux:** `./linux/install.sh`, then `./linux/demo.sh` and `./linux/audit.sh SOL`.

**macOS:** Python 3.11+ (`brew install python@3.12`), `brew install libomp` (LightGBM), clear quarantine once
(`xattr -dr com.apple.quarantine . && chmod +x mac/*.command linux/*.sh`), then double-click `mac/1 Install.command`
and `mac/10 Demo (offline).command`.

**Freqtrade bots (optional):** Docker Desktop (Windows/Mac; Colima works too) or Docker Engine (Linux); change the
three `CHANGE-ME` values in `freqtrade/user_data/config.json`; then the `FT_*` launchers.

If Binance is blocked on your network (HTTP 451/403), use a VPN or a server in an allowed region. The demo, the
self-test and `--offline` work without any network.

## Daily use

| Command | Result |
|---|---|
| `AUDIT.bat SOL` | Asks the timeframe, audits SOLUSDT → `audit/reports/` (.md, .json, `_chart.html`, tearsheet) + `audit/logs/audits.csv`, `signals.csv` |
| `AUDIT.bat SOL --tf all` | Every timeframe, tells you which one (if any) has strategies that survive validation |
| `AUDIT.bat SOL ARB INJ` | Several coins + a ranking |
| `AUDIT.bat SOL --strategies trend,dip` | Restrict the library (ids, families or name fragments) — faster |
| `AUDIT.bat SOL --no-market` / `--no-ml` / `--no-futures` / `--no-tearsheet` | Skip the crypto-wide + macro layer / the meta-labeler / futures data / the HTML tearsheet |
| `AUDIT.bat SOL --watch` | Also add SOL/USDT to the Freqtrade dry-run whitelist |
| `AUDIT.bat SOL --offline research/data/clean` | Audit from local parquet files (downloaded with `RESEARCH_DOWNLOAD.bat`) |
| `AUDIT.bat SOL --notify` | Push the summary when the verdict is FAVORABLE (`DISCORD_WEBHOOK_URL` or `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`) |
| `SCAN.bat 40 --tf 4h --jobs 4` | Scan the top 40 coins in 4 processes → `audit/reports/scan_4h_<time>.md` |
| `AUDIT.bat --review` | Grade past audits and signals; per-strategy live win rate vs backtest (`audit/logs/track_record.json`) |

Suggested order for a new coin: **AUDIT → RESEARCH_DOWNLOAD → RESEARCH_SWEEP → RESEARCH_VERDICT_BACKTEST →
AUDIT --watch → FT_1 → FT_2 / FT_3 → FT_4 → FT_5**, then `--review` and the dashboards after a few weeks.

Every audit keeps a local kline cache in `audit/cache/klines/` (parquet), so repeated audits and `--as-of` replays
are fast. `--no-cache` disables it. Settings (fees, gates, risk caps, weights, scan size, notifications) live in
`audit/settings.toml`.

## How the audit works (per coin + timeframe)

Each stage can only **block** a signal, never create one.

1. **Market checks** — liquidity (24h volume, spread, ±1% depth; < $1M/day forces AVOID), multi-timeframe trend
   (EMA20/50/200, slope, ADX/DI on 1d/4h/1h), momentum (RSI, MACD, ROC), volatility (ATR%, 30/90d vol, 90d
   drawdown, **HAR-RV next-day volatility forecast**), BTC regime + relative strength, taker flow.
2. **Regime classifier** — trend / range / transition (ADX + efficiency ratio), volatility percentile, BTC risk-on/off,
   liquidity, funding, and a **two-state Markov-switching volatility model** (statsmodels; filtered probabilities
   only, so no lookahead) that can veto dip / momentum / range modules.
3. **Strategy library** — 27 modules, one interface, never an order. Each runs through **12 gates**:

   | Gate | Rule |
   |---|---|
   | G0 | Lookahead self-test: signals identical on truncated data |
   | G1 | Walk-forward expectancy > 0 after 0.1% fee + 0.05% slippage per side |
   | G2 | Walk-forward profit factor ≥ 1.20 |
   | G3 | Positive in a majority of folds |
   | G4 | Still PF > 1 at **2× costs** |
   | G5 | Neighbouring parameter values also viable (no isolated peak) |
   | G6 | Still positive after removing the best 5% of trades |
   | G7 | Final untouched holdout (newest 15%) expectancy > 0 — used once |
   | G8 | ≥ 20 walk-forward trades |
   | G9 | Bootstrap significance p < 0.10 (plus Monte Carlo drawdown distribution) |
   | **G10** | **Probability of backtest overfitting < 0.5** (combinatorially symmetric cross-validation over the parameter grid) |
   | **G11** | **Deflated Sharpe ratio ≥ 0.90** — corrected for the number of parameter sets tried, sample length, skew and kurtosis |

   All 12 → **ACCEPTED** (risk 0.3%/trade) · G0 + G1 + G8 + ≥ 8 gates → **CANDIDATE** (0.15%) · otherwise **REJECTED** (0%).
   Parameters are picked per fold on past data only (anchored walk-forward with an embargo). Every report also shows,
   per strategy, the number of trials, PBO, P(loss out-of-sample), the in-sample→out-of-sample degradation slope,
   PSR/DSR, the minimum track record still needed, and a **Benjamini-Hochberg q-value** across the whole library
   (testing 27 strategies on one coin is itself a multiple-comparison problem).

   Exit mechanics available to every strategy: ATR / % / structure stops, ATR / % / R-multiple / measured-move targets,
   offset-activated trailing stops, ROI ladders, time stops, cascading early-loss cuts, limit (retest) entries.
   Protections in every backtest: 2-candle cooldown after a stop, stoploss guard (3 stops in 48 candles → pause 24).
   Every strategy is compared with **buy & hold and no-trade** (Sharpe, trades/year, time in market) and by regime.

   | # | Module | Family | Evidence | Trades only in |
   |---|---|---|---|---|
   | 1 | trend_ema_adx_v1 | trend | B | trend |
   | 2 | trend_ma_ensemble_v1 | trend | A/B | trend |
   | 3 | trend_donchian_v1 | trend | A/B | trend |
   | 4 | momentum_volmanaged_v1 | momentum | A | risk-on, not high vol |
   | 5 | pullback_nfi_modules_v1 (+ ablation table) | pullback | C | risk-on, not high vol |
   | 6–8 | sma_offset_v1 · bincluc_bb_v1 · elliot_ewo_v1 | dip | C | dip |
   | 9 | confluence_score_v1 (early-loss cuts) | trend | C | trend |
   | 10 | ema_cross_funding_v1 | trend | B | any |
   | 11 | ict_sweep_fvg_v1 | mechanised discretionary | C/D | any |
   | 12 | deep_drawdown_reclaim_v1 | low-cap rebound | D | any |
   | 13–22 | double/triple bottom · inverse H&S · range breakout · range support · bull flag/pennant/HTF · triangle/wedge · rounding/cup & handle · V-bottom · trendline break & retest · channel bounce | chart patterns | C | see report |
   | 23–27 | candle at key level · SMC sweep→BOS→FVG→OB · RSI failure swing · squeeze breakout · VWAP reversion | price action | C | see report |

   Evidence: A research-grade · B reproducible backtests · C author/league claims · D reference only.

4. **Chart patterns** (`audit/patterns.py`) — confirmed swing pivots (known K = 3 candles later), prior trend context,
   ATR-adjusted geometry, a candle **close** beyond the neckline, relative volume ≥ 1.2×, bullish BOS, reward:risk ≥ 1.5,
   entry on close or retest, stop "pattern" or "tight". Bearish patterns are research-only on spot and a fresh one
   blocks longs. 21 candlestick patterns, used only at key levels. Pending formations and recent candles are reported.
   The test-suite plants all 15 textbook shapes at three noise seeds and checks that each fires with volume and is
   rejected without.
5. **Crypto-wide + macro regime** (`audit/market.py`) — TOTAL, TOTAL2, TOTAL3, TOTAL2ES, TOTAL3ES, BTC.D, ETH.D, USDT.D,
   USDC.D, STABLE.D, OTHERS.D, BTC.D+USDT.D rebuilt from Binance price × CoinGecko supply + DefiLlama stablecoins;
   FRED macro (broad dollar, EURUSD, USDJPY, USDCNY, SPX, NDX, VIX, 10Y, 2Y, 10Y−2Y, real yield, WTI, Fed balance sheet,
   M2); gold via PAXGUSDT; 8 regimes; the altcoin-long eligibility score; per-coin derivatives fields; your
   `audit/events.csv`. Filters, never triggers. Cached in `audit/cache/`.
6. **Research analyses (reported, never executed)** — cointegrated pairs vs BTC/ETH, funding/basis carry, market
   making, 15m reversal, range-gated grid, narrative phase, funding/OI squeeze, weekly DCA.
7. **ML meta-labeler** (`audit/meta_model.py`) — LightGBM scores the strategies' own past signals. v6: sample weights by
   label uniqueness, isotonic calibration, López de Prado bet sizing, split-conformal ACCEPT / ABSTAIN / REJECT, and a
   population-stability drift check that switches the model off. Counts only if it lifts returns in walk-forward
   **and** the holdout.
8. **Signals, sizing, plan** — every firing strategy emits the standard record (`strategy_id, symbol, timeframe,
   timestamp, direction, confidence, expected_holding_bars, entry_type, stop_distance_atr, regime_required, regime_ok,
   status, max_risk_fraction, evidence_version, decision`) plus price, ATR, stop, target, meta decision, size
   multiplier and Kelly risk. The trade plan shows the Kelly stake from the lead strategy's own walk-forward trades
   (bootstrap 20th percentile, half-Kelly, capped by status), the volatility-target scale, and **the slippage your
   position size would pay against today's order book** with the largest order that still fits the backtest's
   assumption. A strategy whose live signals (graded by `--review`) are significantly worse than its backtest is blocked.

**FAVORABLE** ≥ 70 (requires an approved signal) · **WATCHLIST** 55–70 · **NEUTRAL** 40–55 · **AVOID** < 40.
BTC risk-off caps the verdict at WATCHLIST; a stale feed (newest candle older than 3 candle-lengths) blocks every signal.

### Timeframes

| Timeframe | History used | Good for |
|---|---|---|
| 15m | ~52 days | intraday; fees dominate — most strategies will be rejected |
| 1h | ~7 months | intraday swing |
| 4h | ~16 months | swing (default) |
| 1d | full listing | position trades; fewer trades, so G8 is harder to pass |

## Market scanner and portfolio (`--scan`)

`SCAN.bat 40 --tf 4h --jobs 4` audits the 40 most traded USDT pairs (leveraged tokens and stablecoins excluded),
ranks them (approved signals first, then validated edge, then score), computes the 90-day return correlation matrix,
groups coins whose correlation exceeds 0.70 into clusters, and allocates risk greedily: total open risk ≤ 1.5% of the
account, at most 5 positions, at most 2 per cluster, each position sized from its stop distance. The scan report
(`audit/reports/scan_<tf>_<time>.md/.json`) lists every coin, the allocation and why a coin was left out. Nothing is
executed.

## Watch mode, notifications, dashboard

`WATCH.bat SOL ARB --tf 4h --notify` re-runs the audits 30 s after every 4h candle closes, rebuilds
`audit/reports/dashboard.html` and pushes approved signals. Set `DISCORD_WEBHOOK_URL` and/or `TELEGRAM_BOT_TOKEN` +
`TELEGRAM_CHAT_ID` in the environment (never in a file in this repo). `notify.min_verdict` in `settings.toml`
chooses the minimum verdict that triggers a push.

## Audit the auditor

* `--as-of 2026-06-01` runs any audit as of that moment: only candles that had closed are visible, the 24h ticker and
  order book are rebuilt from candles, and the stale-data clock is that moment.
* `research/verdict_backtest.py SOL --tf 4h --span 180 --every 7 --jobs 4` replays the whole audit at every date,
  grades each verdict with the data that came after it (target 1 vs stop within the plan's horizon), and prints hit
  rate and mean return by verdict, a score-decile calibration table and approved-signal outcomes by strategy.
* `--review` grades every logged signal the same way and writes `audit/logs/track_record.json`: per strategy, live hit
  rate vs backtest win rate with a one-sided binomial test. Significant underperformance (≥ 8 graded signals, p < 0.05)
  blocks that strategy's new signals until it recovers.

## Testing

`TEST.bat` / `./linux/test.sh` / `mac/9 Self-test (offline)` run `pytest` on synthetic data, no network:

* backtester mechanics (fills at the next open, stop at the worse of stop/open, targets, time stops, weights, costs,
  cooldown and stoploss guard, limit entries, trailing stops, ROI ladders, early cuts);
* 15 planted textbook patterns × 2 seeds × with/without volume, candlesticks, pending formations, bearish blockers;
* lookahead self-test for all 27 strategies at five truncation points, and a deliberately leaking strategy that must fail;
* overfitting statistics (PBO on noise vs a real edge, deflated Sharpe, Benjamini-Hochberg), sizing, Markov regime,
  HAR-RV, higher-timeframe alignment, meta-labeler blocks;
* offline client, point-in-time cuts, closed-candle rule, resampling;
* the full pipeline: audit → report → logs → review → replay → scanner → watch loop → dashboard → CLI;
* the hosted app: login wall, failed-login throttle, cross-site POST refusal, path traversal, job queue / cancel /
  restart recovery, candle-close scheduler, connectivity report, a real demo audit driven through the web routes;
* the deployment files agree with the code: `render.yaml` vs the environment variables the app reads, the Dockerfile vs the
  launcher and port, and every third-party import in `audit/` is declared in the hosted image's requirements.

`python audit/audit.py --demo` runs one complete audit on synthetic data; GitHub Actions runs both on every push.

## Safeguards against fooling yourself

- Only **closed** candles are used; signals decided on a candle's close, filled at the next candle's open.
- Higher-timeframe values are aligned by close time; the market layer and the Markov filter never trigger, only block.
- Walk-forward only, with an embargo; newest 15% held out and scored once; parameters chosen on past folds.
- Overfitting is measured, not assumed away: PBO, deflated Sharpe, neighbourhood stability, top-5% concentration,
  2× costs, library-wide false-discovery control.
- Slippage is checked at your size against the live book, not assumed constant.
- Every audit and signal is logged and graded later; live drift blocks a strategy; the whole audit can be replayed
  point-in-time to see how its verdicts would have done.
- Freqtrade side: lookahead-analysis and recursive-analysis (`startup_candle_count` 400), FreqAI `"shuffle": false`.

## Known limits

- Live endpoints (CoinGecko, DefiLlama, FRED, Binance futures) are exercised with offline stand-ins in the tests;
  the market-cap indices use current circulating supply (read them for direction, not exact levels); the dollar index
  is FRED's broad dollar, not ICE's DXY.
- Patterns not implemented: Adam & Eve, bump and run, inverse cup & handle. Not available free: liquidation maps,
  unlock feeds, sector performance (add events to `audit/events.csv`).
- A full 27-strategy audit on 3000 candles takes roughly 10–20 seconds of CPU and about 500 MB of memory (measured on
  synthetic data; the first run of a coin also downloads its candles from Binance). Use `--strategies` to narrow the
  library, `--no-ml` to skip the meta-labeler, and `--jobs` for scans.
- Bearish patterns cannot be traded on spot; there is no short or futures execution anywhere. Live trading is not
  enabled: going live needs weeks of stable dry-run, a trading-only IP-restricted key and `"dry_run": false`.

## Deliberately not included

Copy-trading / smart-money wallet trackers and new-token sniping (need on-chain/DEX feeds), raw
NostalgiaForInfinity / community strategy files (ideas re-implemented and tested module by module), martingale /
unlimited DCA, cross-exchange arbitrage, market-making and funding-carry execution, Jesse (its Monte Carlo and
significance tests are reimplemented here), withdrawal-enabled API keys.

## Tuning

`audit/settings.toml` (costs, gates, risk caps, weights, verdict thresholds, scan universe, meta-labeler, notify) ·
strategy grids: `space` in `audit/strategies.py` · sweep grid: `GRID` in `research/sweep_vectorbt.py` · FreqAI:
`freqtrade/user_data/config-freqai.json` and `ENTRY_PROB` / `TP_ATR` / `SL_ATR` / `HORIZON` in `CoinAuditFreqAI.py`.

## Version history

| Version | Input | What was added |
|---|---|---|
| v6.1 | "host it on Render or Vercel" | Password-protected web app (FastAPI) with job queue, scheduler, report viewer, connectivity check; Docker image, Render Blueprint, smoke test, DEPLOY.md; audits now exit non-zero on failure; measured resource needs |
| v6 | "make sure it has all these and enhance it" | Offline test-suite and synthetic data, 12 gates (PBO, deflated Sharpe, FDR), Kelly + slippage-at-size, Markov regime + HAR-RV, calibrated/conformal meta-labeler, scanner + allocation, watch loop + notifications + dashboard, `--as-of` replay and verdict backtest, signal grading feedback loop, settings file, kline cache, CI, five detector bugs fixed |
| Mac | "create it for mac too" | 13 double-click launchers, Python finder, libomp, Gatekeeper instructions |
| v5 | Chart-pattern list + macro/dominance brief | 15 pattern modules, pivot engine, bearish research, 21 candlesticks, pending formations; TOTAL/dominance rebuild, FRED macro, 8 regimes, eligibility score, derivatives fields, events.csv, squeeze, DCA |
| v4 | Freqtrade/X strategy review | SMA Offset, BinCluc, ElliotV, confluence, EMA cross + funding, ICT sweep + FVG, deep drawdown reclaim, narrative phase; exits and protections; benchmarks; stale-data switch; closed-candle fix; 5 Freqtrade strategies |
| v3 | GitHub strategy evidence review | Strategy library with one interface, 10 gates, evidence levels, regime classifier, research analyses, meta-labeler, signal record, timeframe prompt |
| v2 | "Use the recommended repos, all free" | Official Binance SDK, pandas-ta-classic, LightGBM, quantstats, VectorBT, binance-public-data, Freqtrade + FreqAI, lookahead + recursive analysis |
| v1 | Repository map + "create agent or bot" | Standalone read-only audit, trend backtest, walk-forward ML, trade plan, CSV log, `--review` |

Research tool, not financial advice. Backtests and paper trading do not guarantee future results.
