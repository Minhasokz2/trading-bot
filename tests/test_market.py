"""Crypto-wide + macro layer with every external source mocked (CoinGecko, DefiLlama, FRED)."""
import numpy as np
import pandas as pd
import pytest

import binance_client as bc
import indicators as ind
import market as mk
import patterns as pt


@pytest.fixture
def mocked_sources(monkeypatch, tmp_path, universe_dir):
    monkeypatch.setattr(mk, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(mk, "_cached", lambda name, max_age_h, fn: fn())      # no disk cache in tests
    coins = [{"id": "bitcoin", "name": "Bitcoin", "symbol": "btc", "current_price": 40000, "circulating_supply": 19.7e6,
              "market_cap": 7.9e11, "market_cap_rank": 1, "fully_diluted_valuation": 8.4e11},
             {"id": "ethereum", "name": "Ethereum", "symbol": "eth", "current_price": 2500, "circulating_supply": 120e6,
              "market_cap": 3e11, "market_cap_rank": 2, "fully_diluted_valuation": 3e11},
             {"id": "tether", "name": "Tether", "symbol": "usdt", "current_price": 1.0, "circulating_supply": 1.1e11,
              "market_cap": 1.1e11, "market_cap_rank": 3},
             {"id": "usd-coin", "name": "USDC", "symbol": "usdc", "current_price": 1.0, "circulating_supply": 3e10,
              "market_cap": 3e10, "market_cap_rank": 5},
             {"id": "wrapped-bitcoin", "name": "Wrapped Bitcoin", "symbol": "wbtc", "current_price": 40000,
              "circulating_supply": 1.5e5, "market_cap": 6e9, "market_cap_rank": 12},
             {"id": "demo", "name": "Demo Coin", "symbol": "demo", "current_price": 3.0, "circulating_supply": 5e8,
              "market_cap": 1.5e9, "market_cap_rank": 40, "fully_diluted_valuation": 3e9}]
    monkeypatch.setattr(mk, "coingecko_markets", lambda: coins)
    monkeypatch.setattr(mk, "coingecko_global", lambda: {"total_market_cap": {"usd": 1.3e12}})
    idx = pd.date_range("2022-06-01", periods=1200, freq="1D", tz="UTC")
    st = pd.DataFrame({"ALL": np.linspace(1.3e11, 1.5e11, 1200), "USDT": np.linspace(1.0e11, 1.1e11, 1200),
                       "USDC": np.linspace(3e10, 3.2e10, 1200)}, index=idx)
    monkeypatch.setattr(mk, "stablecoins", lambda: st)
    rng = np.random.default_rng(0)

    def fake_fred(sid):
        n = 400
        base = {"VIXCLS": 18.0, "DGS10": 4.2, "DGS2": 4.0, "T10Y2Y": 0.2, "DFII10": 1.9}.get(sid, 100.0)
        s = pd.Series(base * np.exp(np.cumsum(rng.normal(0, 0.002, n))), index=idx[-n:])
        return s
    monkeypatch.setattr(mk, "fred", fake_fred)
    return universe_dir


def _frames(client, sym):
    return {tf: ind.add_all(client.klines(sym, tf, {"15m": 3000, "1h": 1000, "4h": 1000, "1d": 1000}[tf]))
            for tf in ("15m", "1h", "4h", "1d")}


def test_indices_regime_and_gate(mocked_sources):
    client = bc.OfflineClient(mocked_sources)
    fut = bc.OfflineFuturesClient(mocked_sources)
    syms = client.all_symbols()
    cf, bf = _frames(client, "DEMOUSDT"), _frames(client, "BTCUSDT")
    liq = {"quote_volume_24h": 30e6, "spread_bps": 4.0, "depth_1pct_usd": 800_000, "book_bid_share": 0.5}
    m = mk.snapshot(client, fut, "DEMOUSDT", "DEMO", cf, bf, liq, syms)
    assert m["available"] and m["coins_in_index"] == 3 and m["stables_source"] == "DefiLlama"
    names = [d["symbol"] for d in m["dashboard"]]
    assert {"TOTAL", "TOTAL2", "TOTAL3", "BTC.D", "USDT.D", "OTHERS.D", "BTC.D+USDT.D", "BTCUSDT"} <= set(names)
    assert m["coverage_vs_coingecko"] > 0                     # synthetic prices, so the ratio is arbitrary
    assert "VIX" in m["macro"] and m["macro"]["US10Y"]["unit"] == "pp"
    c = m["coin"]
    assert c["market_cap"] == 1.5e9 and "open_interest_usd" in c and "corr_btc_90d" in c
    assert set(c["performance"]) == {"15m", "1h", "4h", "24h", "7d", "30d", "90d"}
    d1 = m["series_1d"]
    # identity: TOTAL3ES + BTC + ETH + all stablecoins = TOTAL (dominances are shares of TOTAL in %)
    rebuilt = d1["TOTAL3ES"] + d1["TOTAL"] * (d1["BTC.D"] + d1["ETH.D"] + d1["STABLE.D"]) / 100
    assert np.allclose(rebuilt, d1["TOTAL"], rtol=1e-6)
    assert ((d1["BTC.D"] + d1["ETH.D"] + d1["STABLE.D"]) <= 100.0001).all() and (d1["OTHERS.D"] >= 0).all()
    bh4 = bf["4h"]
    hi, lo = pt.pivots(bh4)
    reg = mk.classify(m, bh4, bf["1d"], False, True, pt.structure(bh4, hi, lo, len(bh4) - 1))
    assert reg["regime"] in {"High-volatility event", "BTC selloff", "Risk-off / stablecoin rotation", "Altcoin rotation",
                             "Broad risk-on", "BTC-led rally", "BTC recovery, alts weak", "Range / chop", "Mixed"}
    assert 0 <= reg["alt_score"] <= 100 and len(reg["alt_items"]) == 15 and len(reg["btc_items"]) == 11
    row = {"family": "trend", "status": "ACCEPTED"}
    why = mk.gate(row, {**reg, "regime": "BTC selloff", "alt_score": 80}, False, [])
    assert any("selloff" in w for w in why)
    assert mk.gate(row, {**reg, "regime": "Mixed", "alt_score": 40}, False, []) == ["altcoin eligibility score 40 < 50"]
    assert mk.gate({"family": "trend", "status": "CANDIDATE"}, {**reg, "regime": "Mixed", "alt_score": 60}, False, [])
    assert mk.gate(row, {**reg, "regime": "Range / chop", "alt_score": 80}, False, []) == ["range/chop — trend and breakout modules paused"]
    assert mk.gate({"family": "range", "status": "ACCEPTED"}, {**reg, "regime": "Range / chop", "alt_score": 80}, False, []) == []
    assert mk.gate(row, None, False, [{"pattern": "head_shoulders"}]) == ["fresh bearish pattern: head_shoulders"]


def test_events_file_feeds_penalties(mocked_sources, monkeypatch, tmp_path):
    ev = tmp_path / "events.csv"
    soon = (pd.Timestamp.now(tz="UTC") + pd.Timedelta(hours=10)).strftime("%Y-%m-%d %H:%M")
    ev.write_text(f"# comment\ndate_utc,event,type,coin\n{soon},FOMC,macro,\n{soon},unlock,unlock,DEMO\n")
    monkeypatch.setattr(mk, "EVENTS", ev)
    e = mk.load_events()
    assert len(e) == 2
    client = bc.OfflineClient(mocked_sources)
    cf, bf = _frames(client, "DEMOUSDT"), _frames(client, "BTCUSDT")
    m = mk.snapshot(client, None, "DEMOUSDT", "DEMO", cf, bf, {"quote_volume_24h": 30e6}, client.all_symbols())
    assert len(m["events_48h"]) == 2 and len(m["coin_events_7d"]) == 1
    reg = mk.classify(m, bf["4h"], bf["1d"], False, True, "bullish")
    assert reg["regime"] == "High-volatility event"           # a macro event within 24h pauses entries
    assert next(i for i in reg["alt_items"] if "event" in i["condition"])["met"]


def test_unavailable_sources_degrade_gracefully(monkeypatch, universe_dir):
    monkeypatch.setattr(mk, "coingecko_markets", lambda: None)
    client = bc.OfflineClient(universe_dir)
    m = mk.snapshot(client, None, "DEMOUSDT", "DEMO", {}, {}, {}, client.all_symbols())
    assert not m["available"] and "CoinGecko" in m["reason"]
