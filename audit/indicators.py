"""Technical features via pandas-ta-classic (xgboosted/pandas-ta-classic).
Pure Python — no TA-Lib C library needed on Windows. All indicators are causal
(use only past and current bars); audit.py runs a truncation self-test to prove it."""
import numpy as np
import pandas as pd
import pandas_ta_classic as ta


def max_drawdown(close: pd.Series) -> float:
    return float((close / close.cummax() - 1).min())


def add_all(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if df.empty:
        return df
    c, h, l = df["close"], df["high"], df["low"]
    for n in (20, 50, 200):
        df[f"ema{n}"] = ta.ema(c, length=n)
    df["rsi"] = ta.rsi(c, length=14)
    df["atr"] = ta.atr(h, l, c, length=14)
    df["atr_pct"] = df["atr"] / c
    a = ta.adx(h, l, c, length=14)
    df["adx"], df["pdi"], df["mdi"] = a["ADX_14"], a["DMP_14"], a["DMN_14"]
    m = ta.macd(c, fast=12, slow=26, signal=9)
    df["macd"], df["macd_hist"], df["macd_sig"] = m["MACD_12_26_9"], m["MACDh_12_26_9"], m["MACDs_12_26_9"]
    bb = ta.bbands(c, length=20, std=2)
    df["bbw"] = bb[[col for col in bb.columns if col.startswith("BBB_")][0]] / 100.0
    df["vol_ratio"] = df["quote_volume"] / df["quote_volume"].rolling(30).mean()
    df["taker_ratio"] = (df["taker_buy_quote"] / df["quote_volume"].replace(0, np.nan)).fillna(0.5)
    df["trade_intensity"] = df["trades"] / df["trades"].rolling(96).mean()
    return df
