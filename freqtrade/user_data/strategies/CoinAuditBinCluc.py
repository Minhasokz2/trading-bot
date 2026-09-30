# pragma pylint: disable=missing-docstring, invalid-name
"""
CoinAuditBinCluc — CombinedBinHAndCluc / BinClucMad family.
Entry: BinHV45 (sharp drop below BB40 lower with a short tail) OR ClucMay (below EMA50 and
0.985 x BB20 lower on quiet volume), BTC risk-on. Exit: close > BB20 middle, ROI ladder,
72-candle time stop, 2 x ATR stop. Mirrors bincluc_bb_v1 in the audit library.
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


class CoinAuditBinCluc(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    can_short = False
    minimal_roi = {"0": 100}
    stoploss = -0.25
    use_custom_stoploss = True
    use_exit_signal = True
    process_only_new_candles = True
    startup_candle_count = 400
    STOP_ATR, CLUC_MULT = 2.0, 0.985
    ROI = [(0, 0.05), (24, 0.02), (48, 0.0)]

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
        bb40 = ta.BBANDS(d, timeperiod=40, nbdevup=2.0, nbdevdn=2.0)
        bb20 = ta.BBANDS(d, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
        d["lower40"], d["mid40"] = bb40["lowerband"], bb40["middleband"]
        d["lower20"], d["mid20"] = bb20["lowerband"], bb20["middleband"]
        d["bbdelta"] = (d["mid40"] - d["lower40"]).abs()
        d["closedelta"] = (d["close"] - d["close"].shift()).abs()
        d["tail"] = (d["close"] - d["low"]).abs()
        d["ema50"] = ta.EMA(d, timeperiod=50)
        d["vol_mean30"] = d["volume"].rolling(30).mean()
        d["atr"] = ta.ATR(d, timeperiod=14)
        d["ema20"] = ta.EMA(d, timeperiod=20)
        return d

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        d = dataframe
        binh = ((d["lower40"].shift() > 0) & (d["bbdelta"] > d["close"] * 0.008)
                & (d["closedelta"] > d["close"] * 0.0175) & (d["tail"] < d["bbdelta"] * 0.25)
                & (d["close"] < d["lower40"].shift()) & (d["close"] <= d["close"].shift()))
        cluc = ((d["close"] < d["ema50"]) & (d["close"] < self.CLUC_MULT * d["lower20"])
                & (d["volume"] < d["vol_mean30"] * 20))
        cond = (binh | cluc) & (d["btc_usdt_risk_on_1d"] == 1) & (d["volume"] > 0)
        d.loc[cond, ["enter_long", "enter_tag"]] = (1, "bincluc")
        log_signal(self, d, metadata["pair"], confidence=None, horizon="~36 candles", strategy_name="CoinAuditBinCluc")
        return d

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] > dataframe["mid20"], ["exit_long", "exit_tag"]] = (1, "bb_mid")
        return dataframe

    def custom_exit(self, pair, trade, current_time, current_rate, current_profit, **kwargs):
        held = candles_held(trade, current_time, self.timeframe)
        if roi_ladder(held, current_profit, self.ROI):
            return "roi_ladder"
        if held >= 72:
            return "time_stop"
        return None
