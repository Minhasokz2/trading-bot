# TradingView indicator — the 27-strategy library on your chart

`coin_audit_library.pine` is a Pine Script® v6 **indicator** (overlay) that evaluates every strategy of the bot's
library on the chart you are looking at, on candle close, with the same rules as `audit/strategies.py`,
the same pivot engine as `audit/patterns.py` (K = 3, a pivot counts only 3 candles after it printed) and the
same regime classifier as `audit/regime.py`.

## Install (2 minutes)

1. TradingView → open any Binance chart → **Pine Editor** (bottom panel) → **Open → New indicator**.
2. Delete the template, paste the whole file, press **Save** (name it), then **Add to chart**.
3. Settings (gear icon): the groups are *General*, *Trade plan*, *Rule strategies*, *Pattern parameters*,
   *Strategies* (off / show / validated).
4. Alerts: right-click the chart → *Add alert* → condition **CoinAudit27** → pick *Any alert() function call*
   (the message names the strategies, regime and plan) or one of the four fixed conditions.

## What it shows

| On the chart | Meaning |
|---|---|
| ▲ green triangle below a bar | a strategy you marked **validated** fired in a regime that fits it (the bot's "APPROVED") |
| ● grey dot | some strategy fired (label lists which) |
| label under the bar | the strategies that fired on that candle, approved ones first with ✔ |
| yellow broken line | the nearest armed breakout level (W neckline, inverse H&S neckline, range top, triangle/wedge line, rounding rim, V-bottom reclaim, trendline, flag top) — "wait for the close above it" |
| blue box + dashed lines on the right | the trade plan of `audit.trade_plan`: entry zone (EMA20-anchored, ¼–¾ ATR under the close), stop (under the 20-bar swing low / N ATR), T1 = 1.5R, T2 = 3R, and the size for your risk % |
| red background | BTC is risk-off on the daily (below its long EMA) — every altcoin long is capped, as in the bot |
| board (top right) | regime (trend/range/transition, volatility percentile, BTC, liquidity, structure), **what to do**, the plan, and one row per strategy: regime fit · state (`on` / `FIRED` / —) |

## What it cannot do (that is the bot's job)

* **No validation.** The indicator shows *signals*; the bot decides whether a strategy has an *edge on this coin*
  (walk-forward, 12 gates, Monte Carlo, PBO/DSR, false-discovery control). Run the bot's audit, then mark the
  strategies it reports as ACCEPTED as **validated** in the inputs — only those produce the green triangle.
* **Data TradingView does not have:** taker-buy flow (module dropped from the pullback strategy), funding rates
  (filter dropped from EMA cross), the order book (liquidity is approximated by 24h quote volume), quote volume
  (volume ratios use base volume), the Markov volatility filter and the meta-labeler.
* **Approximations:** the rounding-bottom quadratic fit is replaced by a U-shape test; the trendline / triangle /
  channel lines are least-squares fits through the last 2–3 pivots; one armed formation per pattern at a time
  (the bot keeps a list and picks the best reward:risk).
* **History:** patterns look back at most ~480 bars; the deep-drawdown ATH is the highest high TradingView loaded.

Higher-timeframe values (daily EMAs, BTC regime, the pullback trend filter) use the **last closed** HTF candle
(`lookahead_on` with a one-bar offset), so nothing repaints and the values match `regime.align()`.

Not financial advice. The indicator never trades.
