# pragma pylint: disable=missing-docstring, invalid-name
"""
CoinAuditConfluence — TrendRider-style multi-indicator confluence score (0-6):
EMA20>50>200, RSI 50-70, ADX>20 with +DI>-DI, volume > 1.2x average, close above BB middle with
widening bands, MACD histogram positive and rising. Entry: score >= MIN_SCORE, BTC risk-on.
Exit: score <= 2, trailing after +5%, cascading early-loss cuts (3 candles -1 ATR, 6 candles
-0.5 ATR, 12 candles not profitable = "deadfish"), 60-candle time stop, 2 x ATR stop.
Timeframe defaults to 4h; override with --timeframe. Research strategy — dry-run only.
"""
from datetime import datetime

import numpy as np
import talib.abstract as ta
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative, stoploss_from_absolute

from coinaudit_common import PROTECTIONS, candles_held, roi_ladder
from signal_log import log_signal


class CoinAuditConfluence(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "4h"
    can_short = False
    minimal_roi = {"0": 100}
    stoploss = -0.25
    use_custom_stoploss = True
    use_exit_signal = True
    process_only_new_candles = True
    startup_candle_count = 400
    trailing_stop = True
    trailing_stop_positive = 0.025
    trailing_stop_positive_offset = 0.05
    trailing_only_offset_is_reached = True
    STOP_ATR, MIN_SCORE = 2.0, 4
    EARLY_CUT = [(3, 1.0), (6, 0.5), (12, 0.0)]

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
        for n in (20, 50, 200):
            d[f"ema{n}"] = ta.EMA(d, timeperiod=n)
        d["rsi"] = ta.RSI(d, timeperiod=14)
        d["adx"] = ta.ADX(d, timeperiod=14)
        d["pdi"], d["mdi"] = ta.PLUS_DI(d, timeperiod=14), ta.MINUS_DI(d, timeperiod=14)
        d["atr"] = ta.ATR(d, timeperiod=14)
        bb = ta.BBANDS(d, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
        d["bbw"] = (bb["upperband"] - bb["lowerband"]) / bb["middleband"]
        macd = ta.MACD(d)
        d["macdhist"] = macd["macdhist"]
        vol_ratio = d["volume"] / d["volume"].rolling(30).mean()
        d["score"] = (((d["ema20"] > d["ema50"]) & (d["ema50"] > d["ema200"])).astype(int)
                      + d["rsi"].between(50, 70).astype(int)
                      + ((d["adx"] > 20) & (d["pdi"] > d["mdi"])).astype(int)
                      + (vol_ratio > 1.2).astype(int)
                      + ((d["close"] > bb["middleband"]) & (d["bbw"] > d["bbw"].shift(5))).astype(int)
                      + ((d["macdhist"] > 0) & (d["macdhist"] > d["macdhist"].shift())).astype(int))
        return d

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        d = dataframe
        cond = (d["score"] >= self.MIN_SCORE) & (d["btc_usdt_risk_on_1d"] == 1) & (d["volume"] > 0)
        d.loc[cond, ["enter_long", "enter_tag"]] = (1, "confluence")
        log_signal(self, d, metadata["pair"], confidence=None, horizon="~40 candles", strategy_name="CoinAuditConfluence")
        return d

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["score"] <= 2, ["exit_long", "exit_tag"]] = (1, "score_faded")
        return dataframe

    def custom_exit(self, pair, trade, current_time, current_rate, current_profit, **kwargs):
        held = candles_held(trade, current_time, self.timeframe)
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        rows = df.loc[df["date"] < trade.open_date_utc]
        if not rows.empty:
            atr_frac = rows["atr"].iloc[-1] / trade.open_rate
            for b, l in self.EARLY_CUT:
                if held >= b and current_profit <= -l * atr_frac:
                    return f"early_cut_{b}"
        if held >= 60:
            return "time_stop"
        return None
