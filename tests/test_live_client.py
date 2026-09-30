"""The live BinanceClient code path (pagination, parquet cache, forming-candle rule, point-in-time view, the
scanner's ticker list) run against a stand-in for the SDK — the path a hosted audit takes, which no other
test covered. The first test is the production failure: a fresh cache used to return object-typed columns."""
from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest

import audit as au
import binance_client as bc
import synthetic as sy
from fake_binance import FakeSpotApi, Resp

H4 = bc.TF_MS["4h"]
M15 = bc.TF_MS["15m"]


def recent_start(n_15m: int, tf_ms: int = M15) -> str:
    """A start date such that the last of `n_15m` 15m candles closed a little before now (a "live" feed)."""
    now_ms = int(time.time() * 1000)
    end_open = (now_ms // H4) * H4 - H4        # the feed ends one full 4h block ago: closed, but not "stale"
    return pd.Timestamp(end_open - n_15m * tf_ms, unit="ms", tz="UTC").isoformat()


@pytest.fixture(scope="module")
def live_api() -> FakeSpotApi:
    n = 96 * 420
    start = recent_start(n)
    return FakeSpotApi({"DEMOUSDT": sy.gbm_ohlcv(n, "15m", seed=3, start=start, price=3.2, base_volume=30_000.0),
                        "BTCUSDT": sy.gbm_ohlcv(n, "15m", seed=5, start=start, price=60_000.0, base_volume=20.0),
                        "ETHUSDT": sy.gbm_ohlcv(n, "15m", seed=7, start=start, price=2500.0, base_volume=2000.0)})


def contiguous(df: pd.DataFrame, step_ms: int) -> bool:
    ot = df["open_time"].to_numpy()
    return bool(len(df) and np.all(np.diff(ot) == step_ms))


def assert_typed(df: pd.DataFrame):
    for c in bc.FLOAT_COLS:
        assert df[c].dtype == np.float64, (c, df[c].dtype)
    for c in bc.INT_COLS:
        assert df[c].dtype == np.int64, (c, df[c].dtype)
    assert isinstance(df.index, pd.DatetimeIndex) and str(df.index.tz) == "UTC" and df.index.is_monotonic_increasing
    assert df.index.is_unique and list(df.columns) == bc.FRAME_COLS
    np.log(df["close"])                                          # the call that crashed the hosted audit


# ------------------------------------------------------------------ the production failure
def test_fresh_cache_returns_numeric_frames(live_api, tmp_path):
    c = bc.BinanceClient(api=live_api, cache_dir=tmp_path / "klines")
    df = c.klines("DEMOUSDT", "4h", 800)
    assert len(df) == 800 and contiguous(df, H4)
    assert_typed(df)
    assert (tmp_path / "klines" / "DEMOUSDT_4h.parquet").exists()
    assert_typed(bc.normalise_frame(pd.read_parquet(tmp_path / "klines" / "DEMOUSDT_4h.parquet")))


def test_no_cache_path_is_numeric_too(live_api):
    df = bc.BinanceClient(api=live_api).klines("DEMOUSDT", "1h", 1000)
    assert len(df) == 1000 and contiguous(df, bc.TF_MS["1h"])
    assert_typed(df)


def test_pandas3_concat_with_an_empty_frame_is_the_trap():
    """Documents why normalise_frame exists: an empty untyped frame poisons every dtype in pandas 3."""
    typed = sy.gbm_ohlcv(10, "4h")[bc.FRAME_COLS]
    poisoned = pd.concat([pd.DataFrame(columns=bc.FRAME_COLS), typed])
    assert poisoned["close"].dtype == object
    with pytest.raises(TypeError):
        np.log(poisoned["close"])
    assert_typed(bc.normalise_frame(poisoned))
    assert_typed(bc.merge_frames(bc.empty_frame(), typed))
    assert_typed(bc.merge_frames(typed))
    assert bc.merge_frames().empty and list(bc.merge_frames().columns) == bc.FRAME_COLS


def test_legacy_object_typed_cache_file_is_repaired(live_api, tmp_path):
    """A cache written by the buggy version (object columns, object index) is read back typed."""
    cache = tmp_path / "klines"
    cache.mkdir()
    typed = live_api.frame("DEMOUSDT", "4h").tail(300)
    bad = pd.concat([pd.DataFrame(columns=bc.FRAME_COLS), typed])
    bad.to_parquet(cache / "DEMOUSDT_4h.parquet")
    df = bc.BinanceClient(api=live_api, cache_dir=cache).klines("DEMOUSDT", "4h", 200)
    assert len(df) == 200 and contiguous(df, H4)
    assert_typed(df)


# --------------------------------------------------------------------- cache mechanics
def test_second_call_hits_the_cache_and_only_asks_for_new_candles(live_api, tmp_path):
    c = bc.BinanceClient(api=live_api, cache_dir=tmp_path / "klines")
    first = c.klines("DEMOUSDT", "4h", 500)
    n_calls = len(live_api.calls)
    second = c.klines("DEMOUSDT", "4h", 500)
    new_calls = live_api.calls[n_calls:]
    assert len(new_calls) == 1 and new_calls[0][2] == int(first["open_time"].iloc[-1]) + 1   # one forward refresh
    pd.testing.assert_frame_equal(first, second)


def test_new_candles_are_appended_without_a_gap(tmp_path):
    n = 96 * 200
    api = FakeSpotApi({"DEMOUSDT": sy.gbm_ohlcv(n + 96 * 3, "15m", seed=11, start=recent_start(n + 96 * 3))})
    full = api.raw["DEMOUSDT"]
    api.raw["DEMOUSDT"], later = full.iloc[:n], full.iloc[n:]
    c = bc.BinanceClient(api=api, cache_dir=tmp_path / "klines")
    before = c.klines("DEMOUSDT", "4h", 600)
    api.extend("DEMOUSDT", later)                                # three more days arrive
    after = c.klines("DEMOUSDT", "4h", 600)
    assert len(after) == 600 and contiguous(after, H4)
    assert after["open_time"].iloc[-1] == before["open_time"].iloc[-1] + 18 * H4
    assert_typed(after)
    file = pd.read_parquet(tmp_path / "klines" / "DEMOUSDT_4h.parquet")
    assert contiguous(file, H4) and len(file) == 618


def test_stale_cache_is_refilled_contiguously(tmp_path):
    """The old code capped the refill at 5000 candles, which could leave a hole between the cached history
    and the new candles; the file must stay contiguous however long the bot was switched off."""
    n = 96 * 300
    api = FakeSpotApi({"DEMOUSDT": sy.gbm_ohlcv(n, "15m", seed=12, start=recent_start(n))})
    full = api.raw["DEMOUSDT"]
    api.raw["DEMOUSDT"] = full.iloc[: 96 * 150]
    c = bc.BinanceClient(api=api, cache_dir=tmp_path / "klines")
    c.klines("DEMOUSDT", "15m", 1000)
    api.extend("DEMOUSDT", full.iloc[96 * 150:])                 # 150 days = 14,400 15m candles later
    df = c.klines("DEMOUSDT", "15m", 3000)
    assert len(df) == 3000 and contiguous(df, M15)
    assert df["open_time"].iloc[-1] == int(full["open_time"].iloc[-1])
    file = pd.read_parquet(tmp_path / "klines" / "DEMOUSDT_15m.parquet")
    assert contiguous(file, M15)


def test_backfill_extends_older_history(live_api, tmp_path):
    c = bc.BinanceClient(api=live_api, cache_dir=tmp_path / "klines")
    short = c.klines("DEMOUSDT", "4h", 200)
    longer = c.klines("DEMOUSDT", "4h", 900)
    assert len(longer) == 900 and contiguous(longer, H4)
    assert longer["open_time"].iloc[-1] == short["open_time"].iloc[-1]
    assert_typed(longer)


def test_forming_candle_is_never_returned_nor_cached(live_api, tmp_path, monkeypatch):
    frame = live_api.frame("DEMOUSDT", "4h")
    last_open = int(frame["open_time"].iloc[-1])
    monkeypatch.setattr(bc.time, "time", lambda: (last_open + H4 / 2) / 1000)   # halfway through the last candle
    c = bc.BinanceClient(api=live_api, cache_dir=tmp_path / "klines")
    df = c.klines("DEMOUSDT", "4h", 100)
    assert int(df["open_time"].iloc[-1]) == last_open - H4
    file = pd.read_parquet(tmp_path / "klines" / "DEMOUSDT_4h.parquet")
    assert int(file["open_time"].max()) == last_open - H4


def test_as_of_view_hides_the_future_and_rebuilds_ticker_and_book(live_api, tmp_path):
    frame = live_api.frame("DEMOUSDT", "4h")
    as_of = int(frame["close_time"].iloc[-300]) + 1              # the moment candle -300 closed
    c = bc.BinanceClient(api=live_api, cache_dir=tmp_path / "klines", as_of=as_of)
    df = c.klines("DEMOUSDT", "4h", 500)
    assert len(df) == 500 and int(df["close_time"].iloc[-1]) < as_of and contiguous(df, H4)
    t = c.ticker_24h("DEMOUSDT")
    assert t["synthetic"] and float(t["lastPrice"]) == pytest.approx(float(df["close"].iloc[-1]), rel=0.02)
    assert c.order_book("DEMOUSDT")["synthetic"]
    with pytest.raises(bc.BinanceError):
        c.ticker_24h_all()
    live = bc.BinanceClient(api=live_api, cache_dir=tmp_path / "klines")   # same cache, live view
    assert int(live.klines("DEMOUSDT", "4h", 500)["close_time"].iloc[-1]) > as_of


def test_symbol_info_and_invalid_symbol(live_api):
    c = bc.BinanceClient(api=live_api)
    assert c.symbol_info("DEMOUSDT")["status"] == "TRADING"
    assert c.symbol_info("NOPEUSDT") is None
    assert c.all_symbols() == {"BTCUSDT", "DEMOUSDT", "ETHUSDT"}


# ---------------------------------------------------------------- scanner ticker list
def test_ticker_all_falls_back_to_the_rest_endpoint(live_api, monkeypatch):
    class Odd(FakeSpotApi):
        def ticker24hr(self, symbol=None):
            return Resp({"root": {"symbol": "ONLYONE"}}) if symbol is None else super().ticker24hr(symbol)

    odd = Odd(live_api.raw)
    c = bc.BinanceClient(api=odd)
    urls = []

    class R:
        status_code = 200

        def json(self):
            return [{"symbol": s, "quoteVolume": "1e9", "lastPrice": "1"} for s in ("BTCUSDT", "ETHUSDT", "DEMOUSDT")]

    c.http_get = lambda url, **kw: urls.append(url) or R()
    rows = c.ticker_24h_all()
    assert [r["symbol"] for r in rows] == ["BTCUSDT", "ETHUSDT", "DEMOUSDT"]
    assert urls == [bc.MARKET_DATA_URL + "/api/v3/ticker/24hr"]
    assert [r["symbol"] for r in bc.BinanceClient(api=live_api).ticker_24h_all()] == ["BTCUSDT", "DEMOUSDT", "ETHUSDT"]


def test_records_unwraps_sdk_shapes():
    assert bc._records([{"symbol": "A"}, {"symbol": "B"}]) == [{"symbol": "A"}, {"symbol": "B"}]
    assert bc._records({"root": [{"symbol": "A"}]}) == [{"symbol": "A"}]
    assert bc._records({"symbol": "A"}) == [{"symbol": "A"}]
    assert bc._records({"weird": 1}) == [] and bc._records(None) == []


# ------------------------------------------------------------- the whole audit, live path
@pytest.fixture
def isolated_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(au, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(au, "LOG", tmp_path / "logs" / "audits.csv")
    monkeypatch.setattr(au, "SIGNAL_LOG", tmp_path / "logs" / "signals.csv")
    monkeypatch.setattr(au, "TRACK_RECORD", tmp_path / "logs" / "track_record.json", raising=False)
    monkeypatch.setattr(au, "KLINE_CACHE", tmp_path / "cache" / "klines")
    return tmp_path


def test_full_audit_through_the_live_client(live_api, isolated_outputs):
    c = bc.BinanceClient(api=live_api, cache_dir=isolated_outputs / "cache" / "klines")
    a = au.audit(c, None, "DEMO", "USDT", "4h", use_ml=False, use_market=False,
                 strategies=["trend_ema_adx_v1", "range_support_v1"])
    assert a["symbol"] == "DEMOUSDT" and not a["offline"] and not a["point_in_time"]
    assert a["verdict"] in {"FAVORABLE", "WATCHLIST", "NEUTRAL", "AVOID"}
    assert not any(f.startswith("STALE DATA") for f in a["flags"])
    assert a["regime"]["markov"]["available"] or "reason" in a["regime"]["markov"]


def test_main_cli_on_the_live_path(live_api, isolated_outputs, monkeypatch):
    """`audit.py DOGE --tf 4h --dashboard --no-tearsheet` as the hosted job runner launches it, minus the network."""
    made = []

    def factory(cache_dir=None, as_of=None, **_):
        made.append(cache_dir)
        return bc.BinanceClient(api=live_api, cache_dir=cache_dir, as_of=as_of)

    monkeypatch.setattr(au, "BinanceClient", factory)
    monkeypatch.setattr(au, "FuturesClient", lambda: (_ for _ in ()).throw(RuntimeError("no futures SDK here")))
    rc = au.main(["DEMO", "--tf", "4h", "--dashboard", "--no-tearsheet", "--no-ml", "--no-market",
                  "--strategies", "trend_ema_adx_v1,range_support_v1"])
    assert rc == 0 and made == [au.KLINE_CACHE]
    assert list(au.REPORTS.glob("DEMOUSDT_4h_*.md")) and (au.REPORTS / "dashboard.html").exists()
    assert (au.KLINE_CACHE / "DEMOUSDT_4h.parquet").exists()
    rc2 = au.main(["DEMO", "--tf", "4h", "--no-tearsheet", "--no-ml", "--no-market",     # warm cache
                   "--strategies", "trend_ema_adx_v1,range_support_v1"])
    assert rc2 == 0 and len(list(au.REPORTS.glob("DEMOUSDT_4h_*.md"))) == 2


def test_scan_on_the_live_path(live_api, isolated_outputs, monkeypatch):
    monkeypatch.setattr(au, "BinanceClient",
                        lambda cache_dir=None, as_of=None, **_: bc.BinanceClient(api=live_api, cache_dir=cache_dir, as_of=as_of))
    monkeypatch.setattr(au, "FuturesClient", lambda: (_ for _ in ()).throw(RuntimeError("no futures SDK here")))
    rc = au.main(["--scan", "3", "--jobs", "1", "--tf", "4h", "--no-ml", "--no-market", "--no-tearsheet",
                  "--strategies", "trend_ema_adx_v1"])
    assert rc == 0
    assert list(au.REPORTS.glob("scan_4h_*.md"))
