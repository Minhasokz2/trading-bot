# pragma pylint: disable=missing-docstring, invalid-name
"""
CoinAuditDonchian — Donchian breakout with volatility-normalised position size.
Mirrors trend_donchian_v1 in the audit library.

Entry:  close breaks above the prior N-candle high, BTC daily risk-on.
Exit:   close below the prior N/2-candle low.  Stop: 3 x ATR from entry.
Size:   stake x clip(trailing-year median ATR% / current ATR%, 0.25, 1).

Timeframe defaults to 4h; override with  --timeframe 1h  (or 15m / 1d).
"""
from datetime import datetime

import talib.abstract as ta
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, IntParameter, informative, stoploss_from_absolute

from coinaudit_common import PROTECTIONS
from signal_log import log_signal


class CoinAuditDonchian(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "4h"
    can_short = False
    minimal_roi = {"0": 100}
    stoploss = -0.20
    use_custom_stoploss = True
    use_exit_signal = True
    process_only_new_candles = True
    startup_candle_count = 400

    @property
    def protections(self):
        return PROTECTIONS
    position_adjustment_enable = False

    n = IntParameter(20, 100, default=55, space="buy", optimize=False)
    STOP_ATR = 3.0

    @informative("1d", "BTC/{stake}")
    def populate_indicators_btc_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["risk_on"] = (dataframe["close"] > dataframe["ema200"]).astype(int)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        n = int(self.n.value)
        dataframe["dc_high"] = dataframe["high"].rolling(n).max().shift(1)
        dataframe["dc_low"] = dataframe["low"].rolling(max(5, n // 2)).min().shift(1)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        atr_pct = dataframe["atr"] / dataframe["close"]
        target = atr_pct.rolling(365, min_periods=100).median()
        dataframe["size_w"] = (target / atr_pct).clip(0.25, 1.0).fillna(0.5)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        cond = ((dataframe["close"] > dataframe["dc_high"])
                & (dataframe["btc_usdt_risk_on_1d"] == 1) & (dataframe["volume"] > 0))
        dataframe.loc[cond, ["enter_long", "enter_tag"]] = (1, "donchian_break")
        log_signal(self, dataframe, metadata["pair"], confidence=None,
                   horizon=f"~60 x {self.timeframe}", strategy_name="CoinAuditDonchian")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] < dataframe["dc_low"], ["exit_long", "exit_tag"]] = (1, "dc_low_break")
        return dataframe

    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float,
                            proposed_stake: float, min_stake: float | None, max_stake: float,
                            leverage: float, entry_tag: str | None, side: str, **kwargs) -> float:
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if df.empty:
            return proposed_stake
        w = float(df["size_w"].iloc[-1])
        return max(min_stake or 0, proposed_stake * w)

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                        current_profit: float, after_fill: bool, **kwargs) -> float | None:
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        rows = df.loc[df["date"] < trade.open_date_utc]
        if rows.empty:
            return None
        return stoploss_from_absolute(trade.open_rate - self.STOP_ATR * rows["atr"].iloc[-1], current_rate,
                                      is_short=trade.is_short, leverage=trade.leverage)
