"""Offline client, point-in-time cuts, closed-candle rule, resampling, synthetic ticker/book."""
import numpy as np
import pandas as pd
import pytest

import binance_client as bc
import synthetic as sy


def test_all_timeframes_are_resampled_and_aligned(universe_dir):
    c = bc.OfflineClient(universe_dir)
    assert {"BTCUSDT", "ETHUSDT", "DEMOUSDT"} <= c.all_symbols()
    m15 = c.klines("DEMOUSDT", "15m", 96 * 30)
    d1 = c.klines("DEMOUSDT", "1d", 30)
    day = d1.index[-2]
    part = m15[(m15.index >= day) & (m15.index < day + pd.Timedelta(days=1))]
    row = d1.loc[day]
    assert row["open"] == pytest.approx(part["open"].iloc[0]) and row["close"] == pytest.approx(part["close"].iloc[-1])
    assert row["high"] == pytest.approx(part["high"].max()) and row["low"] == pytest.approx(part["low"].min())
    assert row["quote_volume"] == pytest.approx(part["quote_volume"].sum())
    assert int(row["close_time"]) - int(row["open_time"]) == 86_400_000 - 1
    w = c.klines("DEMOUSDT", "1w", 5)
    assert all(t.day_name() == "Monday" for t in w.index)
    assert d1["open_time"].iloc[-1] == d1.index[-1].as_unit("ms").asi8    # epoch ms, whatever the resolution


def test_as_of_hides_the_future_and_rebuilds_ticker(universe_dir):
    live = bc.OfflineClient(universe_dir)
    pit = bc.OfflineClient(universe_dir, as_of="2024-06-01T13:00:00Z")
    cut = pd.Timestamp("2024-06-01T13:00:00Z")
    for tf in ("15m", "1h", "4h", "1d", "1w"):
        k = pit.klines("DEMOUSDT", tf, 50)
        assert (pd.to_datetime(k["close_time"], unit="ms", utc=True) <= cut).all()
        assert k.index[-1] < cut
    assert pit.klines("DEMOUSDT", "4h", 5).index[-1] == pd.Timestamp("2024-06-01T08:00:00Z")
    t_live, t_pit = live.ticker_24h("DEMOUSDT"), pit.ticker_24h("DEMOUSDT")
    assert t_live["lastPrice"] != t_pit["lastPrice"] and t_pit["synthetic"]
    assert float(t_pit["quoteVolume"]) > 0
    info = pit.symbol_info("DEMOUSDT")
    assert info["status"] == "TRADING" and info["baseAsset"] == "DEMO"
    assert pit.symbol_info("NOPEUSDT") is None


def test_synthetic_book_depth_matches_volume_ratio():
    b = bc.synthetic_book(100.0, quote_volume_24h=10_000_000, spread_bps=5.0, depth_ratio=0.02)
    bids = [(float(p), float(q)) for p, q in b["bids"]]
    asks = [(float(p), float(q)) for p, q in b["asks"]]
    spread_bps = (asks[0][0] - bids[0][0]) / 100.0 * 1e4
    assert spread_bps == pytest.approx(5.0, abs=0.01)
    depth = sum(p * q for p, q in bids if p >= 99.0) + sum(p * q for p, q in asks if p <= 101.0)
    assert depth == pytest.approx(200_000, rel=0.02)


def test_forming_candle_is_dropped():
    rows = [[1_700_000_000_000 + i * 3_600_000, "1", "1", "1", "1", "1", 1_700_000_000_000 + (i + 1) * 3_600_000 - 1,
             "1", 1, "0.5", "0.5", "0"] for i in range(5)]
    df = bc.to_frame(rows)
    assert len(bc.drop_forming(df, now_ms=1_700_000_000_000 + 4 * 3_600_000 + 10)) == 4
    assert len(bc.drop_forming(df, now_ms=1_700_000_000_000 + 5 * 3_600_000)) == 5
    assert len(bc.drop_forming(df, now_ms=1_700_000_000_000 + 5 * 3_600_000 - 1)) == 4   # closes AT now-1ms: still forming


def test_offline_futures_client(universe_dir):
    f = bc.OfflineFuturesClient(universe_dir, as_of="2024-06-01T13:00:00Z")
    h = f.funding_history("DEMOUSDT", 90)
    assert len(h) == 90 and all("fundingRate" in x for x in h)
    assert max(int(x["fundingTime"]) for x in h) <= bc.to_ms("2024-06-01T13:00:00Z")
    m = f.mark_price("DEMOUSDT")
    assert float(m["markPrice"]) > 0 and float(m["indexPrice"]) > 0
    s = f.funding_series("DEMOUSDT", bc.to_ms("2024-01-01"))
    assert s.index[0] >= pd.Timestamp("2024-01-01", tz="UTC") and s.index[-1] <= pd.Timestamp("2024-06-01T13:00Z")
    oi = f.api.open_interest_statistics("DEMOUSDT", limit=10)
    assert len(oi) == 10 and float(oi[0]["sumOpenInterestValue"]) > 0


def test_to_ms_accepts_common_inputs():
    assert bc.to_ms(None) is None and bc.to_ms("") is None
    assert bc.to_ms(1_700_000_000_000) == 1_700_000_000_000
    assert bc.to_ms("2024-01-01") == 1_704_067_200_000
    assert bc.to_ms("2024-01-01T00:00:00+02:00") == 1_704_067_200_000 - 2 * 3_600_000


def test_tradable_symbol_filter():
    assert bc.is_tradable_symbol("SOLUSDT", "USDT") and not bc.is_tradable_symbol("SOLUPUSDT", "USDT")
    assert not bc.is_tradable_symbol("USDCUSDT", "USDT") and not bc.is_tradable_symbol("SOLBTC", "USDT")


def test_synthetic_generator_shape_and_sanity():
    df = sy.gbm_ohlcv(2000, "1h", seed=2)
    assert list(df.columns) == sy.KLINE_COLS
    assert (df["high"] >= df[["open", "close"]].max(axis=1)).all() and (df["low"] <= df[["open", "close"]].min(axis=1)).all()
    assert (df["taker_buy_base"] <= df["volume"]).all() and (df["quote_volume"] > 0).all()
    assert df.index.freq is not None or (np.diff(df["open_time"].values) == 3_600_000).all()
