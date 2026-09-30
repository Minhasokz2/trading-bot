# pragma pylint: disable=missing-docstring, invalid-name
"""
CoinAuditTrend — the non-ML baseline (same rules as the audit tool's backtest).

Entry (4h): close > EMA50 > EMA200, RSI > 50, ADX > 20 with +DI > -DI,
            and BTC daily close above its EMA200 (market risk-on).
Exit:       4h close below EMA50.
Stop:       2 x ATR below entry (fixed at entry), hard floor -15%.

Spot, long only. Signals are written to user_data/signals/signals.csv in dry/live mode.
"""
from datetime import datetime

import talib.abstract as ta
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative, stoploss_from_absolute

from coinaudit_common import PROTECTIONS
from signal_log import log_signal


class CoinAuditTrend(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "4h"
    can_short = False
    minimal_roi = {"0": 100}          # exits are signal/stop driven, not ROI
    stoploss = -0.15
    use_custom_stoploss = True
    process_only_new_candles = True
    startup_candle_count = 400

    @property
    def protections(self):
        return PROTECTIONS
    use_exit_signal = True

    @informative("1d", "BTC/{stake}")
    def populate_indicators_btc_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["risk_on"] = (dataframe["close"] > dataframe["ema200"]).astype(int)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["pdi"] = ta.PLUS_DI(dataframe, timeperiod=14)
        dataframe["mdi"] = ta.MINUS_DI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        cond = (
            (dataframe["close"] > dataframe["ema50"])
            & (dataframe["ema50"] > dataframe["ema200"])
            & (dataframe["rsi"] > 50)
            & (dataframe["adx"] > 20)
            & (dataframe["pdi"] > dataframe["mdi"])
            & (dataframe["btc_usdt_risk_on_1d"] == 1)
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[cond, ["enter_long", "enter_tag"]] = (1, "trend")
        log_signal(self, dataframe, metadata["pair"], confidence=None,
                   horizon="2-10 days", strategy_name="CoinAuditTrend")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] < dataframe["ema50"], ["exit_long", "exit_tag"]] = (1, "below_ema50")
        return dataframe

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                        current_profit: float, after_fill: bool, **kwargs) -> float | None:
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        signal_rows = dataframe.loc[dataframe["date"] < trade.open_date_utc]
        if signal_rows.empty:
            return None
        atr = signal_rows["atr"].iloc[-1]
        stop_price = trade.open_rate - 2.0 * atr
        return stoploss_from_absolute(stop_price, current_rate, is_short=trade.is_short,
                                      leverage=trade.leverage)
