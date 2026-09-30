# pragma pylint: disable=missing-docstring, invalid-name
"""
CoinAuditFreqAI — supervised ML signals with FreqAI + LightGBMClassifier.

Label (triple-barrier, long only, 1h candles): starting at the next candle's open,
"win" if price rises +2 x ATR before it falls -1 x ATR within 24 candles, else "loss".
A candle that touches both counts as a loss (conservative). The label is the only
place future data is used; features use only past/current candles.

FreqAI retrains on a sliding window (train_period_days) and, in backtesting,
simulates that retraining so every prediction is out-of-sample.

Entry: model trusts the data point (do_predict == 1, inside the training distribution),
       P(win) >= ENTRY_PROB, and BTC daily close above EMA200 (risk-on).
Exit:  +2 x ATR target, -1 x ATR stop, 24-candle timeout, or P(win) collapses.

Run with:  --config config.json --config config-freqai.json --freqaimodel LightGBMClassifier
"""
from datetime import datetime

import numpy as np
import talib.abstract as ta
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative, stoploss_from_absolute

from coinaudit_common import PROTECTIONS
from signal_log import log_signal

TP_ATR, SL_ATR, HORIZON = 2.0, 1.0, 24
ENTRY_PROB = 0.55


class CoinAuditFreqAI(IStrategy):
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

    # ----------------------------------------------------------------- regime
    @informative("1d", "BTC/{stake}")
    def populate_indicators_btc_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["risk_on"] = (dataframe["close"] > dataframe["ema200"]).astype(int)
        return dataframe

    # --------------------------------------------------------------- features
    def feature_engineering_expand_all(self, dataframe: DataFrame, period: int,
                                       metadata: dict, **kwargs) -> DataFrame:
        """Expanded over indicator_periods_candles, include_timeframes and corr pairs."""
        dataframe["%-rsi-period"] = ta.RSI(dataframe, timeperiod=period)
        dataframe["%-adx-period"] = ta.ADX(dataframe, timeperiod=period)
        dataframe["%-mfi-period"] = ta.MFI(dataframe, timeperiod=period)
        atr = ta.ATR(dataframe, timeperiod=period)
        dataframe["%-atr_pct-period"] = atr / dataframe["close"]
        ema = ta.EMA(dataframe, timeperiod=period)
        dataframe["%-dist_ema-period"] = (dataframe["close"] - ema) / atr
        dataframe["%-roc-period"] = ta.ROC(dataframe, timeperiod=period)
        bb = ta.BBANDS(dataframe, timeperiod=period, nbdevup=2.0, nbdevdn=2.0)
        up, mid, low = bb["upperband"], bb["middleband"], bb["lowerband"]
        dataframe["%-bb_width-period"] = (up - low) / mid
        dataframe["%-bb_pos-period"] = (dataframe["close"] - low) / (up - low)
        dataframe["%-rel_volume-period"] = dataframe["volume"] / dataframe["volume"].rolling(period).mean()
        return dataframe

    def feature_engineering_expand_basic(self, dataframe: DataFrame, metadata: dict,
                                         **kwargs) -> DataFrame:
        dataframe["%-pct_change"] = dataframe["close"].pct_change()
        dataframe["%-range_pct"] = (dataframe["high"] - dataframe["low"]) / dataframe["close"]
        dataframe["%-dist_ema200"] = dataframe["close"] / ta.EMA(dataframe, timeperiod=200) - 1
        return dataframe

    def feature_engineering_standard(self, dataframe: DataFrame, metadata: dict,
                                     **kwargs) -> DataFrame:
        dataframe["%-day_of_week"] = dataframe["date"].dt.dayofweek
        dataframe["%-hour_of_day"] = dataframe["date"].dt.hour
        return dataframe

    # ------------------------------------------------------------------ label
    def set_freqai_targets(self, dataframe: DataFrame, metadata: dict, **kwargs) -> DataFrame:
        self.freqai.class_names = ["loss", "win"]
        o, hi, lo = dataframe["open"].values, dataframe["high"].values, dataframe["low"].values
        atr = ta.ATR(dataframe, timeperiod=14).values
        n = len(dataframe)
        lab = np.full(n, np.nan, dtype=object)
        for i in range(n - HORIZON - 1):
            if np.isnan(atr[i]):
                continue
            entry = o[i + 1]
            tp, sl = entry + TP_ATR * atr[i], entry - SL_ATR * atr[i]
            out = "loss"
            for j in range(i + 1, i + 1 + HORIZON):
                if lo[j] <= sl:
                    break
                if hi[j] >= tp:
                    out = "win"
                    break
            lab[i] = out
        dataframe["&-target"] = lab
        return dataframe

    # ------------------------------------------------------------- indicators
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = self.freqai.start(dataframe, metadata, self)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        prob = dataframe["win"] if "win" in dataframe else 0
        cond = (
            (dataframe["do_predict"] == 1)
            & (prob >= ENTRY_PROB)
            & (dataframe["btc_usdt_risk_on_1d"] == 1)
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[cond, ["enter_long", "enter_tag"]] = (1, "freqai_win")
        conf = dataframe["win"].iloc[-1] if "win" in dataframe and len(dataframe) else None
        log_signal(self, dataframe, metadata["pair"], confidence=conf,
                   horizon=f"up to {HORIZON}h", strategy_name="CoinAuditFreqAI")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        if "win" in dataframe:
            dataframe.loc[(dataframe["do_predict"] == 1) & (dataframe["win"] < 0.30),
                          ["exit_long", "exit_tag"]] = (1, "prob_collapse")
        return dataframe

    # ----------------------------------------------------- barriers as exits
    def _entry_atr(self, pair: str, trade: Trade):
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        rows = df.loc[df["date"] < trade.open_date_utc]
        return None if rows.empty else float(rows["atr"].iloc[-1])

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                        current_profit: float, after_fill: bool, **kwargs) -> float | None:
        atr = self._entry_atr(pair, trade)
        if not atr:
            return None
        return stoploss_from_absolute(trade.open_rate - SL_ATR * atr, current_rate,
                                      is_short=trade.is_short, leverage=trade.leverage)

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                    current_profit: float, **kwargs):
        atr = self._entry_atr(pair, trade)
        if atr and current_rate >= trade.open_rate + TP_ATR * atr:
            return "target_2atr"
        if (current_time - trade.open_date_utc).total_seconds() >= HORIZON * 3600:
            return "timeout"
        return None
