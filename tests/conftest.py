"""Shared fixtures: everything runs offline on synthetic data (no network, no API keys)."""
from __future__ import annotations

import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

warnings.filterwarnings("ignore")

import indicators as ind  # noqa: E402
import regime as rg  # noqa: E402
import synthetic as sy  # noqa: E402


@pytest.fixture(scope="session")
def universe_dir(tmp_path_factory) -> Path:
    """Three synthetic coins as 15m parquet files (BTC first, others correlated to it)."""
    d = tmp_path_factory.mktemp("universe")
    sy.write_universe(d, {"BTCUSDT": {}, "ETHUSDT": {"price": 2500.0, "base_volume": 2000.0},
                          "DEMOUSDT": {"price": 3.2, "base_volume": 30_000.0}}, days=760, seed=5)
    return d


@pytest.fixture(scope="session")
def h4_frame() -> pd.DataFrame:
    """~1000 4h candles with indicators (regime-switching, seed fixed)."""
    m15 = sy.gbm_ohlcv(96 * 170, "15m", seed=3)
    return ind.add_all(sy.resample(m15, "4h"))


@pytest.fixture(scope="session")
def ctx4(h4_frame) -> dict:
    """A strategy context like audit.audit() builds: BTC close, BTC risk-on, higher-timeframe values."""
    df = h4_frame
    btc = ind.add_all(sy.resample(sy.gbm_ohlcv(96 * 170, "15m", seed=4), "4h"))
    d1 = ind.add_all(sy.resample(sy.gbm_ohlcv(96 * 170, "15m", seed=3), "1d"))
    d1x = d1.assign(risk_on=(d1["close"] > d1["ema200"].fillna(d1["ema50"])).astype(float))
    ctx = {"tf": "4h", "is_btc": False, "btc_close": btc["close"].reindex(df.index).ffill(),
           "btc_risk_on": rg.align(df.index, "4h", d1x, "1d", ["risk_on"])["risk_on"]}
    ha = rg.align(df.index, "4h", d1, "1d", ["close", "ema200"])
    ctx["htf_close"], ctx["htf_ema200"] = ha["close"], ha["ema200"]
    ctx["funding_3d"] = pd.Series(0.0001 + 0.0002 * np.sin(np.arange(len(df)) / 50), index=df.index)
    return ctx


@pytest.fixture
def flat_frame() -> pd.DataFrame:
    """A deterministic OHLC frame for backtester mechanics: prices are integers, ATR is set to 1.0."""
    n = 60
    idx = pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC")
    close = np.full(n, 100.0)
    df = pd.DataFrame({"open": close, "high": close + 0.5, "low": close - 0.5, "close": close,
                       "volume": 1000.0, "quote_volume": 100_000.0, "trades": 100, "taker_buy_base": 500.0,
                       "taker_buy_quote": 50_000.0, "atr": 1.0}, index=idx)
    df["open_time"] = df.index.as_unit("ms").asi8
    df["close_time"] = df["open_time"] + 4 * 3_600_000 - 1
    return df
