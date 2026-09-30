"""Shared pieces for the CoinAudit Freqtrade strategies (not a strategy itself)."""
from datetime import datetime

from freqtrade.exchange import timeframe_to_minutes

# Freqtrade protections (applied live/dry-run; in backtests add --enable-protections)
PROTECTIONS = [
    {"method": "CooldownPeriod", "stop_duration_candles": 2},
    {"method": "StoplossGuard", "lookback_period_candles": 48, "trade_limit": 3,
     "stop_duration_candles": 24, "only_per_pair": False},
    {"method": "MaxDrawdown", "lookback_period_candles": 200, "trade_limit": 10,
     "stop_duration_candles": 48, "max_allowed_drawdown": 0.15},
    {"method": "LowProfitPairs", "lookback_period_candles": 400, "trade_limit": 4,
     "stop_duration_candles": 100, "required_profit": 0.0},
]


def candles_held(trade, current_time: datetime, timeframe: str) -> float:
    return (current_time - trade.open_date_utc).total_seconds() / 60 / timeframe_to_minutes(timeframe)


def roi_ladder(held: float, profit: float, ladder: list) -> bool:
    """ladder = [(candles_held, min_profit), ...] — timeframe-independent ROI table."""
    need = None
    for b, r in ladder:
        if held >= b:
            need = r
    return need is not None and profit >= need
