# pragma pylint: disable=missing-docstring, invalid-name
"""
CoinAuditPullback — trend pullback built from NFI-inspired MODULES (not NFI's rule tree).
Mirrors pullback_nfi_modules_v1 in the audit library. Each module can be switched off
in MODULES below to run the same ablation test inside Freqtrade.

  trend_filter         pair's daily close above its daily EMA200
  btc_regime           BTC daily close above its EMA200
  safe_dip             RSI < RSI_TH and price >= 1 ATR below EMA20
  pump_guard           24-candle range < 8 x ATR%  (skip after pumps)
  volume_confirmation  volume not dead, taker selling not overwhelming
  ewo_guard            Elliott Wave Oscillator above its trailing 5th percentile (not in free fall)

Exits: close back above EMA20 or RSI > 65 (profit protection), 24-candle time stop,
stop 2.5 x ATR. Timeframe defaults to 1h; override with --timeframe 15m / 4h.
"""
from datetime import datetime

import talib.abstract as ta
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative, stoploss_from_absolute

from coinaudit_common import PROTECTIONS
from signal_log import log_signal

MODULES = {"trend_filter": True, "btc_regime": True, "safe_dip": True,
           "pump_guard": True, "volume_confirmation": True, "ewo_guard": True}
RSI_TH, STOP_ATR, TIME_STOP = 35, 2.5, 24


class CoinAuditPullback(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    can_short = False
    minimal_roi = {"0": 100}
    stoploss = -0.15
    use_custom_stoploss = True
    use_exit_signal = True
    process_only_new_candles = True
    startup_candle_count = 400

    @property
    def protections(self):
        return PROTECTIONS

    @informative("1d", "BTC/{stake}")
    def populate_indicators_btc_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["risk_on"] = (dataframe["close"] > dataframe["ema200"]).astype(int)
        return dataframe

    @informative("1d")
    def populate_indicators_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        c = dataframe["close"]
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        atr_pct = dataframe["atr"] / c
        rng24 = dataframe["high"].rolling(24).max() / dataframe["low"].rolling(24).min() - 1
        ewo = (ta.EMA(dataframe, timeperiod=5) - ta.EMA(dataframe, timeperiod=35)) / c * 100
        vol_ratio = dataframe["volume"] / dataframe["volume"].rolling(30).mean()
        d1_ema = dataframe["ema200_1d"]
        dataframe["m_trend_filter"] = (dataframe["close_1d"] > d1_ema) | d1_ema.isna() & (c > ta.EMA(dataframe, timeperiod=200))
        dataframe["m_btc_regime"] = dataframe["btc_usdt_risk_on_1d"] == 1
        dataframe["m_safe_dip"] = (dataframe["rsi"] < RSI_TH) & ((dataframe["ema20"] - c) / dataframe["atr"] >= 1.0)
        dataframe["m_pump_guard"] = rng24 < 8 * atr_pct
        dataframe["m_volume_confirmation"] = vol_ratio > 0.7
        dataframe["m_ewo_guard"] = ewo > ewo.rolling(200, min_periods=50).quantile(0.05)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        cond = dataframe["volume"] > 0
        for name, on in MODULES.items():
            if on:
                cond &= dataframe[f"m_{name}"].fillna(False).astype(bool)
        dataframe.loc[cond, ["enter_long", "enter_tag"]] = (1, "pullback")
        log_signal(self, dataframe, metadata["pair"], confidence=None,
                   horizon=f"up to {TIME_STOP} x {self.timeframe}", strategy_name="CoinAuditPullback")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(dataframe["close"] > dataframe["ema20"]) | (dataframe["rsi"] > 65),
                      ["exit_long", "exit_tag"]] = (1, "reverted")
        return dataframe

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                    current_profit: float, **kwargs):
        from freqtrade.exchange import timeframe_to_minutes
        if (current_time - trade.open_date_utc).total_seconds() >= TIME_STOP * timeframe_to_minutes(self.timeframe) * 60:
            return "time_stop"
        return None

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                        current_profit: float, after_fill: bool, **kwargs) -> float | None:
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        rows = df.loc[df["date"] < trade.open_date_utc]
        if rows.empty:
            return None
        return stoploss_from_absolute(trade.open_rate - STOP_ATR * rows["atr"].iloc[-1], current_rate,
                                      is_short=trade.is_short, leverage=trade.leverage)
