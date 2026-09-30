# pragma pylint: disable=missing-docstring, invalid-name
"""
CoinAuditSMAOffset — NotAnotherSMAOffset-style dip buying (HO/Protect variant).
Entry: close below SMA14 x LOW_OFFSET with EWO strength (or a deep EWO flush), fast RSI oversold,
BTC risk-on, no recent pump. Exit: close > SMA20 x 1.01 with RSI > 50, trailing stop after +2%,
ROI ladder in candles, 3 x ATR stop. Mirrors sma_offset_v1 in the audit library.
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


class CoinAuditSMAOffset(IStrategy):
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
    trailing_stop_positive_offset = 0.02
    trailing_only_offset_is_reached = True
    STOP_ATR, LOW_OFFSET, EWO_HIGH = 3.0, 0.975, 2.0
    ROI = [(0, 0.08), (30, 0.03), (60, 0.005)]

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
        c = dataframe["close"]
        dataframe["sma14"] = ta.SMA(dataframe, timeperiod=14)
        dataframe["sma20"] = ta.SMA(dataframe, timeperiod=20)
        dataframe["ewo"] = (ta.EMA(dataframe, timeperiod=5) - ta.EMA(dataframe, timeperiod=35)) / c * 100
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["rsi_fast"] = ta.RSI(dataframe, timeperiod=4)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        rng24 = dataframe["high"].rolling(24).max() / dataframe["low"].rolling(24).min() - 1
        dataframe["no_pump"] = rng24 < 8 * dataframe["atr"] / c
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        d = dataframe
        below = d["close"] < d["sma14"] * self.LOW_OFFSET
        e1 = below & (d["ewo"] > self.EWO_HIGH) & (d["rsi"] < 50) & (d["rsi_fast"] < 35)
        e2 = below & (d["ewo"] < -8) & (d["rsi_fast"] < 25)
        cond = (e1 | e2) & d["no_pump"] & (d["btc_usdt_risk_on_1d"] == 1) & (d["volume"] > 0)
        d.loc[cond, ["enter_long", "enter_tag"]] = (1, "sma_offset")
        log_signal(self, d, metadata["pair"], confidence=None, horizon="~60 candles", strategy_name="CoinAuditSMAOffset")
        return d

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        d = dataframe
        d.loc[(d["close"] > d["sma20"] * 1.01) & (d["rsi"] > 50), ["exit_long", "exit_tag"]] = (1, "above_sma")
        return d

    def custom_exit(self, pair, trade, current_time, current_rate, current_profit, **kwargs):
        held = candles_held(trade, current_time, self.timeframe)
        if roi_ladder(held, current_profit, self.ROI):
            return "roi_ladder"
        if held >= 120:
            return "time_stop"
        return None
