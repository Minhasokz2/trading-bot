# TradingView indicator — the 27-strategy library on your chart

| File | What it is |
|---|---|
| `coin_audit_library.pine` | The indicator (Pine Script® v6, overlay, read-only). Paste it into TradingView. |
| `INDICATOR_PROMPT.md` | The master prompt: every strategy, rule, gate, score and UI element in full detail. Read it to understand any number on the chart, or give it to an AI / Pine developer to rebuild, extend or port the indicator. |
| `lint_pine.py` | Offline checks for the script (there is no Pine compiler outside TradingView). |

The indicator ports the bot to the chart you are looking at: the 27 strategies of `audit/strategies.py` with the
pivot engine and breakout trigger of `audit/patterns.py`, the coin regime of `audit/regime.py`, the crypto-wide and
macro market layer of `audit/market.py`, the checks and verdict of `audit/checks.py` / `audit/audit.py`, the trade
plan, and a paper-trading backtest of every strategy with the exit mechanics of `audit/validation.py`.

## Install (2 minutes)

1. TradingView → open a Binance spot chart (e.g. `BINANCE:SOLUSDT`, 4h) → **Pine Editor** (bottom panel) →
   **Open → New indicator**.
2. Delete the template, paste the whole `coin_audit_library.pine`, press **Save**, then **Add to chart**.
3. Give it a few seconds: it paper-trades all 27 strategies over the loaded history and asks TradingView for the
   market indices (TOTAL, TOTAL2, TOTAL3, BTC.D, USDT.D, ETH/BTC, DXY, VIX, NDX).

## How to read it

**The dashboard (top right)**

* **Verdict and score** (hover it for the breakdown): FAVORABLE ≥ 70 · WATCHLIST 55-70 · NEUTRAL 40-55 · AVOID < 40,
  with the bot's weights and caps (illiquid → AVOID, BTC risk-off or nothing approved → at most WATCHLIST).
* **DO** — what to do now, with the live price against the plan: *BUY ZONE LIVE*, *WAIT FOR THE PULLBACK*, *UNDER THE
  ZONE*, *PLAN INVALID*, *WAIT FOR THE BREAKOUT* (a close above the yellow level with volume), *NOTHING TO DO YET*,
  *STAY OUT*.
* **MARKET** — the coin's regime (trend / range / transition, volatility, BTC, liquidity, structure) and the market
  regime (one of 8, e.g. *Broad risk-on*, *BTC selloff*, *Range / chop*) with the altcoin or BTC eligibility score.
* **PLAN** — entry zone, stop, T1 (1.5R), T2 (3R) and the position size for your risk % and account.
* **CHECKS** — Trend, Momentum, Volatility, vs BTC, Flow, Liquidity, Market, Library (0-100; green ≥ 65).
* **One row per strategy** — does the regime fit, its state now (`▲ BUY`, `APPROVED`, `FIRED`, `on`, `—`), and its
  paper-trading record on this chart: trades, win %, profit factor, average % per trade, status (`PASS ✓`, `partial`,
  `few`, `fail`). Hover a strategy's name for how it works; hover its status for every gate's number; hover its state
  for why it is not approved.

**On the chart:** ▲ approved buy signal · ● a strategy fired (hover the label: which ones, why approved or blocked,
their stops and targets) · ▼ a bearish pattern completed (blocks longs for 3 candles) · the yellow line is the nearest
armed breakout level · pattern lines are dashed while armed, solid green after the breakout · the boxes to the right of
the last candle are the plan (entry zone, risk to the stop, reward to T1 and T2) · a red background means BTC is
risk-off on the daily.

## When is a signal APPROVED?

1. The strategy is **validated** — input *A strategy is validated by*:
   * **Auto: chart-backtest gates** (default) — it passed the gates on this chart: ≥ 20 trades, profit factor ≥ 1.2,
     still profitable at twice the costs, positive average per trade, and ≥ 3 profitable-on-average trades in the
     holdout (the last 15 % of the chart). Costs: 0.15 % per side.
   * **Manual: my ✓ list** — you set the strategy to *Validated ✓* in group ⑧, e.g. the strategies the bot reported
     as ACCEPTED for this coin (its 12 walk-forward gates are much stronger evidence than a chart test).
   * **Auto or manual** — either.
   * Optional: *Also approve 'partial' strategies (half risk)* mirrors the bot approving CANDIDATEs at half risk.
2. Its **regime fits** (trend / pullback / momentum / dip / range / any).
3. **Nothing blocks it**: 24h volume above the floor, no bearish pattern completed in the last 3 candles, and the
   market gate allows it (high-volatility event, BTC selloff, risk-off, chop and a low altcoin score block entries).

## Settings

① Display (theme, Full / Compact / Off dashboard, position, text size, labels, plan, pattern lines, EMAs, colours) ·
② Signals & approval · ③ Chart backtest gates · ④ Trade plan & sizing · ⑤ Market layer (symbols) · ⑥ Rule
strategies · ⑦ Patterns & price action · ⑧ the 27 strategies (Off / On / Validated ✓). Every parameter's tooltip
lists the values the bot tests.

## Alerts

Right-click the chart → *Add alert* → condition **CoinAudit27** → choose *Approved buy signal*, *Any strategy fired*,
*Breakout setup armed*, *Bearish pattern completed*, *BTC turned risk-off*, *Verdict became FAVORABLE*, or *Any
alert() function call* (one message per closed candle with the strategies, the verdict and what to do).

## Limits (what only the bot can do)

* TradingView has no quote volume, taker-buy volume, order book, funding rates or macro calendar: liquidity is the
  24h-volume band, flow has no taker terms, strategy 05 has no taker filter, strategy 10 no funding filter.
* The chart test is one fixed parameter set on the loaded history with 5 gates — not the bot's walk-forward
  optimisation, 12 gates, Monte Carlo, PBO / deflated Sharpe and false-discovery control. No ML meta-labeler and no
  Markov volatility filter.
* Each pattern strategy tracks its newest formation; patterns look back at most ~480 candles; the deep-drawdown ATH
  is the highest high TradingView loaded.
* The full list is in `INDICATOR_PROMPT.md`, section 18.

No repainting: every decision uses closed candles; higher-timeframe values use the last **closed** higher-timeframe
candle (`lookahead_on` with a one-candle offset), like `regime.align()`. Not financial advice — the indicator never
trades.

## Checking the script offline

```bash
pip install pynescript                                                    # optional: the Pine grammar
python tradingview/lint_pine.py --fast tradingview/coin_audit_library.pine   # names, arguments, layout, v6 rules (seconds)
python tradingview/lint_pine.py tradingview/coin_audit_library.pine          # + full grammar parse (minutes)
```

It checks every built-in function, constant and named argument against the Pine v6 reference, the wrapped-line
indentation rule, typed `na` in `array.from`, duplicate declarations, `:=` before a declaration, and Pine v6's lazy
`and` / `or`: a stateful call (`ta.*`, `math.sum`, `request.*`, or a function that reads its argument's history)
after `and`, `or` or `?` does not run on every candle and silently corrupts its state. TradingView itself is the final
judge: if it reports an error, paste the message with `INDICATOR_PROMPT.md` into your AI of choice.
