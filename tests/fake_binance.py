"""A stand-in for the Binance SDK's REST API: same method names, keyword arguments and response SHAPES
(kline rows are lists of strings and ints, exactly as Binance returns them), served from synthetic data.
It lets the tests run the real BinanceClient code (pagination, cache, forming-candle rule) without a network."""
from __future__ import annotations

import pandas as pd

import synthetic as sy


class Resp:
    def __init__(self, data):
        self._d = data

    def data(self):
        return self._d


class FakeSpotApi:
    def __init__(self, frames_15m: dict[str, pd.DataFrame]):
        self.raw = dict(frames_15m)
        self.calls: list[tuple] = []
        self._tf: dict = {}

    def frame(self, symbol: str, interval: str) -> pd.DataFrame:
        key = (symbol, interval)
        if key not in self._tf:
            base = self.raw[symbol]
            self._tf[key] = base if interval == "15m" else sy.resample(base, interval)
        return self._tf[key]

    def extend(self, symbol: str, frame_15m: pd.DataFrame) -> None:
        """New candles arrive."""
        self.raw[symbol] = pd.concat([self.raw[symbol], frame_15m])
        self._tf = {k: v for k, v in self._tf.items() if k[0] != symbol}

    # ------------------------------------------------------------- endpoints
    def exchange_info(self, symbol=None):
        if symbol is not None and symbol not in self.raw:
            raise RuntimeError('{"code":-1121,"msg":"Invalid symbol."}')
        names = [symbol] if symbol else sorted(self.raw)
        return Resp({"symbols": [{"symbol": n, "status": "TRADING", "baseAsset": n[:-4], "quoteAsset": "USDT",
                                  "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.0001"}]} for n in names]})

    def ticker24hr(self, symbol=None):
        def one(n):
            last = self.raw[n].tail(96)
            return {"symbol": n, "lastPrice": str(last["close"].iloc[-1]), "quoteVolume": str(last["quote_volume"].sum()),
                    "volume": str(last["volume"].sum()), "count": int(last["trades"].sum())}
        return Resp(one(symbol) if symbol else [one(n) for n in sorted(self.raw)])

    def depth(self, symbol, limit=500):
        px = float(self.raw[symbol]["close"].iloc[-1])
        bids = [[str(px * (1 - 0.0002 * (i + 1))), str(50_000 / px)] for i in range(25)]
        asks = [[str(px * (1 + 0.0002 * (i + 1))), str(50_000 / px)] for i in range(25)]
        return Resp({"bids": bids, "asks": asks})

    def klines(self, symbol, interval, start_time=None, end_time=None, limit=500, **_):
        self.calls.append((symbol, interval, start_time, end_time, limit))
        df = self.frame(symbol, interval)
        ot = df["open_time"]
        if start_time is not None:                                   # forward from start (optionally bounded)
            sel = df[(ot >= start_time) & ((ot <= end_time) if end_time is not None else True)].head(limit)
        elif end_time is not None:                                   # the newest `limit` candles up to end_time
            sel = df[ot <= end_time].tail(limit)
        else:
            sel = df.tail(limit)
        rows = [[int(r.open_time), str(r.open), str(r.high), str(r.low), str(r.close), str(r.volume), int(r.close_time),
                 str(r.quote_volume), int(r.trades), str(r.taker_buy_base), str(r.taker_buy_quote), "0"]
                for r in sel.itertuples()]
        return Resp(rows)
