"""Crypto-wide + macro market regime layer (filters, never entry triggers).

SOURCE OF TRUTH (one methodology, documented, never mixed with TradingView's CRYPTOCAP series):
  * Coin market caps  = Binance close price x CURRENT circulating supply (CoinGecko snapshot) for the
                        largest non-stable coins that trade on Binance vs USDT (default 60 coins).
                        Supply changes slowly for most large caps, but inflationary tokens are
                        overstated in the past -> use these series for TREND/DIRECTION, not exact levels.
  * Stablecoins       = DefiLlama circulating supply history (all stablecoins, USDT, USDC).
  * TOTAL             = coins above + all stablecoins        TOTAL2 = TOTAL - BTC      TOTAL3 = TOTAL2 - ETH
    TOTAL2ES/TOTAL3ES = TOTAL2/TOTAL3 excluding stablecoins  X.D   = X market cap / TOTAL x 100
    OTHERS.D          = everything outside the 10 largest assets (incl. stablecoins) / TOTAL x 100
  * Macro             = FRED (St. Louis Fed) public CSV series; gold via Binance PAXGUSDT.
                        DXY itself is ICE-licensed and not free: FRED's Broad US Dollar Index
                        (DTWEXBGS) is used as the dollar proxy.
Everything is cached under audit/cache/ so repeated audits don't re-download.
"""
from __future__ import annotations

import io
import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from settings import CFG

CACHE = Path(__file__).resolve().parent / "cache"
EVENTS = Path(__file__).resolve().parent / "events.csv"
UNIVERSE_N = CFG["market"]["universe_size"]
STABLES = {"usdt", "usdc", "dai", "fdusd", "usde", "tusd", "pyusd", "usdd", "busd", "usds", "frax", "lusd",
           "gusd", "usdp", "eurc", "rlusd", "usd1", "usdtb", "susds", "susde", "bsc-usd", "usdx", "usd0",
           "gho", "crvusd", "usdb", "buidl", "usyc", "usdf", "usdg", "eurt", "xaut", "paxg"}
DERIVATIVE_HINTS = ("wrapped", "staked", "bridged", "restaked", "liquid staking", "wbtc", "weth", "steth",
                    "wsteth", "cbbtc", "weeth", "jitosol", "msol", "bnsol", "lbtc", "solvbtc", "rseth", "ezeth",
                    "tbtc", "cbeth", "reth", "meth", "sweth", "jupsol", "clbtc", "wbeth", "binance-staked")
FRED = {"USD_BROAD (DXY proxy)": "DTWEXBGS", "EURUSD": "DEXUSEU", "USDJPY": "DEXJPUS", "USDCNY": "DEXCHUS",
        "SPX": "SP500", "NDX": "NASDAQ100", "VIX": "VIXCLS", "US10Y": "DGS10", "US02Y": "DGS2",
        "US10Y-US02Y": "T10Y2Y", "REAL_YIELD_10Y": "DFII10", "WTI": "DCOILWTICO",
        "FED_BALANCE_SHEET": "WALCL", "M2": "M2SL"}
YIELDS = {"US10Y", "US02Y", "US10Y-US02Y", "REAL_YIELD_10Y"}
S = requests.Session()


# ------------------------------------------------------------------- cache
def _cached(name: str, max_age_h: float, fn):
    CACHE.mkdir(exist_ok=True)
    p = CACHE / name
    if p.exists() and time.time() - p.stat().st_mtime < max_age_h * 3600:
        return pd.read_parquet(p) if p.suffix == ".parquet" else json.loads(p.read_text())
    data = fn()
    if data is None:
        if p.exists():                       # stale cache beats nothing
            return pd.read_parquet(p) if p.suffix == ".parquet" else json.loads(p.read_text())
        return None
    if p.suffix == ".parquet":
        data.to_parquet(p)
    else:
        p.write_text(json.dumps(data))
    return data


def _get(url, **kw):
    try:
        r = S.get(url, timeout=25, **kw)
        return r if r.status_code == 200 else None
    except requests.RequestException:
        return None


# ------------------------------------------------------------ data sources
def coingecko_markets() -> list | None:
    def fetch():
        h = {"x-cg-demo-api-key": os.environ["COINGECKO_API_KEY"]} if os.environ.get("COINGECKO_API_KEY") else {}
        r = _get("https://api.coingecko.com/api/v3/coins/markets",
                 params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": 1}, headers=h)
        return r.json() if r is not None else None
    return _cached("coingecko_markets.json", 12, fetch)


def coingecko_global() -> dict | None:
    def fetch():
        h = {"x-cg-demo-api-key": os.environ["COINGECKO_API_KEY"]} if os.environ.get("COINGECKO_API_KEY") else {}
        r = _get("https://api.coingecko.com/api/v3/global", headers=h)
        return r.json().get("data") if r is not None else None
    return _cached("coingecko_global.json", 1, fetch)


def stablecoins() -> pd.DataFrame | None:
    def fetch():
        out = {}
        for key, q in (("ALL", ""), ("USDT", "?stablecoin=1"), ("USDC", "?stablecoin=2")):
            r = _get("https://stablecoins.llama.fi/stablecoincharts/all" + q)
            if r is None:
                return None
            rows = r.json()
            out[key] = pd.Series({pd.Timestamp(int(x["date"]), unit="s", tz="UTC"):
                                  float((x.get("totalCirculatingUSD") or x.get("totalCirculating") or {}).get("peggedUSD", np.nan))
                                  for x in rows})
        return pd.DataFrame(out).sort_index()
    return _cached("stablecoins.parquet", 12, fetch)


def fred(series_id: str) -> pd.Series | None:
    def fetch():
        r = _get("https://fred.stlouisfed.org/graph/fredgraph.csv", params={"id": series_id})
        if r is None:
            return None
        d = pd.read_csv(io.StringIO(r.text))
        d.columns = ["date", "value"]
        d["value"] = pd.to_numeric(d["value"], errors="coerce")
        d["date"] = pd.to_datetime(d["date"], utc=True)
        return d.dropna().set_index("date")
    d = _cached(f"fred_{series_id}.parquet", 12, fetch)
    return None if d is None else d["value"]


def load_events() -> pd.DataFrame:
    if not EVENTS.exists():
        return pd.DataFrame(columns=["date_utc", "event", "type", "coin"])
    lines = [l for l in EVENTS.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    if len(lines) <= 1:
        return pd.DataFrame(columns=["date_utc", "event", "type", "coin"])
    d = pd.read_csv(io.StringIO("\n".join(lines)))
    d["date_utc"] = pd.to_datetime(d["date_utc"], utc=True, errors="coerce")
    return d.dropna(subset=["date_utc"])


# ---------------------------------------------------------------- universe
def _is_derivative(c: dict) -> bool:
    txt = (c.get("id", "") + " " + c.get("name", "") + " " + c.get("symbol", "")).lower()
    return any(h in txt for h in DERIVATIVE_HINTS)


def _is_stable(c: dict) -> bool:
    sym = c.get("symbol", "").lower()
    px = c.get("current_price") or 0
    return sym in STABLES or ("usd" in sym and 0.97 <= px <= 1.03)


def build_indices(client, tf: str, markets: list, stables: pd.DataFrame | None, binance_symbols: set) -> dict:
    """Market-cap indices and dominance series on one timeframe ('1d' or '4h')."""
    coins = [c for c in markets if not _is_stable(c) and not _is_derivative(c)
             and f"{c['symbol'].upper()}USDT" in binance_symbols and c.get("circulating_supply")]
    coins = coins[:UNIVERSE_N]
    bars = 400 if tf == "1d" else 1000

    def fetch():
        closes, vols = {}, {}
        for c in coins:
            sym = f"{c['symbol'].upper()}USDT"
            try:
                k = client.klines(sym, tf, bars)
            except Exception:
                continue
            if len(k):
                closes[c["symbol"].upper()] = k["close"]
                vols[c["symbol"].upper()] = k["quote_volume"]
        if not closes:
            return None
        return pd.concat({"close": pd.DataFrame(closes), "qv": pd.DataFrame(vols)}, axis=1)
    raw = _cached(f"universe_{tf}.parquet", 4 if tf == "4h" else 12, fetch)
    if raw is None:
        return {"available": False, "reason": "could not download the market universe from Binance"}
    closes, qv = raw["close"], raw["qv"]
    supply = {c["symbol"].upper(): float(c["circulating_supply"]) for c in coins}
    mc = pd.DataFrame({s: closes[s] * supply[s] for s in closes.columns if s in supply}).ffill()
    # coins listed later than the window start contribute from their listing onward
    if stables is not None and len(stables):
        st = stables.reindex(mc.index, method="ffill")
    else:
        snap = {c["symbol"].upper(): c.get("market_cap", 0) for c in markets if _is_stable(c)}
        st = pd.DataFrame({"ALL": sum(snap.values()), "USDT": snap.get("USDT", 0), "USDC": snap.get("USDC", 0)},
                          index=mc.index)
    non_stable = mc.sum(axis=1, min_count=1)
    total = non_stable + st["ALL"]
    btc = mc.get("BTC", pd.Series(0.0, index=mc.index))
    eth = mc.get("ETH", pd.Series(0.0, index=mc.index))
    last_caps = {**{s: mc[s].iloc[-1] for s in mc.columns}, "USDT": st["USDT"].iloc[-1], "USDC": st["USDC"].iloc[-1]}
    top10 = sorted(last_caps, key=lambda k: -np.nan_to_num(last_caps[k]))[:10]
    top10_sum = sum(mc[s] for s in top10 if s in mc.columns) + sum(st[s] for s in ("USDT", "USDC") if s in top10)
    alt_vol = qv.drop(columns=[c for c in ("BTC", "ETH") if c in qv.columns]).sum(axis=1)
    idx = pd.DataFrame({
        "TOTAL": total, "TOTAL2": total - btc, "TOTAL3": total - btc - eth,
        "TOTAL2ES": total - btc - st["ALL"], "TOTAL3ES": total - btc - eth - st["ALL"],
        "BTC.D": btc / total * 100, "ETH.D": eth / total * 100, "USDT.D": st["USDT"] / total * 100,
        "USDC.D": st["USDC"] / total * 100, "STABLE.D": st["ALL"] / total * 100,
        "OTHERS.D": (total - top10_sum) / total * 100,
        "BTC.D+USDT.D": (btc + st["USDT"]) / total * 100, "TOTAL3_VOLUME": alt_vol}).dropna()
    return {"available": True, "index": idx, "coins": len(mc.columns),
            "stables_source": "DefiLlama" if stables is not None and len(stables) else "CoinGecko snapshot (flat)"}


# ------------------------------------------------------------------ metrics
def _ema(s, n):
    return s.ewm(span=n, adjust=False, min_periods=min(n, max(5, len(s) // 2))).mean()


def change(s: pd.Series, periods: int, pp: bool = False) -> float | None:
    if s is None or len(s) <= periods:
        return None
    a, b = float(s.iloc[-1 - periods]), float(s.iloc[-1])
    if pp:
        return round(b - a, 3)
    return round((b / a - 1) * 100, 2) if a else None


def trend(s: pd.Series, n_long: int = 200) -> dict:
    e_long, e20 = _ema(s, n_long), _ema(s, 20)
    return {"above_long_ema": bool(s.iloc[-1] > e_long.iloc[-1]) if np.isfinite(e_long.iloc[-1]) else None,
            "ema20_slope": "up" if e20.iloc[-1] > e20.iloc[-6] else "down"}


def broke_support(s: pd.Series, look=30) -> bool:
    return bool(len(s) > look + 2 and s.iloc[-1] < s.iloc[-look - 1:-1].min())


def broke_higher(s: pd.Series, look=30) -> bool:
    return bool(len(s) > look + 2 and s.iloc[-1] > s.iloc[-look - 1:-1].max())


# ------------------------------------------------------------------ builder
def snapshot(client, fut, symbol: str, base: str, coin_frames: dict, btc_frames: dict, liq_metrics: dict,
             binance_symbols: set) -> dict:
    """Everything the regime classifier and per-coin table need, with graceful degradation."""
    out = {"available": False, "notes": [], "methodology": __doc__.split("SOURCE OF TRUTH")[1].split("Everything is cached")[0].strip()}
    markets = coingecko_markets()
    if not markets:
        out["reason"] = "CoinGecko market list unavailable (network?)"
        return out
    st = stablecoins()
    if st is None:
        out["notes"].append("DefiLlama unavailable — stablecoin supply held flat at today's CoinGecko value.")
    i1 = build_indices(client, "1d", markets, st, binance_symbols)
    i4 = build_indices(client, "4h", markets, st, binance_symbols)
    if not i1.get("available") or not i4.get("available"):
        out["reason"] = i1.get("reason") or i4.get("reason")
        return out
    d1, h4 = i1["index"], i4["index"]
    glob = coingecko_global() or {}
    cg_total = (glob.get("total_market_cap") or {}).get("usd")
    coverage = float(d1["TOTAL"].iloc[-1] / cg_total) if cg_total else None

    dash = []
    for name in ["TOTAL", "TOTAL2", "TOTAL3", "TOTAL2ES", "TOTAL3ES", "BTC.D", "ETH.D", "USDT.D", "USDC.D",
                 "STABLE.D", "OTHERS.D", "BTC.D+USDT.D"]:
        pp = name.endswith(".D")
        dash.append({"symbol": name, "value": float(d1[name].iloc[-1]),
                     "4h": change(h4[name], 1, pp), "24h": change(h4[name], 6, pp), "7d": change(d1[name], 7, pp),
                     "30d": change(d1[name], 30, pp), "90d": change(d1[name], 90, pp),
                     "trend_4h": trend(h4[name]), "trend_1d": trend(d1[name], 50), "unit": "pp" if pp else "%"})
    # BTC / ETH / ETHBTC / coin-BTC from Binance
    pairs = {}
    for sym, tfkey in (("BTCUSDT", None), ("ETHUSDT", None), ("ETHBTC", None), ("PAXGUSDT", None)):
        try:
            pairs[sym] = {"1d": client.klines(sym, "1d", 400)["close"], "4h": client.klines(sym, "4h", 1000)["close"]}
        except Exception:
            pass
    coin_btc_sym = f"{base}BTC"
    if base not in ("BTC",):
        try:
            if coin_btc_sym in binance_symbols:
                pairs[coin_btc_sym] = {"1d": client.klines(coin_btc_sym, "1d", 400)["close"],
                                       "4h": client.klines(coin_btc_sym, "4h", 1000)["close"]}
            else:
                pairs[coin_btc_sym + " (synthetic)"] = {
                    "1d": (coin_frames["1d"]["close"] / btc_frames["1d"]["close"]).dropna(),
                    "4h": (coin_frames["4h"]["close"] / btc_frames["4h"]["close"]).dropna()}
        except Exception:
            pass
    for sym, s in pairs.items():
        dash.append({"symbol": sym, "value": float(s["1d"].iloc[-1]), "4h": change(s["4h"], 1),
                     "24h": change(s["4h"], 6), "7d": change(s["1d"], 7), "30d": change(s["1d"], 30),
                     "90d": change(s["1d"], 90), "trend_4h": trend(s["4h"]), "trend_1d": trend(s["1d"], 50), "unit": "%"})
    macro = {}
    for name, sid in FRED.items():
        s = fred(sid)
        if s is None or len(s) < 30:
            continue
        pp = name in YIELDS
        macro[name] = {"value": float(s.iloc[-1]), "as_of": s.index[-1].strftime("%Y-%m-%d"),
                       "5obs": change(s, 5, pp), "20obs": change(s, 20, pp), "60obs": change(s, 60, pp),
                       "trend": trend(s, 50), "broke_higher_60": broke_higher(s, 60), "series": sid,
                       "unit": "pp" if pp else "%"}
    if not macro:
        out["notes"].append("FRED unavailable — macro layer skipped.")
    events = load_events()
    now = datetime.now(timezone.utc)
    soon = events[(events["date_utc"] >= now) & (events["date_utc"] <= now + timedelta(hours=48))] if len(events) else events
    coin_ev = events[(events.get("coin", pd.Series(dtype=str)).astype(str).str.upper() == base)
                     & (events["date_utc"] >= now) & (events["date_utc"] <= now + timedelta(days=7))] if len(events) else events

    out.update({"available": True, "coverage_vs_coingecko": coverage, "coins_in_index": i1["coins"],
                "stables_source": i1["stables_source"], "dashboard": dash, "macro": macro,
                "series_1d": d1, "series_4h": h4, "pairs": pairs,
                "events_48h": soon.assign(date_utc=soon["date_utc"].astype(str)).to_dict("records") if len(soon) else [],
                "coin_events_7d": coin_ev.assign(date_utc=coin_ev["date_utc"].astype(str)).to_dict("records") if len(coin_ev) else []})
    out["coin"] = coin_fields(markets, base, symbol, coin_frames, btc_frames, d1, h4, pairs, liq_metrics, fut)
    return out


def coin_fields(markets, base, symbol, cf, bf, d1, h4, pairs, liq, fut) -> dict:
    cg = next((c for c in markets if c["symbol"].upper() == base and not _is_derivative(c)), None)
    per = {"15m": ("15m", 1), "1h": ("1h", 1), "4h": ("4h", 1), "24h": ("1h", 24), "7d": ("1d", 7),
           "30d": ("1d", 30), "90d": ("1d", 90)}
    rows = {}
    for lab, (tf, k) in per.items():
        c_ = change(cf[tf]["close"], k) if tf in cf else None
        b_ = change(bf[tf]["close"], k) if tf in bf else None
        t3 = change(h4["TOTAL3"], {"4h": 1, "24h": 6}[lab]) if lab in ("4h", "24h") else \
            change(d1["TOTAL3"], k) if tf == "1d" else None
        rows[lab] = {"coin": c_, "btc": b_, "total3": t3,
                     "vs_btc": None if c_ is None or b_ is None else round(c_ - b_, 2),
                     "vs_total3": None if c_ is None or t3 is None else round(c_ - t3, 2)}
    r = cf["1d"]["close"].pct_change()
    corr_btc = float(r.tail(90).corr(bf["1d"]["close"].pct_change().tail(90))) if "1d" in bf else None
    eth = pairs.get("ETHUSDT", {}).get("1d")
    corr_eth = float(r.tail(90).corr(eth.pct_change().reindex(r.index).tail(90))) if eth is not None else None
    fields = {"performance": rows, "corr_btc_90d": None if corr_btc is None else round(corr_btc, 2),
              "corr_eth_90d": None if corr_eth is None else round(corr_eth, 2),
              "spread_bps": liq.get("spread_bps"), "depth_1pct_usd": liq.get("depth_1pct_usd"),
              "volume_24h": liq.get("quote_volume_24h"),
              "relative_volume": round(float(cf["1d"]["quote_volume"].iloc[-1] / cf["1d"]["quote_volume"].tail(30).mean()), 2)}
    if cg:
        fields.update({"market_cap": cg.get("market_cap"), "market_cap_rank": cg.get("market_cap_rank"),
                       "fdv": cg.get("fully_diluted_valuation"),
                       "volume_to_mcap": round(liq.get("quote_volume_24h", 0) / cg["market_cap"], 4) if cg.get("market_cap") else None})
    if fut:
        fields.update(derivatives(fut, symbol))
    fields["not_available"] = ["liquidation levels (no free historical source)",
                               "token unlocks (add them to audit/events.csv)", "sector performance"]
    return fields


def derivatives(fut, symbol: str) -> dict:
    out = {}
    try:
        from binance_sdk_derivatives_trading_usds_futures.rest_api.models import enums as E
        api = fut.api

        def call(fn, **kw):
            return fut._call(fn, **kw)
        oi = call(api.open_interest_statistics, symbol=symbol, period=E.OpenInterestStatisticsPeriodEnum["PERIOD_1h"], limit=200)
        if oi:
            v = pd.Series([float(x["sumOpenInterestValue"]) for x in oi])
            out.update({"open_interest_usd": float(v.iloc[-1]),
                        "oi_change_24h_pct": round((v.iloc[-1] / v.iloc[-25] - 1) * 100, 2) if len(v) > 25 else None,
                        "oi_change_7d_pct": round((v.iloc[-1] / v.iloc[-169] - 1) * 100, 2) if len(v) > 169 else None})
        ls = call(api.long_short_ratio, symbol=symbol, period=E.LongShortRatioPeriodEnum["PERIOD_1h"], limit=24)
        if ls:
            out["long_short_ratio_accounts"] = float(ls[-1]["longShortRatio"])
        tt = call(api.top_trader_long_short_ratio_positions, symbol=symbol,
                  period=E.TopTraderLongShortRatioPositionsPeriodEnum["PERIOD_1h"], limit=24)
        if tt:
            out["top_trader_long_short_positions"] = float(tt[-1]["longShortRatio"])
        tk = call(api.taker_buy_sell_volume, symbol=symbol, period=E.TakerBuySellVolumePeriodEnum["PERIOD_1h"], limit=24)
        if tk:
            out["taker_buy_sell_ratio_24h"] = round(float(np.mean([float(x["buySellRatio"]) for x in tk])), 3)
    except Exception as e:
        out["derivatives_error"] = str(e)[:120]
    return out


# --------------------------------------------------------------- regime
def classify(m: dict, btc4: pd.DataFrame, btc1: pd.DataFrame, coin_is_btc: bool, liq_ok: bool,
             btc_structure: str) -> dict:
    """The eight practical regimes + the altcoin-long eligibility score (a starting design, not validated)."""
    d1, h4, mac = m["series_1d"], m["series_4h"], m["macro"]
    bc4, bc1 = btc4["close"], btc1["close"]
    btc_above = bool(bc4.iloc[-1] > btc4["ema200"].iloc[-1])
    btc_bull = btc_above and btc_structure == "bullish"
    t_up = lambda s: bool(s.iloc[-1] > _ema(s, 200).iloc[-1])
    slope_dn = lambda s: bool(_ema(s, 20).iloc[-1] < _ema(s, 20).iloc[-6])
    btc7 = change(bc1, 7) or 0
    t3_7 = change(d1["TOTAL3"], 7) or 0
    tot7 = change(d1["TOTAL"], 7) or 0
    btcd_fall = slope_dn(h4["BTC.D"]) and slope_dn(d1["BTC.D"])
    btcd_rise = (not slope_dn(h4["BTC.D"])) and (not slope_dn(d1["BTC.D"]))
    usdtd_fall = slope_dn(h4["USDT.D"]) and slope_dn(d1["USDT.D"])
    usdtd_rise = (not slope_dn(h4["USDT.D"])) and (not slope_dn(d1["USDT.D"]))
    t3_break = bool(h4["TOTAL3"].iloc[-6:].max() > h4["TOTAL3"].iloc[-56:-6].max()) and \
        bool(h4["TOTAL3_VOLUME"].iloc[-6:].mean() > 1.2 * h4["TOTAL3_VOLUME"].iloc[-60:-6].mean())
    ethbtc = m["pairs"].get("ETHBTC", {}).get("1d")
    ethbtc_bull = bool(ethbtc is not None and ethbtc.iloc[-1] > _ema(ethbtc, 50).iloc[-1]
                       and _ema(ethbtc, 50).iloc[-1] > _ema(ethbtc, 50).iloc[-6])
    coin_btc = next((v["1d"] for k, v in m["pairs"].items() if k not in ("BTCUSDT", "ETHUSDT", "ETHBTC", "PAXGUSDT")), None)
    coin_btc_bull = bool(coin_btc is not None and coin_btc.iloc[-1] > _ema(coin_btc, 50).iloc[-1]
                         and _ema(coin_btc, 50).iloc[-1] > _ema(coin_btc, 50).iloc[-6])
    usd = mac.get("USD_BROAD (DXY proxy)")
    dxy_ok = usd is not None and (not usd["trend"]["above_long_ema"] or usd["trend"]["ema20_slope"] == "down")
    dxy_break = usd is not None and usd["broke_higher_60"] and (usd["20obs"] or 0) > 2
    vix = mac.get("VIX")
    events = m.get("events_48h", [])
    btc_support = broke_support(bc1, 30)
    t3_support = broke_support(d1["TOTAL3"], 30)
    usdt_up = broke_higher(d1["USDT.D"], 30)

    items = [("BTC above 4H 200 EMA with bullish structure", 15, btc_bull),
             ("TOTAL2 above 4H 200 EMA", 10, t_up(h4["TOTAL2"])),
             ("TOTAL3 above 4H 200 EMA", 15, t_up(h4["TOTAL3"])),
             ("TOTAL3 breaks/reclaims resistance with volume", 10, t3_break),
             ("BTC.D falling on 4H and 1D", 10, btcd_fall),
             ("USDT.D falling on 4H and 1D", 10, usdtd_fall),
             ("ETH/BTC bullish", 10, ethbtc_bull),
             ("Dollar (DXY proxy) falling or below its trend", 5, dxy_ok),
             ("Coin/BTC pair bullish", 10, coin_btc_bull and not coin_is_btc),
             ("Coin has high liquidity and low spread", 5, liq_ok),
             ("BTC breaks key support (30-day low close)", -25, btc_support),
             ("TOTAL3 breaks key support", -20, t3_support),
             ("USDT.D breaks higher", -15, usdt_up),
             ("Dollar breaks higher strongly", -10, dxy_break),
             ("Major macro/news event within 48h (events.csv)", -10, bool(events))]
    alt_score = float(np.clip(sum(p for _, p, ok in items if ok), 0, 100))
    btc_items = [("BTC above 4H 200 EMA with bullish structure", 30, btc_bull),
                 ("TOTAL above 4H 200 EMA", 15, t_up(h4["TOTAL"])),
                 ("USDT.D falling on 4H and 1D", 20, usdtd_fall),
                 ("Dollar falling or below trend", 10, dxy_ok),
                 ("VIX below 20", 10, bool(vix and vix["value"] < 20)),
                 ("SPX/NDX above trend", 10, bool(mac.get("NDX", mac.get("SPX", {})).get("trend", {}).get("above_long_ema"))),
                 ("High liquidity", 5, liq_ok),
                 ("BTC breaks key support", -25, btc_support), ("USDT.D breaks higher", -15, usdt_up),
                 ("Dollar breaks higher strongly", -10, dxy_break), ("Macro/news event within 48h", -10, bool(events))]
    btc_score = float(np.clip(sum(p for _, p, ok in btc_items if ok), 0, 100))

    atr_pct = (btc4["atr"] / bc4)
    hv = bool(atr_pct.iloc[-1] >= atr_pct.tail(500).quantile(0.95)) or abs(change(bc4, 6) or 0) > 8 or \
        bool(vix and vix["value"] > 35) or any(e.get("type") == "macro" and
                                               pd.Timestamp(e["date_utc"]) <= pd.Timestamp.now(tz="UTC") + pd.Timedelta(hours=24)
                                               for e in events)
    recent_reclaim = bool((bc4.iloc[-30:] > btc4["ema200"].iloc[-30:]).any() and
                          (bc4.iloc[-60:-30] < btc4["ema200"].iloc[-60:-30]).mean() > 0.5)
    chop = bool(btc4["adx"].iloc[-1] < 18 and abs(btc7) < 3)
    if hv:
        reg, beh = "High-volatility event", "Pause automatic entries; every signal needs manual approval."
    elif btc7 < -10 and tot7 < -10:
        reg, beh = "BTC selloff", "Disable altcoin longs; watch liquidation and volatility filters."
    elif usdtd_rise and tot7 < 0:
        reg, beh = "Risk-off / stablecoin rotation", "Restrict longs: BTC only if fully validated; no alt longs."
    elif btcd_fall and usdtd_fall and t3_7 > btc7 + 3:
        reg, beh = "Altcoin rotation", "Selective ETH/alt longs, ranked by relative strength."
    elif btc_bull and t_up(h4["TOTAL3"]) and not btcd_rise and usdtd_fall:
        reg, beh = "Broad risk-on", "Enable BTC, ETH and high-quality alt long setups."
    elif btc_bull and btcd_rise and t3_7 < btc7:
        reg, beh = "BTC-led rally", "Prefer BTC; be selective with alts."
    elif recent_reclaim and not t_up(h4["TOTAL3"]):
        reg, beh = "BTC recovery, alts weak", "Trade BTC carefully; wait for alt confirmation."
    elif chop:
        reg, beh = "Range / chop", "Range, grid and mean-reversion modules only; no breakout/trend entries."
    else:
        reg, beh = "Mixed", "No dominant regime; rely on per-strategy validation and the eligibility score."
    return {"regime": reg, "behaviour": beh, "alt_score": alt_score, "btc_score": btc_score,
            "alt_items": [{"condition": n, "points": p, "met": bool(ok)} for n, p, ok in items],
            "btc_items": [{"condition": n, "points": p, "met": bool(ok)} for n, p, ok in btc_items],
            "facts": {"btc_7d": btc7, "total3_7d": t3_7, "total_7d": tot7, "btcd_falling": btcd_fall,
                      "usdtd_falling": usdtd_fall, "usdtd_rising": usdtd_rise, "btc_structure_4h": btc_structure}}


RANGE_FAMILIES = {"range", "dip", "mean reversion", "channel", "candlestick"}


def gate(sig_row: dict, reg: dict | None, is_btc: bool, fresh_bear: list) -> list[str]:
    """Reasons to BLOCK a long signal from market-wide context (empty list = allowed)."""
    why = []
    if fresh_bear:
        why.append("fresh bearish pattern: " + ", ".join(b["pattern"] for b in fresh_bear))
    if not reg:
        return why
    r, fam, status = reg["regime"], sig_row["family"], sig_row["status"]
    if r == "High-volatility event":
        why.append("high-volatility event — manual approval required")
    if r == "BTC selloff" and not is_btc:
        why.append("BTC selloff — altcoin longs disabled")
    if r == "Risk-off / stablecoin rotation" and (not is_btc or status != "ACCEPTED"):
        why.append("risk-off — only fully validated BTC longs allowed")
    if r == "Range / chop" and fam not in RANGE_FAMILIES:
        why.append("range/chop — trend and breakout modules paused")
    if not is_btc:
        if reg["alt_score"] < 50:
            why.append(f"altcoin eligibility score {reg['alt_score']:.0f} < 50")
        elif reg["alt_score"] < 70 and status != "ACCEPTED":
            why.append(f"altcoin score {reg['alt_score']:.0f} (selective) — only ACCEPTED strategies")
    elif reg["btc_score"] < 40:
        why.append(f"BTC regime score {reg['btc_score']:.0f} < 40")
    return why
