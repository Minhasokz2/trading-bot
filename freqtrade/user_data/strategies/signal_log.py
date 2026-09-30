"""Append every new entry signal to user_data/signals/signals.csv (dry-run / live only).

Each row holds what a human needs to judge the signal: price, confidence,
regime, entry zone, invalidation (stop), targets and horizon — not just "BUY".
Later outcomes can be read from Freqtrade's trade database / FreqUI.
"""
import csv
from pathlib import Path

from freqtrade.enums import RunMode

FIELDS = ["candle_time_utc", "pair", "strategy", "price", "confidence", "btc_risk_on",
          "entry_low", "entry_high", "stop", "target1", "target2", "horizon"]
_seen: set = set()


def log_signal(strategy, dataframe, pair: str, confidence, horizon: str, strategy_name=None, **_):
    if strategy.dp.runmode not in (RunMode.DRY_RUN, RunMode.LIVE):
        return
    if dataframe.empty or dataframe["enter_long"].iloc[-1] != 1:
        return
    last = dataframe.iloc[-1]
    key = (pair, str(last["date"]))
    if key in _seen:
        return
    _seen.add(key)
    price, atr = float(last["close"]), float(last.get("atr", 0) or 0)
    entry_low = max(float(last.get("ema20", price)), price - 0.75 * atr) if atr else price
    entry_low = min(entry_low, price - 0.25 * atr)
    stop = entry_low - 1.5 * atr
    mid = (entry_low + price) / 2
    risk = mid - stop
    row = {"candle_time_utc": str(last["date"]), "pair": pair,
           "strategy": strategy_name or type(strategy).__name__,
           "price": price, "confidence": "" if confidence is None else round(float(confidence), 3),
           "btc_risk_on": int(last.get("btc_usdt_risk_on_1d", 1) or 0),
           "entry_low": round(entry_low, 8), "entry_high": price, "stop": round(stop, 8),
           "target1": round(mid + 1.5 * risk, 8), "target2": round(mid + 3 * risk, 8),
           "horizon": horizon}
    path = Path(strategy.config["user_data_dir"]) / "signals" / "signals.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)
