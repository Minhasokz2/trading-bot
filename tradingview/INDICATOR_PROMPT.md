# Coin Audit · 27-strategy TradingView indicator — the master prompt

This file describes, in full detail, every strategy and every rule of the Coin Audit Bot as they run inside the
TradingView indicator `tradingview/coin_audit_library.pine` (v2). It has two uses:

1. **Understand the indicator.** Every number on the dashboard, every label and every line on the chart is explained
   here: what it measures, the exact rule, and where it differs from the Python bot.
2. **Build, rebuild or extend it with an AI or a Pine developer.** Everything between `PROMPT START` and `PROMPT END`
   is a self-contained specification. Paste it into ChatGPT / Claude / Gemini (or hand it to a developer) together
   with the request, for example *"build this indicator"*, *"add strategy 28 that does X following the same
   conventions"*, *"turn strategy 13 into a `strategy()` script for the Strategy Tester"*, or *"explain why the
   dashboard says BLOCKED here"*. The ready-made indicator already implements all of it, so to just use it: open
   TradingView → Pine Editor → paste `coin_audit_library.pine` → **Add to chart** (see `tradingview/README.md`).

The indicator is a **read-only research tool**. It never places orders and it is not financial advice. The bot's
verdict (12 walk-forward gates, order book, taker flow, funding, ML filter) is stronger evidence than any chart test.

---

`PROMPT START`

## 1. Role and goal

You are an expert Pine Script **v6** developer and a systematic crypto trader. Build (or modify) **one overlay
indicator** for TradingView called **"Coin Audit · 27-strategy library v2"** (short title `CoinAudit27`) that ports a
Python research bot to the chart:

* 27 long-only spot strategies (12 rule strategies, 9 chart-pattern strategies, 6 price-action strategies), each
  with its own entry, exit, stop, target, time limit, trailing stop and limit-order rules;
* a pivot engine with confirmed breakouts, and 5 bearish patterns that block longs;
* the coin's regime (trend / range / transition, volatility, BTC risk-on / off, liquidity) that decides which
  strategies may trade;
* the crypto-wide and macro market layer (8 market regimes, an altcoin / BTC eligibility score and a gate);
* a paper-trading backtest of every strategy on the loaded chart history with the bot's exact exit mechanics and
  costs, and gates that decide whether a strategy is VALIDATED;
* an approval chain (validated → regime fits → nothing blocks → APPROVED);
* the bot's weighted checks, a 0-100 score, a verdict (FAVORABLE / WATCHLIST / NEUTRAL / AVOID) with its caps;
* the bot's trade plan (entry zone, stop, T1 = 1.5R, T2 = 3R, position size) and a plain-language "what to do now";
* a clean, theme-aware UI: a dashboard table (Full and Compact), chart labels with tooltips, pattern drawings,
  a plan projection to the right of the last candle, data-window values and alerts.

It must never repaint, never use future data, never place orders, and must compile in Pine v6 without errors or
warnings.

## 2. Hard rules (non-negotiable)

1. `//@version=6`, `indicator(..., overlay = true, max_labels_count = 500, max_lines_count = 500, max_boxes_count = 200)`.
   No `strategy()` calls, no order functions.
2. **Closed candles only.** Every decision (signals, approval, verdict, plan) is taken on a confirmed candle. On the
   forming (realtime) candle every decision value is read one candle back:
   `f_clf(float x) => barstate.isconfirmed ? x : x[1]` (same for bool and string). New signals on the forming candle
   are only listed as "forming candle (not confirmed yet)". Labels, markers and alerts fire only when
   `barstate.isconfirmed`.
3. **Higher timeframes without lookahead.** For a timeframe HIGHER than the chart: `request.security(sym, tf,
   f(simple int off = 1), lookahead = barmerge.lookahead_on)` where the function returns `value[off]` — this is the
   last CLOSED higher-timeframe candle and never changes. For the same or a lower timeframe use `off = 0` and
   `lookahead_off`. Compute `off` and the lookahead constant from `timeframe.in_seconds()` so they are `simple`.
4. **Lazy `and` / `or`.** Pine v6 stops evaluating `and` / `or` as soon as the result is known, and evaluates only
   the taken branch of `?:`. Never place a stateful call (`ta.*`, `math.sum`, `request.*`, or a user function that
   reads `x[1]` of its argument) after `and`, `or`, `?` or `:` — compute it into a global variable first and use the
   variable. Never call stateful functions inside `if` / `for` blocks either.
5. **Line wrapping.** A wrapped line must be indented by a number of spaces that is NOT a multiple of 4 (use 5).
6. **Typed `na`.** Inside `array.from` write `float(na)` / `int(na)`, never a bare `na`.
7. **Limits.** At most 40 `request.*` calls (this design uses 22), 64 plot-type calls (12), 500 labels / lines,
   200 boxes. History references with a computed offset need `max_bars_back(series, N)`; this design uses 500 for
   high, low, close, open, atr, rsi, ema50 and bbw, and 1500 for atrPct (it feeds `ta.percentrank` over a year).
8. **Tables.** Create the table once (`var table`), merge cells once on `barstate.isfirst` (the layout depends only
   on inputs), and fill cells only on `barstate.islast`. Never merge on a realtime tick.
9. **Missing data degrades gracefully.** Every market symbol is requested with `ignore_invalid_symbol = true`;
   missing BTC data counts as risk-on (as in the bot); a missing market layer drops its weight from the score.
10. Text everywhere is plain English a trader understands; every dashboard cell, label and chip has a tooltip that
    explains the number and the rule behind it.

## 3. Core series (computed on the chart timeframe)

| Name | Definition |
|---|---|
| EMA 20 / 50 / 200 | `ta.ema(close, n)` |
| RSI | `ta.rsi(close, 14)`; RSI(4) for strategy 06 |
| ATR, ATR % | `ta.atr(14)`, `atr / close` |
| ADX, +DI, −DI | `ta.dmi(14, 14)` |
| MACD | `ta.macd(close, 12, 26, 9)`; the histogram is used |
| Bollinger 20 / 2 | `ta.bb(close, 20, 2)`; width `bbw = (upper − lower) / middle`; Bollinger 40 / 2 for strategy 07 |
| Volume ratio | `volume / ta.sma(volume, 30)` (base volume: quote volume and taker-buy volume do not exist on TradingView) |
| EWO (Elliott wave oscillator) | `(ta.ema(close, 5) − ta.ema(close, 35)) / close × 100` |
| Quote volume | `volume × hlc3` per candle; **24h volume** = its sum over the candles of one day (`86400 / timeframe.in_seconds()`) |
| Liquidity OK | 24h volume ≥ *Minimum 24h volume* (default $1,000,000) |
| Is BTC | `syminfo.basecurrency == "BTC"` |

## 4. Multi-timeframe context (each through one `request.security`, last closed candle)

* **Coin, daily:** RSI 14, ATR % (ATR 14 / close), 90-day maximum drawdown of the close (running peak over the last
  90 daily closes), 30-day and 90-day return (90 = `min(90, bar_index)`), 7-day / 30-day volume ratio
  (`sma(volume × hlc3, 7) / sma(volume × hlc3, 30)`), 7-day change.
* **BTC, daily** (input *BTC symbol*, default `BINANCE:BTCUSDT`): **risk-on** = close > long EMA (EMA200, or EMA50 when
  EMA200 has no value yet) **and** EMA20 > EMA50; 30-day and 90-day return; 7-day change; "broke support" = close under
  the lowest close of the prior 30 days.
* **BTC on the chart timeframe:** its close (for strategy 04's relative momentum).
* **Trend points per timeframe** (daily, 4h, 1h — the bot's `_tf_trend`): +25 close > EMA50, +25 EMA50 > long EMA
  (EMA200; EMA50 when there is no EMA200, which then scores 0), +20 EMA50 above its value 5 candles ago, +30 when
  ADX ≥ 20 with +DI > −DI, or +12 when ADX < 20 (no strong trend either way).
* **Next higher timeframe** (≤15m → 1h, ≤1h → 4h, ≤4h → D, ≤D → W, else M): its last closed close and EMA200 (strategy
  05's trend filter).

**BTC filter switch.** `btcOn = not useBtc or btcRiskOn`. When the input is on (default, as in the bot) BTC risk-off
switches off the strategies that need BTC (01 05 06 07 08 09 11 24 26), the regimes that need it, and caps the
verdict at WATCHLIST. When the input is off, BTC is treated as risk-on everywhere (the chips still show its state).

## 5. The coin's regime (`regime.classify` and `regime.matches`)

* **Efficiency ratio** (Kaufman, 20): `|close − close[20]| / Σ|close − close[1]|` over 20 candles.
* **Trend state:** `trend` if ADX ≥ 22 or efficiency ≥ 0.35; `range` if ADX < 18 and efficiency < 0.25; otherwise
  `transition`.
* **Direction:** up when close > EMA50, else down.
* **Volatility state:** percent rank of ATR % over the trailing year (`min(1 year of candles, 1500)`):
  `high` ≥ 0.8, `low` ≤ 0.2, else `normal`. (The bot also uses a Markov-switching filter; not available here.)
* **What each strategy needs (`regime_required`) and when it fits** — every fit also needs Liquidity OK:

| Requirement | Fits when |
|---|---|
| `trend` | trend state is trend or transition, direction up, BTC on |
| `trend_pullback`, `momentum`, `dip` | BTC on, volatility not high |
| `range` | trend state is range, volatility not high |
| `any` | always (with liquidity) |

## 6. The market layer (`market.classify` and `market.gate`) — 15 % of the verdict

Inputs (group ⑤, each `ignore_invalid_symbol = true`): `CRYPTOCAP:TOTAL`, `TOTAL2`, `TOTAL3`, `BTC.D`, `USDT.D`,
`BINANCE:ETHBTC`, `TVC:DXY`, `TVC:VIX`, `NASDAQ:NDX`, plus `BINANCE:<coin>BTC` when that pair exists. The layer is
"available" when the input *Market layer* is on and TOTAL returns data.

**Facts** (4h values = last closed 4h candle; daily = last closed daily candle):

* BTC 4h *bullish* = close > EMA200 **and** the last two confirmed 4h pivot highs AND lows are rising (pivots 3/3).
* *Above trend* of a series = close > its EMA200 (4h) — TOTAL, TOTAL2, TOTAL3.
* *Falling* BTC.D / USDT.D = EMA20 below its value 5 candles ago on **both** 4h and daily; *rising* = on neither.
* TOTAL3 *breaks resistance on volume* (4h) = highest close of the last 6 candles > highest close of the 50 before,
  and the average volume of the last 6 > 1.2 × the average of the 54 before.
* ETH/BTC *bullish* (daily) = close > EMA50 and EMA50 rising (vs 5 candles ago); the same test on the coin/BTC pair.
* Dollar OK = DXY not above its EMA50 **or** its EMA20 falling. Dollar *breaks higher strongly* = close above the
  highest close of the prior 60 days **and** +2 % over 20 days.
* VIX = last daily close. NDX above trend = daily close > EMA50.
* *Broke support* (daily): BTC or TOTAL3 close under the lowest close of the prior 30 days. USDT.D *broke higher* =
  close above the highest close of the prior 30 days.
* BTC, TOTAL, TOTAL3 7-day changes (daily, in %). High liquidity = 24h volume ≥ $20M (the bot also needs a spread ≤ 10 bps).

**Altcoin-long eligibility score** (clamped 0-100):

| Condition | Points |
|---|---|
| BTC above 4H EMA200 with bullish structure | +15 |
| TOTAL2 above 4H EMA200 | +10 |
| TOTAL3 above 4H EMA200 | +15 |
| TOTAL3 breaks resistance with volume | +10 |
| BTC.D falling on 4H and 1D | +10 |
| USDT.D falling on 4H and 1D | +10 |
| ETH/BTC bullish | +10 |
| Dollar falling or below its trend | +5 |
| Coin/BTC pair bullish (not for BTC) | +10 |
| High liquidity | +5 |
| BTC breaks its 30-day low close | −25 |
| TOTAL3 breaks support | −20 |
| USDT.D breaks higher | −15 |
| Dollar breaks higher strongly | −10 |
| (bot only) macro / news event within 48 h | −10 |

**BTC score** (used when the chart symbol is BTC): BTC bullish 4H +30, TOTAL above 4H EMA200 +15, USDT.D falling +20,
dollar OK +10, VIX < 20 +10, Nasdaq 100 above trend +10, high liquidity +5, BTC breaks support −25, USDT.D breaks
higher −15, dollar breaks higher −10 (bot only: event −10). Clamped 0-100.

**The 8 regimes, first match wins:**

| # | Regime | Condition | Behaviour shown |
|---|---|---|---|
| 1 | High-volatility event | BTC 4h ATR % ≥ its 95th percentile of the last 500 candles, or BTC moved > 8 % in 6 × 4h, or VIX > 35 | Pause automatic entries; every signal needs manual approval. |
| 2 | BTC selloff | BTC 7-day < −10 % and TOTAL 7-day < −10 % | Disable altcoin longs; watch liquidation and volatility. |
| 3 | Risk-off / stablecoin rotation | USDT.D rising and TOTAL 7-day < 0 | BTC longs only, and only fully validated ones. |
| 4 | Altcoin rotation | BTC.D falling, USDT.D falling and TOTAL3 7-day > BTC 7-day + 3 | Selective ETH / alt longs, ranked by relative strength. |
| 5 | Broad risk-on | BTC bullish 4H, TOTAL3 above trend, BTC.D not rising, USDT.D falling | BTC, ETH and high-quality alt long setups are enabled. |
| 6 | BTC-led rally | BTC bullish 4H, BTC.D rising, TOTAL3 7-day < BTC 7-day | Prefer BTC; be selective with alts. |
| 7 | BTC recovery, alts weak | BTC closed above its 4H EMA200 in the last 30 candles after > 50 % of the 30 before were under it, and TOTAL3 not above trend | Trade BTC carefully; wait for alt confirmation. |
| 8 | Range / chop | BTC 4h ADX < 18 and \|BTC 7-day\| < 3 % | Range, dip and mean-reversion modules only; no breakout / trend entries. |
| – | Mixed | none of the above | No dominant regime; rely on per-strategy validation and the eligibility score. |

**The market gate** (input *Market-regime gate*, only when the layer is available) blocks an approval when:
High-volatility event (always); BTC selloff and the coin is not BTC; Risk-off and (the coin is not BTC or the
strategy is not validated); Range / chop and the strategy's family is not a range family (range, dip, mean reversion,
channel, candlestick → strategies 06 07 08 16 22 23 27); for altcoins the score < 50, or < 70 and the strategy is not
validated; for BTC the BTC score < 40.

## 7. Pivot engine, structure and the breakout trigger (`patterns.py`)

* **Pivot (K = 3).** The candle 3 bars ago is a pivot high when its high is strictly higher than every other high in
  the 7-candle window around it (ties do not count); a pivot low likewise with lows. It is KNOWN only 3 candles later,
  so it is pushed (bar index, price) into arrays of confirmed pivots at that moment (keep the last 80 of each).
* **Market structure:** the last two confirmed pivot highs AND the last two pivot lows both rising = `bullish`, both
  falling = `bearish`, otherwise `mixed` (`unknown` with fewer than two of each).
* **Equality tolerance** at a bar: `clamp(0.5 × ATR %, 0.5 %, 1.5 %)` of price.
* **Prior downtrend** at bar i: the highest high of the 30 candles up to i is ≥ 3 ATR above the low of i, and the close
  of i ≤ EMA50. **Prior uptrend**: the low of the 30 candles is ≥ 3 ATR under the high of i, and the close ≥ EMA50.
* **A setup** = level (flat price or a sloped line `p0 + slope × (bar − i0)`), stop (invalidation), target, start bar,
  first bar it may trigger ("armed from") and expiry. Each pattern strategy keeps its newest armed setup.
* **Bullish trigger (`patterns.trigger`), evaluated on every closed candle while armed:**
  1. a close at or under the stop, or passing the expiry → the setup is void;
  2. a close at or under the level → keep waiting;
  3. the **first** close above the level decides everything (the setup is consumed either way):
     * volume ratio ≥ *Breakout volume* (default 1.2×; V-bottom never below 1.3×), else rejected;
     * **break of structure** (patterns that need it): the newest confirmed pivot high made after the pattern started
       must be cleared by this close, else rejected;
     * entry reference = the close (entry *close*) or level + 0.1 ATR (entry *retest*: a limit order there, valid 5
       candles); stop = the pattern stop (stop *pattern*) or `max(pattern stop, level − 1.5 ATR)` (stop *tight*);
       target = the pattern target, or reference + 2 × risk when the pattern has none;
     * reward : risk = (target − reference) / (reference − stop) must be ≥ *Pattern reward : risk* (1.5), else
       rejected.
* **Bearish trigger** (blockers only — spot cannot short): void on a close at or above its stop or after expiry; the
  first close under the level completes it when the volume ratio ≥ the breakout volume.
* **Drawing:** while armed, a dashed line (or box) in the *Setup* colour along the level; when it fires it turns solid
  in the *Bull* colour (bearish: *Bear* colour) and stays on the chart; when it is voided or expires it is deleted.

## 8. The 27 strategies

Conventions used below:

* **State strategy** — the entry condition can stay true for many candles. The dashboard shows `on` while it holds
  and `FIRED` on the candle it switches on; the chart backtest enters whenever it holds and the strategy is flat
  (so it re-enters after an exit and its cooldown, exactly like the bot). **Event strategy** — true on one candle.
* **Fallback stop** — used when the strategy has no stop price of its own: `fill − N × ATR(signal candle)`.
  A strategy's own stop is always kept at least 0.1 % under the fill.
* **ROI ladder** — exit when the high reaches `fill × (1 + x)` where x depends on how many candles the trade is
  old. **Trailing (a, d)** — once the highest high since entry is ≥ `fill × (1 + a)`, the stop rises to
  `peak × (1 − d)`. **Time limit** — exit at the open after N candles.
* **Exit signal** — a state strategy's exit condition on a closed candle sells at the next open.
* "Bot tests" = the parameter grid the Python bot walk-forward-optimises; the indicator's default is one of them.
* **BTC** = needs BTC risk-on (`btcOn`). **Chop OK** = still allowed by the market gate in the Range / chop regime.

### Rule strategies (01-12)

**01 · Trend baseline EMA50/200 + ADX** — family trend · evidence B · regime `trend` · BTC · state
* Entry: close > EMA50 > EMA200, RSI > 50, ADX > *ADX at least* (20; bot tests 15 / 20 / 25), +DI > −DI.
* Exit signal: close < EMA50. Fallback stop 2 ATR (bot tests 1.5 / 2 / 3). No target, no time limit.
* Origin: the Freqtrade CoinAuditTrend rules — the non-ML benchmark of the library.

**02 · Multi-horizon MA ensemble** — trend · A/B · `trend` · state
* One vote per SMA 20, 50 and 100: price above it AND the SMA above its value 3 candles ago.
* Entry: votes ≥ *votes* (2; bot tests 2 / 3, and the lengths × 0.5 / × 1 / × 2). Exit signal: votes ≤ 1.
* Fallback stop 3 ATR. (The bot sizes it by volatility target — trailing 1-year median ATR % / ATR %, clipped
  0.25-1 — the chart test uses full size.)

**03 · Donchian breakout** — trend · A/B · `trend` · state
* Entry: close > the highest high of the prior N candles (N = 20; bot tests 20 / 55 / 100).
* Exit signal: close < the lowest low of the prior `max(5, N / 2)` candles. Fallback stop 2 ATR (bot tests 2 / 3).
  Volatility-target sizing in the bot, as 02.

**04 · Volatility-managed momentum** — momentum · A · `momentum` · state
* `mom = close / close[L] − 1` (L = 90; bot tests 30 / 90 / 180); `rv` = standard deviation of 1-candle returns over 30;
  `score = mom / (rv × √L)`; relative momentum = mom − BTC's L-candle return (BTC itself: +1e-9); crash = rv above its
  90th percentile over the trailing year (max 1000 candles).
* Entry: score > *min score* (0; bot tests 0 / 0.5), relative momentum > 0, no crash.
* Exit signal: mom < 0, or relative momentum < 0, or crash. Fallback stop 3 ATR. (Volatility-target sizing in the bot.)

**05 · Trend pullback (NFI-inspired modules)** — pullback · C · `trend_pullback` · BTC · state
* Six modules, all required: (1) trend filter — the next higher timeframe's last closed close > its EMA200
  (fallback: close > EMA200); (2) BTC risk-on; (3) safe dip — RSI < *RSI below* (35; bot tests 30 / 35 / 40) and
  (EMA20 − close) / ATR ≥ 1; (4) pump guard — 24-candle high / low − 1 < 8 × ATR %; (5) volume — volume ratio > 0.7
  (the bot also wants the taker-buy share of the last 3 candles > 42 %: not available on TradingView); (6) EWO guard —
  EWO above its 5th percentile of the last 200 candles.
* Exit signal: close > EMA20 or RSI > 65. Fallback stop 2.5 ATR (bot tests 1.5 / 2.5). Time limit 24 candles.
* The bot also runs an ablation: each module is kept only if removing it lowers the median walk-forward fold return.

**06 · SMA offset (NotAnotherSMAOffset, HO/Protect)** — dip · C · `dip` · BTC · chop OK · state
* A: close < SMA14 × *offset* (0.975; bot tests 0.96 / 0.975 / 0.99), EWO > *EWO above* (4; bot tests 2 / 4),
  RSI < 50, RSI(4) < 35. B: close < SMA14 × offset, EWO < −8, RSI(4) < 25.
* Entry: (A or B), BTC on, pump guard (as 05). Exit signal: close > SMA20 × 1.01 and RSI > 50.
* Fallback stop 3 ATR · time limit 120 · trailing (2 %, 1 %) · ROI: +8 % (held 0-29), +3 % (30-59), +0.5 % (60+).

**07 · BinCluc Bollinger dip (BinHV45 + ClucMay)** — dip · C · `dip` · BTC · chop OK · state
* BinHV45: the previous lower BB40 > 0, band delta `|mid40 − lower40|` > 0.8 % of the close, `|close − close[1]|` >
  1.75 % of the close, tail `|close − low|` < 0.25 × delta, close < the previous lower BB40, close ≤ previous close.
* ClucMay: close < EMA50, close < *cluc ×* (0.985; bot tests 0.975 / 0.985 / 0.99) × lower BB20, volume < 20 × its
  30-candle average.
* Entry: (BinHV45 or ClucMay) and BTC on. Exit signal: close > BB20 middle.
* Fallback stop 2 ATR (bot tests 2 / 3) · time limit 72 · ROI: +5 % (0-23), +2 % (24-47), 0 % (48+).

**08 · Elliot EWO offset (ElliotV5/V8 style)** — dip · C · `dip` · BTC · chop OK · state
* A: close < EMA17 × *offset* (0.978; bot tests 0.97 / 0.978 / 0.985), EWO > *EWO above* (3; bot tests 3 / 5.6), RSI < 65.
  B (capitulation): close < EMA17 × offset, EWO < −19.
* Entry: (A or B) and BTC on. Exit signal: close > EMA49 × 1.006.
* Fallback stop 3 ATR · time limit 120 · trailing (3 %, 1 %) · ROI: +20 % (0-29), +4 % (30-89), 0 % (90+).

**09 · Confluence score (TrendRider style)** — trend · C · `trend` · BTC · state
* Six parts, one point each: EMA20 > EMA50 > EMA200 · RSI 50-70 · ADX > 20 with +DI > −DI · volume ratio > 1.2 ·
  close > SMA20 with BB width above its value 5 candles ago · MACD histogram > 0 and rising.
* Entry: points ≥ *parts* (4; bot tests 4 / 5) and BTC on. Exit signal: points ≤ 2.
* Fallback stop 2 ATR (bot tests 2 / 3) · time limit 60 · trailing (5 %, 2.5 %) · early-loss cuts, sold at the next
  open when the previous close is ≥ 1 ATR (entry ATR) under the fill after 3 candles, ≥ 0.5 ATR after 6, or not above
  the fill after 12 ("dead fish").

**10 · EMA cross (btc-strategy-lab winner)** — trend · B · `any` · state
* In while EMA fast (21; bot tests 12 / 21) > EMA slow (55; bot tests 55 / 100); exit signal on the cross back.
* The bot also requires the 3-day average funding rate under 0.01 % / 0.03 % (crowded longs stand aside) — there is no
  funding data on TradingView, so the filter is skipped. Fallback stop 3 ATR.

**11 · ICT liquidity sweep + fair-value gap** — smart money · C/D · `any` · BTC · event
* Swing = lowest low of the prior N candles (20; bot tests 20 / 40). Sweep = low < swing and close > swing. FVG =
  low > the high 2 candles ago on a green candle.
* Entry: FVG, a sweep within the last 5 candles (this one included), BTC on.
* Limit order at 50 % of the displacement `(lowest low of 6 + high) / 2`, valid 3 candles; stop = lowest low of 6 −
  0.1 ATR; target = *target R* (2; bot tests 2 / 3) × risk; time limit 30.

**12 · Deep drawdown reclaim** — rebound · D · `any` · state
* Drawdown = close / the highest high loaded on the chart − 1 ≤ −*below ATH by* (0.85; bot tests 0.80 / 0.85 / 0.90),
  close > the highest high of the prior 20 candles (reclaim), at least 90 candles of history.
* Stop −25 % · target +*target* (100 %; bot tests 50 % / 100 %) · time limit 90. (Narrative / community confirmation
  from the original idea is replaced by the price reclaim.)

### Chart-pattern strategies (13-21)

Common to all nine: event strategies built on the pivot engine and fired by the trigger of section 7 (volume, break
of structure where noted, reward : risk ≥ 1.5, entry *close* or *retest*, stop *pattern* or *tight*; the bot tests
volume 1.2 / 1.5, both entries and both stop modes). Time limit 40 candles; a retest limit order is valid 5 candles.
The stop and target come from the pattern. Each keeps its newest armed formation and draws it.

**13 · Double / triple bottom (W)** — chart pattern · C · `any` · break of structure
* On each new pivot low take the last 3 (triple) or 2 (double) pivot lows: each ≥ 5 candles after the previous one,
  the whole span ≤ 150 candles, `(highest − lowest) / mean ≤ tolerance` (at the last low).
* Neckline = the highest pivot high strictly between the first and the last low; it must be ≥ 2 ATR (ATR at the last
  low) above the highest of the lows. A prior downtrend at the first low.
* Level = neckline · stop = lowest low − 0.2 ATR · target = neckline + (neckline − lowest low) · armed from the last
  low + 3 candles · expires after another (last − first low) candles. A triple bottom wins over a double.

**14 · Inverse head & shoulders** — chart pattern · C · `any` · break of structure
* Three consecutive pivot lows LS, H, RS: RS − LS ≤ 150 candles, ≥ 3 candles between each; the head is under both
  shoulders by ≥ 0.5 ATR (ATR at the head); the shoulders are level: `|LS − RS| / H ≤ 2 × tolerance`.
* N1 = highest pivot high between LS and H, N2 = between H and RS, `|N2 − N1| ≤ 3 ATR`. A prior downtrend at LS.
* Level = the (sloped) neckline through N1 and N2 · stop = RS − 0.2 ATR · target = the neckline at RS + 3 + head depth
  (the neckline above the head minus the head) · armed from RS + 3 · expires 25 candles later.

**15 · Range breakout (rectangle)** — chart pattern · C · `any` · break of structure
* On each new pivot: the pivot highs and lows of the last 50 candles (≥ 2 of each); top = the highest pivot high,
  bottom = the lowest pivot low; ≥ 2 highs within `max(tolerance × top, 0.3 ATR)` of the top and ≥ 2 lows of the
  bottom; height 3-14 ATR; no close outside [bottom − 0.5 ATR, top + 0.5 ATR] since the first pivot.
* Level = top · stop = middle of the range · target = top + height · expires 25 candles. Drawn as a box.

**16 · Range support** — range · C · `range` · chop OK · event (no trigger)
* While a validated box of 15 holds (until it expires or a close goes 0.5 ATR outside it): a green candle with a
  higher close, its close in the bottom *buy zone* (30 % of the height; bot tests 20 / 30 %) and its low ≥ bottom −
  0.5 ATR. Never in the middle of the range.
* Stop = bottom − 0.5 ATR · target = top − 0.1 × height · time limit 40.

**17 · Bull flag / pennant / high tight flag** — continuation · C · `trend` · no break-of-structure check
* Pole = a candle that is the highest high of 13 with (high − lowest low of 13) ≥ 4 ATR.
* Flag = the candles after the pole. From the 3rd to the 15th candle after it, as long as the flag has not retraced
  more than 50 % of the pole and has not made a high above the pole, a close above the flag's highest high (the
  candles before this one) triggers. A new qualifying pole restarts the flag.
* Stop = the flag's lowest low − 0.2 ATR · target = flag high + pole. (Names in the bot: high tight flag = retrace
  ≤ 25 % and pole ≥ 8 ATR; pennant = flag highs falling and lows rising; otherwise flag.)

**18 · Triangle / falling wedge** — continuation · C · `any` · no break-of-structure check
* On each new pivot: the last 2-3 pivot highs and lows, each within 60 candles; a least-squares line through each set;
  the largest residual of both lines ≤ 0.6 ATR; the width (upper − lower) > 0 at the start and now.
* Contracting = width now < 0.85 × width at the start AND BB width now ≤ BB width at the start. With span = now −
  start, "flat" = the line moved less than 1 ATR over the span:
  ascending triangle (upper flat, lower up > 1 ATR) · symmetrical (upper down > 1 ATR, lower up > 1 ATR) · falling
  wedge (both falling, the upper line steeper) → **long setups**; descending triangle (lower flat, upper down) and
  rising wedge (both rising, the lower line steeper) → **bearish blockers**; the symmetrical triangle arms both;
  not contracting, width change < 0.8 ATR and the lower line up > 1 ATR → **ascending channel** (strategy 22).
* Long: level = the sloped upper line · stop = the lower line now − 0.2 ATR · target = the upper line now + the
  starting width · expires 25 candles. Both lines are drawn.

**19 · Rounding bottom (U) / cup & handle** — chart pattern · C · `any` · break of structure
* A least-squares parabola `y = a x² + b x + c` through the last 60 closes with x evenly spaced from −1 to 1 (the
  bot's `numpy.polyfit`): it opens upward (a > 0), fits well (R² ≥ 0.7) and its bottom is in the middle
  (`|−b / 2a| ≤ 0.4`). Left rim = the highest high of the window's first 10 candles; depth = rim − the window's lowest
  low ≥ 3 ATR; volume dries up and returns (the average of the middle 20 candles < the first 20, and the last 20 >
  the middle 20).
  Closed form for x symmetric (Σx = Σx³ = 0): `a = (T2 − S2·T0/60) / (S4 − S2²/60)`, `b = T1 / S2`,
  `c = (T0 − a·S2) / 60`, R² = `1 − (Σy² − a·T2 − b·T1 − c·T0) / (Σy² − T0²/60)` with `T0 = Σy`, `T1 = Σxy`,
  `T2 = Σx²y`, `S2 = Σx²`, `S4 = Σx⁴` (subtract the newest close from y first for precision).
* **U:** armed on the candle after the window; level = the left rim · stop = `max(window low, rim − 3 ATR)` ·
  target = rim + depth · expires 10 candles.
* **Cup & handle:** the same tests on the window that ended 4 candles ago, plus: the right side (highest high of the
  window's last 10 candles) is back within 25 % of the depth from the rim, and the 4 candles since (the handle) hold
  the top half of the cup (rim − handle low ≤ 50 % of the depth) on volume no higher than the window's first 20
  candles. Level = max(left rim, right rim) · stop = handle low − 0.2 ATR · target = rim + depth · expires 17 candles
  later (20 after the window).
* The same rim is never armed twice in a row.

**20 · V-bottom reclaim** — chart pattern · C/D · `any` · break of structure · volume ≥ 1.3×
* The previous candle is the lowest low of 11 candles and the current candle closes higher with a higher low (the
  bottom is only known then); the highest high of those 11 candles is ≥ 6 ATR above the low (a drop within 10
  candles); at most one V per 20 candles.
* Level = the low + 50 % of the drop (the reclaim) · stop = low − 0.2 ATR · target = the pre-drop high · expires 9
  candles after it is armed. Dead-cat filter: volume, break of structure and reward : risk must all pass.

**21 · Downtrend-line break** — chart pattern · C · `any` · break of structure
* On each new pivot high: a line through the last two pivot highs when they are ≥ 15 candles apart and the second is
  ≥ 1 ATR lower, and no close between them was above the line by more than 0.1 ATR; a pivot low must exist between
  them. Stop = that newest pivot low − 0.2 ATR; target = 2 × risk (no measured move); expires 30 candles after the
  second high is confirmed.
* The line stays alive until it expires or a close goes 0.1 ATR above it; every new pivot low confirmed under it
  re-arms the setup with that low − 0.2 ATR as the new stop (a lower low is normal inside a downtrend).
* The bot tests entry *retest* first for this one (buy the retest of the broken line).

### Price-action strategies (22-27)

**22 · Ascending channel bounce** — channel · C · `trend_pullback` · chop OK · event
* While an ascending channel from 18 is active (20 candles; it ends on a close 0.5 ATR under the lower line): a green
  candle whose low comes within *touch* ATR of the lower line (0.6; bot tests 0.3 / 0.6) and closes above it.
* Stop = lower line − 0.5 ATR · target = the upper line now · time limit 30.

**23 · Bullish candlestick at a key level** — candlestick · C · `any` · chop OK · event
* One of nine bullish candles (downtrend = close < SMA10):
  engulfing (green, previous red, close ≥ previous open, open ≤ previous close, bigger body) · hammer (downtrend,
  lower wick ≥ 2 × body, upper wick ≤ 0.3 × body) · inverted hammer (downtrend, upper wick ≥ 2 × body, lower wick ≤
  0.3 × body) · morning star (2 ago red, previous body ≤ 0.3 × that body, green, close above the middle of the candle 2
  ago) · piercing line (previous red, green, open < previous close, close above the previous middle but under its
  open) · three white soldiers (three green candles, rising closes) · tweezer bottom (downtrend, lows within 0.1 % of
  the close, previous red, green) · bullish harami (previous red, green, opens above the previous close, closes under
  the previous open, smaller body) · bullish marubozu (green, body ≥ 90 % of the range).
* AND at a level: the low within *within* ATR (0.3; bot tests 0.3 / 0.6) of a confirmed pivot low of the last 100
  candles, or the low at / under the lower Bollinger band (20, 2).
* Stop = low − 0.2 ATR · target = *target R* (1.5; bot tests 1.5 / 2) × risk · time limit 30.

**24 · SMC: sweep → break of structure + FVG → order-block retest** — smart money · C/D · `any` · BTC · event
* Sweep: from 4 candles after the newest confirmed pivot low and within 60 candles of it, a candle wicks under that
  low and closes back above it (one sweep per pivot low). Optional RSI divergence (bot tests both): RSI at the sweep >
  RSI at the pivot low. The newest confirmed pivot high is the break-of-structure level.
* Within the next 10 candles the first close above that level decides: it needs a bullish fair-value gap (low > high
  2 candles earlier) on one of the last 4 candles (not before the sweep + 2), and an order block = the newest red
  candle between the sweep and the break.
* Limit order at the order block's top (max of its open and close), valid 10 candles · stop = min(order-block low,
  sweep low) − 0.1 ATR · target = *target R* (2; bot tests 2 / 3) × risk · time limit 40.

**25 · Bullish RSI failure swing** — reversal · C · `any` · event
* RSI goes under 30; it comes back to ≥ 30 (start of the bounce) and makes a peak; within 30 candles of the bounce, at
  least 2 candles after the peak, RSI pulls back to a higher low that stays above 30 and is ≥ 5 points under the peak;
  then within 20 candles RSI closes above the peak while the previous RSI is above 30 → signal. Momentum failed to
  make a new low.
* Stop = the lowest low since RSI first went under 30 − 0.2 ATR · target = *target R* (1.5; bot tests 1.5 / 2.5) ×
  risk · time limit 40.

**26 · Squeeze breakout** — momentum · B/C · `momentum` · BTC · event
* Bollinger width at or under its lowest *x %* (20; bot tests 10 / 20 %) of the last 120 candles on any of the last 5
  candles, a close above the highest high of the prior 20 candles, volume ratio ≥ *volume ×* (1.3; bot tests 1.3 / 1.8),
  BTC on.
* Stop = lowest low of 10 − 0.2 ATR · trailing (5 %, 2.5 %) · time limit 40.

**27 · VWAP reversion** — mean reversion · C · `dip` · chop OK · event
* 50-candle rolling VWAP = Σ(hlc3 × volume) / Σvolume; z = (close − VWAP) / standard deviation of (close − VWAP) over
  50. The previous candle's z < −*z below* (2; bot tests 2 / 2.5) and the close is above the previous high.
* Stop = lowest low of 5 − 0.2 ATR · target = the VWAP · time limit 30.

### Strategy table (what the dashboard and the backtest use)

| # | Code | Family | Needs | Chop OK | Fallback stop | Time limit | Trailing | Limit valid | Own stop / target |
|---|---|---|---|---|---|---|---|---|---|
| 01 | EMA/ADX | trend | trend | – | 2 ATR | – | – | – | – / – |
| 02 | MA-ens | trend | trend | – | 3 ATR | – | – | – | – / – |
| 03 | Donchian | trend | trend | – | 2 ATR | – | – | – | – / – |
| 04 | VolMom | momentum | momentum | – | 3 ATR | – | – | – | – / – |
| 05 | Pullback | pullback | trend_pullback | – | 2.5 ATR | 24 | – | – | – / – |
| 06 | SMA-off | dip | dip | ✓ | 3 ATR | 120 | 2 % / 1 % | – | ROI 8 → 3 → 0.5 % |
| 07 | BinCluc | dip | dip | ✓ | 2 ATR | 72 | – | – | ROI 5 → 2 → 0 % |
| 08 | Elliot | dip | dip | ✓ | 3 ATR | 120 | 3 % / 1 % | – | ROI 20 → 4 → 0 % |
| 09 | Confl | trend | trend | – | 2 ATR | 60 | 5 % / 2.5 % | – | early-loss cuts |
| 10 | EMA-x | trend | any | – | 3 ATR | – | – | – | – / – |
| 11 | ICT | smart money | any | – | – | 30 | – | 3 | sweep − 0.1 ATR / 2R |
| 12 | DD-reclaim | rebound | any | – | −25 % | 90 | – | – | −25 % / +100 % |
| 13 | W-bottom | chart pattern | any | – | – | 40 | – | 5 | pattern |
| 14 | iH&S | chart pattern | any | – | – | 40 | – | 5 | pattern |
| 15 | Range-brk | chart pattern | any | – | – | 40 | – | 5 | pattern |
| 16 | Range-sup | range | range | ✓ | – | 40 | – | – | box |
| 17 | Flag | continuation | trend | – | – | 40 | – | 5 | pattern |
| 18 | Tri/Wedge | continuation | any | – | – | 40 | – | 5 | pattern |
| 19 | Cup | chart pattern | any | – | – | 40 | – | 5 | pattern |
| 20 | V-bottom | chart pattern | any | – | – | 40 | – | 5 | pattern |
| 21 | TL-break | chart pattern | any | – | – | 40 | – | 5 | pivot low / 2R |
| 22 | Channel | channel | trend_pullback | ✓ | – | 30 | – | – | lower − 0.5 ATR / upper line |
| 23 | Candle | candlestick | any | ✓ | – | 30 | – | – | low − 0.2 ATR / 1.5R |
| 24 | SMC | smart money | any | – | – | 40 | – | 10 | OB / 2R |
| 25 | RSI-FS | reversal | any | – | – | 40 | – | – | swing low / 1.5R |
| 26 | Squeeze | momentum | momentum | – | – | 40 | 5 % / 2.5 % | – | 10-low − 0.2 ATR / – |
| 27 | VWAP | mean reversion | dip | ✓ | – | 30 | – | – | 5-low − 0.2 ATR / VWAP |

## 9. Bearish patterns — long blockers (research only on spot)

Detected with the same engine and completed by the bearish trigger (first close under the level with ≥ the breakout
volume; void on a close at / above the stop or after expiry):

* **Double / triple top (M)** — the mirror of 13 (pivot highs within tolerance, neckline = the lowest pivot low
  between them ≥ 2 ATR under the lowest top, prior uptrend); stop = highest top + 0.2 ATR.
* **Head & shoulders top** — the mirror of 14 (head ≥ 0.5 ATR above both shoulders, sloped neckline through the lows
  between, prior uptrend); stop = right shoulder + 0.2 ATR; expires 25 candles.
* **Bear flag / pennant** — the mirror of 17 (pole = lowest low of 13 ≥ 4 ATR under the highest high of 13; 3-15
  candles retracing ≤ 50 % without a lower low; void on a close ≥ flag high + 0.2 ATR; completes on a close under
  the flag low).
* **Descending triangle, symmetrical triangle, rising wedge** — from 18; level = the sloped lower line; stop = the
  upper line + 0.2 ATR; expires 25 candles.
* **Rounding top** — the parabola of 19 opening downward (a < 0, R² ≥ 0.7, vertex in the middle), ≥ 3 ATR tall;
  level = the lowest low of the window's first 10 candles; stop = `min(window high, rim + 3 ATR)`; expires 10 candles.

A completed bearish pattern is **fresh** for 3 candles (the completion candle and the next two). While fresh (input
*Bearish patterns block longs*), every long approval is blocked with the reason "bearish <name> just completed".

## 10. Validation on the chart — every strategy is paper-traded (`validation.backtest`)

Run on every confirmed candle, for each of the 27 strategies independently (long / flat, one position each), with
the signals of the PREVIOUS closed candle (a signal on a close is filled at the next open — never on the same candle):

1. **In a position** (in this order):
   * trailing: if the highest high since entry (up to the previous candle) ≥ `fill × (1 + a)`, the stop becomes
     `max(stop, peak × (1 − d))`;
   * **stop** — low ≤ stop → exit at `min(stop, open)` (a gap down fills at the open);
   * else **target** — high ≥ target → exit at `max(target, open)`;
   * else **ROI ladder** (06, 07, 08) — high ≥ `fill × (1 + ROI for the candles held)` → exit at
     `max(open, fill × (1 + ROI))`;
   * else at the **open**: the previous candle's exit signal (only if it came after the entry candle), the time limit
     reached (`candles held ≥ limit`), or an early-loss cut (09) → exit at the open;
   * otherwise update the peak with this candle's high.
2. **Flat:**
   * a resting limit order fills when the low ≤ its price, at `min(open, limit)`; it expires after its validity;
   * a new entry needs: the previous candle's signal (state strategies: the condition holds), the ATR known, the
     cooldown over, and no exit on this same candle. With a limit price (11, 24, patterns on *retest*) it places a
     limit order valid N candles counted from the signal candle (it can fill on this candle; a new signal replaces a
     resting order); otherwise it buys at this open.
   * Entry: stop = the strategy's own stop (kept ≤ 99.9 % of the fill) → −25 % (12) → fallback N × ATR of the signal
     candle; target = R multiple × (fill − stop) (11, 23, 24, 25) → +x % (12) → the strategy's own target when it is
     above the fill. If the entry candle's low already reaches the stop, the trade is stopped on that candle.
3. **Protections:** after a stop-out, no new signal for 2 candles; 3 stop-outs within 48 candles pause the strategy
   for 24 candles (the bot's Freqtrade-style CooldownPeriod and StoplossGuard).
4. **Costs** (input *Cost per side*, default 0.15 % = 0.10 % fee + 0.05 % slippage): every trade's return is
   `exit / fill × (1 − c) / (1 + c) − 1`. A second record with 2 × the cost is kept for the cost-stress gate.
5. **Statistics per strategy:** trades, win rate (return > 0), profit factor (gross wins / gross losses; 99 with no
   loss), expectancy (mean return per trade), profit factor at 2 × costs, and the **holdout** = trades opened in the
   last *Holdout* % of the loaded candles (default 15 %, like the bot): count and mean return.
6. **Gates → status:**

| Gate | Rule (defaults) | Bot gate |
|---|---|---|
| Expectancy | mean return per trade > 0 after costs | G1 |
| Profit factor | ≥ *Profit factor at least* (1.2) | G2 |
| Cost stress | profit factor at 2 × costs > 1 | G4 |
| Holdout | ≥ 3 holdout trades with a positive mean (*Holdout must be profitable*) | G7 |
| Enough trades | ≥ *At least N trades* (20) | G8 |

   `PASS` = all gates · `few` = fewer trades than required · `partial` = profitable with enough trades but another gate
   fails (≈ the bot's CANDIDATE) · `fail` = not profitable.

   The bot's full validation is stronger and is NOT reproduced: parameters chosen per fold on past data only (anchored
   5-fold walk-forward, 15 % final holdout scored once), G0 lookahead self-test, G3 positive in most folds, G5
   neighbouring parameters also work, G6 profit not from the top 5 % of trades, G9 bootstrap p < 0.10, G10 probability
   of backtest overfitting < 0.5 (CSCV), G11 deflated Sharpe ≥ 0.90, Benjamini-Hochberg false-discovery control
   across the 27 strategies. The chart test uses one fixed parameter set (the inputs) on the history TradingView has
   loaded, so treat `PASS` as "worth a closer look", and prefer strategies the bot reported as ACCEPTED (mark them
   *Validated ✓* and pick *Manual* or *Auto or manual*).

## 11. The approval chain (`audit.audit` + `market.gate`)

On every confirmed candle, for every strategy that is not *Off*:

1. **Validated?** depends on *A strategy is validated by*: `Auto: chart-backtest gates` → status `PASS`;
   `Manual: my ✓ list` → the strategy is set to *Validated ✓* in group ⑧; `Auto or manual` → either.
   Option *Also approve 'partial' strategies (half risk)* (off by default): a `partial` strategy may also approve, at
   half the risk, like the bot's CANDIDATE.
2. **Regime fits?** its requirement (section 5).
3. **Blocked?** first reason found: 24h volume under the floor → a fresh bearish pattern → the market gate (section 6).
4. **APPROVED** = the signal is on + validated (or partial with the option) + fits + not blocked. `▲ BUY` = approved on
   the candle the signal switched on / fired.

The **leading strategy** is the first approved one in the order 01 → 27, a validated one before a half-risk one; its
fallback stop sets the plan's stop distance.

## 12. Checks, score and verdict (`checks.py`, `audit.verdict`)

Each check scores 0-100 (pass ≥ 65, warn ≥ 40, fail < 40), on the last closed candle:

| Check | Weight | Score on TradingView |
|---|---|---|
| Liquidity | 10 | 24h volume band: ≥ $100M 100 · ≥ $25M 85 · ≥ $5M 65 · ≥ $1M 40 · ≥ $250k 20 · else 0. (The bot: 45 % volume + 30 % spread + 25 % order-book depth.) |
| Trend (multi-timeframe) | 13 | 0.45 × daily + 0.35 × 4h + 0.20 × 1h trend points (section 4) |
| Momentum | 6 | 0.40 × RSI score + 0.35 × MACD score + 0.25 × clamp(50 + 20-candle change × 500, 0, 100) on the chart timeframe. RSI score: 50-68 → 100, 68-78 → 55, > 78 → 25, 40-50 → 55, 30-40 → 35, < 30 → 25. MACD score: +50 histogram > 0, +50 histogram > its value 2 candles ago. |
| Volatility & risk | 5 | daily ATR %: < 1.5 % → 45, ≤ 6 % → 90, ≤ 10 % → 60, else 25; −20 if the 90-day max drawdown < −50 %. (Bot only: −10 when its HAR-RV model expects a volatile day.) |
| Market regime & relative strength | 9 | BTC: 75 if risk-on else 35. Others: 50 + clamp((coin − BTC 30-day return) × 150, ±30) + clamp((coin − BTC 90-day return) × 60, ±20) + (10 if BTC risk-on else −15). |
| Order flow & volume | 6 | 50 + 10 if the 7-day change > 0 with 7d / 30d volume > 1.2 · −5 if rising on volume < 0.8 · −10 if falling on volume > 1.2. (Bot only: taker-buy share terms.) |
| Crypto-wide & macro regime | 15 | the altcoin (or BTC) eligibility score; weight 0 when the market layer is off or has no data |
| Strategy library (validated) | 28 | 35 + 22 × min(validated strategies on in a fitting regime, 2) + 10 × min(partial ones on and fitting, 2) + 6 × min(validated, 3) + 2 × min(partial, 3); 25 if none is validated or partial |
| ML meta-labeler | 8 (bot) | not on TradingView — excluded (the bot also excludes it when it has no proven edge) |

**Score** = Σ(weight × score) / Σ(weights used). **Caps:** 24h volume under the floor → at most 35 (AVOID);
BTC risk-off (with the BTC filter on) → at most 64.9; no approved strategy → at most 64.9.
**Verdict:** ≥ 70 FAVORABLE · ≥ 55 WATCHLIST · ≥ 40 NEUTRAL · else AVOID.

| Verdict | Meaning |
|---|---|
| FAVORABLE | A validated strategy has an approved signal and market conditions agree. Use the plan; size by risk. |
| WATCHLIST | Some conditions line up, but no validated signal is confirmed. Wait. |
| NEUTRAL | No clear edge right now. Better opportunities likely exist elsewhere. |
| AVOID | Conditions are unfavourable for a spot long. Stay out. |

## 13. The trade plan (`audit.trade_plan`) — from the last closed candle

* Entry zone: low = `min(max(min(EMA20, close), close − 0.75 ATR), close − 0.25 ATR)`, high = the close.
* Stop = `max(min(lowest low of 20 candles − 0.25 ATR, close − S × ATR), entry low − 3 ATR)` where S = the leading
  strategy's fallback stop (ATR) or the input *Stop (ATR) when no strategy leads* (2).
* Reference = middle of the zone; risk = reference − stop; **T1** = reference + 1.5 × risk; **T2** = reference + 3 × risk.
* Size = `min(risk % / stop distance %, 100 %)` of the account (input *Risk per trade*, default 1 %; half of it when
  the leading approval is a half-risk `partial`); in dollars for *Account size* (default $10,000).
* Shown as reference levels when nothing is approved.

## 14. What to do now (`brief.build`, with the live price)

Evaluated on every tick with the live price against the plan of the last closed candle, first match wins:

| Code | When | Text (template) |
|---|---|---|
| STAY OUT | verdict AVOID | "STAY OUT — <caps>." + notes |
| PLAN INVALID | approved, price ≤ stop | "PLAN INVALID — price is under the stop <stop>. Wait for a new signal." |
| WAIT FOR THE PULLBACK | approved, price > zone high + 0.2 % | "<codes> is live but price is <x %> above the zone <lo–hi>. Don't chase." |
| UNDER THE ZONE | approved, price < zone low − 0.2 % (above the stop) | "Stretched: buy only after a close back above <zone low>." |
| BUY ZONE LIVE | approved, price inside the zone | "<codes>: buy <lo–hi>, stop <stop> (−x %), T1 <t1> (1.5R), T2 <t2> (3R), size <x %> of the account." |
| WAIT FOR THE BREAKOUT | nothing approved, a bullish pattern is armed above price | "A <tf> close above <level> (<pattern>) with ≥ 1.2× volume." |
| NOTHING TO DO YET | otherwise | "No validated strategy is firing in a fitting regime." |

Notes appended where relevant: "BTC is risk-off — altcoin longs are capped.", "Bearish <name> just completed — longs
blocked.", "Market: <High-volatility event / BTC selloff / Risk-off>." The armed level shown is the nearest one above
the price among the W neckline, inverse H&S neckline, range top, triangle / wedge line, rounding-bottom rim, cup rim,
V-bottom reclaim level, downtrend line and flag high.

## 15. User interface

### 15.1 Inputs (settings dialog) — 8 groups, every input has a tooltip

| Group | Inputs (default) |
|---|---|
| ① Display | Theme (Auto / Dark / Light — Auto reads `chart.bg_color`) · Dashboard (Full / Compact / Off) · Dashboard position (Top right + 7 more) · Dashboard text (Tiny / Small / Normal) · Signal labels (All signals / Approved only / Off) · Trade plan ✓ · Pattern lines ✓ · EMA 20/50/200 ✓ · Risk-off background ✓ · colours: Bull #089981, Bear #f23645, Approved #00c853, Setup #ffa726, Plan #2962ff |
| ② Signals & approval | A strategy is validated by (Auto: chart-backtest gates / Manual: my ✓ list / Auto or manual) · Also approve 'partial' strategies (half risk) ✗ · BTC risk-on filter (daily) ✓ · BTC symbol (BINANCE:BTCUSDT) · Bearish patterns block longs (3 candles) ✓ · Market-regime gate ✓ · Minimum 24h volume ($1,000,000) · Breakout volume ≥ × 30-bar average (1.2) · Pattern reward : risk at least (1.5) |
| ③ Chart backtest gates | Cost per side % (0.15) · At least N trades (20) · Profit factor at least (1.2) · Holdout = last N % of the chart (15) · Holdout must be profitable ✓ |
| ④ Trade plan & sizing | Stop (ATR) when no strategy leads (2.0) · Risk per trade % (1.0) · Account size $ (10,000) |
| ⑤ Market layer | Market layer ✓ · TOTAL, TOTAL2, TOTAL3, BTC.D, USDT.D, ETH/BTC, Dollar, VIX, Nasdaq 100 symbols (paired two per row) |
| ⑥ Rule strategies (bot defaults) | 01 ADX at least 20 · 02 votes 2 · 03 breakout bars 20 · 04 lookback 90, min score 0 · 05 RSI below 35 · 06 offset 0.975, EWO above 4 · 07 cluc × 0.985 · 08 offset 0.978, EWO above 3 · 09 parts 4 · 10 fast 21, slow 55 · 11 swing bars 20, target 2R · 12 below ATH 0.85, target +1.0 — each tooltip lists the values the bot tests |
| ⑦ Patterns & price action | Pattern entry (close / retest) + stop (pattern / tight) · 16 buy zone 0.3 · 22 touch 0.6 ATR · 23 within 0.3 ATR, target 1.5R · 24 RSI divergence ✗, target 2R · 25 target 1.5R · 26 lowest 20 %, volume 1.3× · 27 z below 2 |
| ⑧ Strategies | one dropdown per strategy 01-27: Off / On / Validated ✓ (default On) |

### 15.2 On the chart

* **EMA 20 / 50 / 200** — #f7a600 (1 px), #2962ff (1 px), #9c27b0 (2 px).
* **Nearest breakout level** — a line (broken where there is none) in the *Setup* colour at the nearest armed bullish
  level above the price.
* **BTC risk-off background** — the *Bear* colour at 94 % transparency while BTC is risk-off (filter on).
* **Markers** (confirmed candles only): ▲ *Approved signal* under the candle (Approved colour, small) · ● *Strategy
  fired* under the candle (neutral grey, tiny) · ▼ *Bearish pattern (blocks longs)* above the candle (Bear, tiny).
* **Signal labels** under the candle that fired: `▲ BUY  ICT ✔ · W-bottom` (green when something is approved, grey
  otherwise). The tooltip lists every strategy that fired with its verdict (APPROVED / not validated (chart test:
  status) / regime does not fit (needs …) / blocked — reason), its chart-test numbers (trades, win %, PF, % per trade),
  its own stop and target, then the coin's regime and the plan. Bearish completions get `▼ <pattern>` above the candle
  with a tooltip explaining the 3-candle block. Keep tooltips under ~3,000 characters.
* **Pattern drawings** — one live line (or box) per armed formation: dashed *Setup* colour while armed, solid *Bull*
  (2 px) when it breaks out (it stays), deleted when void or expired. Triangles / wedges draw both lines; the
  ascending channel is drawn in the *Plan* colour; range boxes are filled at 93 %; bearish formations use the *Bear*
  colour.
* **Plan projection** on the last candle, from 2 to 18 candles to the right: entry-zone box (Plan, 75 %), risk box from
  the reference to the stop (Bear, 86 %), reward boxes to T1 (Bull, 86 %) and T1 → T2 (Bull, 93 %), and labels at the
  right edge: `BUY lo–hi · size x % ≈ $y` (or `Entry …` when nothing is approved), `Stop x (−y %) · risk $z`,
  `T1 x (+y % · 1.5R)`, `T2 x (+y % · 3R)`.
* **Data window** (no chart clutter): Verdict score, Market score, Approved strategies, Strategies on.

### 15.3 The dashboard (a `table`, position / size from the inputs)

Theme tokens — dark: panel #131722 (4 % transp.), header #1e222d, zebra row #1e222d at 55 %, text #d1d4dc, muted
#787b86, grid #2a2e39, neutral #9598a1. Light: panel #ffffff, header #eef1f6, zebra #f5f7fa, text #131722, muted
#6a6d78, grid #d1d4dc, neutral #6a6d78. A status colour's *tint* (cell background) = the colour at 70 % (dark) / 82 %
(light) transparency. Score colours: ≥ 65 Bull, ≥ 40 Setup, else Bear. Verdict colours: FAVORABLE Bull, WATCHLIST
Setup, NEUTRAL neutral, AVOID Bear.

**Full** — 9 columns × 34 rows:

| Row | Cells |
|---|---|
| 0 | `COIN AUDIT · 27` (cols 0-1) · `SYMBOL · TF · price` (2-5) · `VERDICT nn/100` in white on the verdict colour (6-8); its tooltip is the weighted breakdown of every check, the raw score, the caps and the thresholds |
| 1 | `DO` · the what-to-do text, word-wrapped at ~78 characters, in the action's colour on its tint (1-8); tooltip = the full text |
| 2 | `MARKET` · trend chip (`trend ↑`, coloured) · `vol normal/high/low` · `BTC on/OFF` (+ "(filter off)") · `liq ok/thin` · structure · `<market regime> · alt nn` (6-8, tooltip = behaviour + the score items met) |
| 3 | `PLAN` · `Entry lo–hi` (1-2) · `Stop x (−y %)` (3-4) · `T1 · T2` (5-6) · `Size x % ≈ $y` (7-8) — each with a tooltip giving its formula |
| 4 | `CHECKS` · Trend · Mom · Vol · vs BTC · Flow · Liq · Mkt · Library — `Name nn` in the score colour on its tint; tooltips give the rule and the weight |
| 5 | header: `#` · `Strategy` · `Fit` · `Now` · `Trades` · `Win` · `PF` · `Exp` · `Status` |
| 6-32 | one row per strategy: `01`… · name (+ ` ✓` when marked) with the how-it-works tooltip, family, regime and why it is not approved · `✓`/`✗` · `▲ BUY` / `APPROVED` / `FIRED` / `on` / `—` / `off` (` ½` = half risk) · trades · win % · PF (Bull ≥ the PF gate, Setup ≥ 1, Bear < 1) · expectancy (Bull > 0, Bear) · `PASS ✓` / `partial` / `few` / `fail` with every gate's number in the tooltip. Approved rows are tinted in the Approved colour, other rows zebra-striped, Off rows muted |
| 33 | the legend (cols 0-8): "▲ BUY = validated + regime fits + nothing blocks · FIRED = new signal · on = condition holds · PASS = chart-backtest gates", plus the strategies firing on the forming candle |

**Compact** — 5 columns × 16 rows: row 0 `SYMBOL · TF` (0-2) and the verdict (3-4); row 1 `DO` + text wrapped at ~46
(1-4); row 2 `PLAN` entry · stop · T1 (tooltip with T2 and size); row 3 `MKT` regime chips; row 4 header `#`,
`Strategy`, `Now`, `PF`, `Status`; rows 5-14 up to ten strategies picked in the order approved → fired → on →
validated; row 15 a hint ("No strategy is active or validated on this chart." or "Full dashboard: inputs → Dashboard
→ Full").

### 15.4 Alerts

`alertcondition`s (confirmed candles): **Approved buy signal**, **Any strategy fired**, **Breakout setup armed** (a
level appears), **Bearish pattern completed**, **BTC turned risk-off**, **Verdict became FAVORABLE** — each message
names `{{ticker}} {{interval}}` and what to do next. Plus `alert(..., alert.freq_once_per_bar_close)` on every candle
where a strategy fired: `SYMBOL TF · APPROVED: codes · fired: codes · VERDICT nn/100 · <what to do>` (for "Any
alert() function call" alerts).

## 16. Script layout (keep this order)

1. Header comment (what is on the chart, the read-only disclaimer) and `indicator()`.
2. Inputs ① → ⑧. 3. Theme tokens and colour helpers. 4. Core series and `max_bars_back`.
5. Multi-timeframe context (functions with `simple int off`, then the `request.security` calls). 6. Coin regime.
7. Market layer (functions, 15 requests, facts, scores, regime, behaviour, the score items text).
8. Pivots, structure, helpers (`f_off` capped at 480, tolerance, highest / lowest between bars, prior trend, pivot
   search between bars, least-squares fit), the bullish and bearish triggers.
9. Strategies 01-12. 10. Drawing helpers, then patterns 13-22. 11. Bearish blockers. 12. Strategies 23-27.
13. Library metadata arrays (code, name, family, regime need, chop OK, mode, how-it-works text, exit mechanics) and
    this candle's signal arrays (on, new, exit, stop, target, limit).
14. The chart backtest (state arrays, record / stop / open helpers, the confirmed-candle loop) and `f_stats`.
15. Closed-candle values, the approval helpers and the approval loop.
16. Checks, score, verdict, caps. 17. Trade plan, nearest armed level. 18. What to do now.
19. Plots, markers, labels, the plan projection. 20. Dashboard (table, merges on the first candle, Full / Compact).
21. Alerts.

## 17. Acceptance tests

1. Compiles in Pine v6 with no errors and no warnings; ≤ 40 requests; loads within TradingView's time limit on 20,000
   candles.
2. Works on BINANCE:BTCUSDT, ETHUSDT, SOLUSDT and a small altcoin, on 15m / 1h / 4h / 1D, with the market layer on and
   off, in the light and the dark theme, in Full and Compact.
3. **No repainting:** in Bar Replay, step candle by candle — a label, marker, approval or verdict on a past candle never
   changes; nothing appears until its candle closes; the forming-candle list is the only live part.
4. Turning a strategy *Off* removes it from labels, approvals and the library score; *Validated ✓* with *Manual*
   approves it when it fires in a fitting regime with no block.
5. With the BTC filter off, BTC risk-off neither caps the verdict nor blocks the BTC-dependent strategies.
6. Pattern drawings: dashed while armed, solid green after the breakout, gone when void or expired.
7. Cross-check against the bot (`python audit/audit.py SYMBOL --tf 4h`) on the same closed candle: regime, BTC state,
   market regime, plan levels and the strategies that fired should agree (see the differences below).

## 18. Known differences from the Python bot (keep them documented in the UI)

* Volume is base volume (no quote volume, taker-buy volume, trade counts, order book, spread or depth on TradingView):
  the liquidity check is the 24h-volume band only, the flow check has no taker terms, strategy 05 has no taker filter.
* No funding rates (strategy 10's funding filter is skipped), no Markov-switching volatility filter, no HAR-RV, no ML
  meta-labeler, no macro / news events file, no stale-data kill switch, no live track-record drift block.
* Validation is a chart backtest with fixed parameters and 5 gates, not the bot's walk-forward optimisation and 12
  gates (section 10); the vol-target position weights of 02 / 03 / 04 are not applied; the 15 % holdout is reported
  separately but its trades also count in the overall numbers.
* Each pattern strategy tracks its newest formation only (the bot tracks every formation at once); the pivot rule is
  identical. The coin/BTC pair is read from Binance only (the bot builds a synthetic ratio when the pair is missing).
* The market layer uses TradingView's CRYPTOCAP indices, TVC:DXY, TVC:VIX and NASDAQ:NDX (the bot builds its own
  indices from CoinGecko / Binance and reads FRED); values are close but not identical.

## 19. What to return (when you are the AI building or changing this)

1. The complete Pine v6 script in ONE code block — never fragments or "the rest stays the same".
2. Then a short changelog (what changed and why) and the acceptance tests you checked mentally.
3. Keep every name, input, section order, colour token and rule in this document unless the request changes it;
   if a rule must change, say so explicitly.
4. Before answering, re-check the hard rules of section 2 line by line: lazy `and` / `or`, wrapped-line indentation,
   typed `na`, `request.security` lookahead, closed-candle decisions, table merges, limits.
5. Never add order execution, never claim profitability, keep the read-only disclaimer.

`PROMPT END`

---

## Using the prompt for common requests

* **"Add strategy 28 (e.g. a Supertrend flip) following the same conventions."** — The AI must add: the input dropdown
  in ⑧, its parameters in ⑥ / ⑦ with tooltips, its entry / exit / stop / target series, one more entry in every
  metadata and execution array (27 → 28, including the backtest arrays sized with `array.new<…>(28, …)` and every
  `for i = 0 to 26` loop), a dashboard row (the Full table needs one more row), and a line in the strategy table here.
* **"Make strategy N a `strategy()` script."** — Copy sections 3-8 for that strategy and the backtest rules of section
  10 into `strategy.entry` / `strategy.exit` (with the default `process_orders_on_close = false` orders fill at the
  next open), `commission_type = strategy.commission.percent` and `commission_value = 0.15` (the 0.10 % fee plus the
  0.05 % slippage — the `slippage` argument counts ticks, not percent), and the same stop / target / time-limit /
  trailing rules.
* **"Why is nothing approved?"** — Hover the strategy's *Now* cell and *Status* cell: not validated (which gate failed),
  regime does not fit (which requirement), or blocked (liquidity, bearish pattern, market regime / score).

## Validation of the shipped indicator

`tradingview/coin_audit_library.pine` follows this document. It cannot be compiled outside TradingView, so it is
checked offline with `tradingview/lint_pine.py`: a full Pine grammar parse (pynescript), every built-in function,
constant and named argument checked against the Pine v6 reference, the wrapped-line, typed-`na`, duplicate-declaration
and `:=`-before-declaration rules, and the v6 lazy-evaluation rule (no stateful call after `and` / `or` / `?:`, none
inside local blocks). Run `python tradingview/lint_pine.py --fast tradingview/coin_audit_library.pine` (seconds) or
without `--fast` for the grammar parse (minutes). If TradingView still reports an error, paste the message and the
line into your AI together with this prompt.
