# pragma pylint: disable=missing-docstring, invalid-name
"""
CoinAuditElliot — ElliotV5/V8-style EMA offset gated by the Elliott Wave Oscillator.
Entry: close < EMA17 x LOW_OFFSET with EWO > EWO_HIGH and RSI < 65, or a deep EWO < -19 flush;
BTC risk-on. Exit: close > EMA49 x 1.006, trailing after +3%, ROI ladder, 3 x ATR stop.
Mirrors elliot_ewo_v1 in the audit library.
Timeframe defaults to 1h; override with --timeframe. Research strategy — dry-run only.
"""
from datetime import datetime

import numpy as np
import talib.abstract as ta
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative, stoploss_from_absolute

from coinaudit_common import PROTECTIONS, candles_held, roi_ladder
from signal_log import log_signal


class CoinAuditElliot(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    can_short = False
    minimal_roi = {"0": 100}
    stoploss = -0.25
    use_custom_stoploss = True
    use_exit_signal = True
    process_only_new_candles = True
    startup_candle_count = 400
    trailing_stop = True
    trailing_stop_positive = 0.01
    trailing_stop_positive_offset = 0.03
    trailing_only_offset_is_reached = True
    STOP_ATR, LOW_OFFSET, EWO_HIGH, EWO_LOW = 3.0, 0.978, 5.6, -19.0
    ROI = [(0, 0.20), (30, 0.04), (90, 0.0)]

    @property
    def protections(self):
        return PROTECTIONS

    @informative("1d", "BTC/{stake}")
    def populate_indicators_btc_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["risk_on"] = (dataframe["close"] > dataframe["ema200"]).astype(int)
        return dataframe

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                        current_profit: float, after_fill: bool, **kwargs) -> float | None:
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        rows = df.loc[df["date"] < trade.open_date_utc]
        if rows.empty:
            return None
        return stoploss_from_absolute(trade.open_rate - self.STOP_ATR * rows["atr"].iloc[-1], current_rate,
                                      is_short=trade.is_short, leverage=trade.leverage)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        d = dataframe
        d["ema17"] = ta.EMA(d, timeperiod=17)
        d["ema49"] = ta.EMA(d, timeperiod=49)
        d["ewo"] = (ta.EMA(d, timeperiod=5) - ta.EMA(d, timeperiod=35)) / d["close"] * 100
        d["rsi"] = ta.RSI(d, timeperiod=14)
        d["atr"] = ta.ATR(d, timeperiod=14)
        d["ema20"] = ta.EMA(d, timeperiod=20)
        return d

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        d = dataframe
        below = d["close"] < d["ema17"] * self.LOW_OFFSET
        cond = ((below & (d["ewo"] > self.EWO_HIGH) & (d["rsi"] < 65)) | (below & (d["ewo"] < self.EWO_LOW)))
        cond &= (d["btc_usdt_risk_on_1d"] == 1) & (d["volume"] > 0)
        d.loc[cond, ["enter_long", "enter_tag"]] = (1, "elliot_ewo")
        log_signal(self, d, metadata["pair"], confidence=None, horizon="~48 candles", strategy_name="CoinAuditElliot")
        return d

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] > dataframe["ema49"] * 1.006, ["exit_long", "exit_tag"]] = (1, "above_ema49")
        return dataframe

    def custom_exit(self, pair, trade, current_time, current_rate, current_profit, **kwargs):
        held = candles_held(trade, current_time, self.timeframe)
        if roi_ladder(held, current_profit, self.ROI):
            return "roi_ladder"
        if held >= 120:
            return "time_stop"
        return None
