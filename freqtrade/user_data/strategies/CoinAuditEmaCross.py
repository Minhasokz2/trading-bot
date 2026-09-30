# pragma pylint: disable=missing-docstring, invalid-name
"""
CoinAuditEmaCross — the review's Phase-1 baseline: EMA cross + volume/ATR filter + BTC regime.
(btc-strategy-lab's surviving rule adds a funding filter; the audit tests that version with real
funding data — Freqtrade spot mode has no funding feed, so here BTC regime stands in.)
Entry: EMA21 > EMA55, volume above its 30-candle mean, ATR% below its 90th percentile, BTC risk-on.
Exit: EMA21 < EMA55. Stop 3 x ATR.
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


class CoinAuditEmaCross(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    can_short = False
    minimal_roi = {"0": 100}
    stoploss = -0.25
    use_custom_stoploss = True
    use_exit_signal = True
    process_only_new_candles = True
    startup_candle_count = 400
    STOP_ATR = 3.0

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
        d["ema_f"], d["ema_s"] = ta.EMA(d, timeperiod=21), ta.EMA(d, timeperiod=55)
        d["atr"] = ta.ATR(d, timeperiod=14)
        d["ema20"] = ta.EMA(d, timeperiod=20)
        atr_pct = d["atr"] / d["close"]
        d["vol_ok"] = d["volume"] > d["volume"].rolling(30).mean()
        d["atr_ok"] = atr_pct < atr_pct.rolling(500, min_periods=100).quantile(0.9)
        return d

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        d = dataframe
        cross = (d["ema_f"] > d["ema_s"]) & (d["ema_f"].shift() <= d["ema_s"].shift())
        cond = cross & d["vol_ok"] & d["atr_ok"] & (d["btc_usdt_risk_on_1d"] == 1) & (d["volume"] > 0)
        d.loc[cond, ["enter_long", "enter_tag"]] = (1, "ema_cross")
        log_signal(self, d, metadata["pair"], confidence=None, horizon="~80 candles", strategy_name="CoinAuditEmaCross")
        return d

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["ema_f"] < dataframe["ema_s"], ["exit_long", "exit_tag"]] = (1, "ema_cross_down")
        return dataframe
