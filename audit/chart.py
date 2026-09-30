"""The chart of an audited coin with the opportunity drawn on it.

`build()` turns the audit result plus the candles it analysed into a compact JSON structure (candles,
EMAs, pivots, the trade plan, forming / completed patterns, strategy signals) that `static/chart.js`
renders in the browser: an interactive candlestick chart with the entry zone, stop, targets and pattern
triggers on the price, and an "opportunity" panel next to it. `standalone_html()` writes it as a
self-contained page next to the report (works from file://); the web app embeds the same data with
`embed_html()` and serves the script from /static/chart.js under its strict Content-Security-Policy."""
from __future__ import annotations

import base64
import hashlib
import html
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

import patterns as pt
from binance_client import TF_MS, epoch_ms
from dashboard import CSS as BASE_CSS

STATIC = Path(__file__).resolve().with_name("static")
CHART_JS = (STATIC / "chart.js").read_text(encoding="utf-8")
CHART_JS_HASH = "sha256-" + base64.b64encode(hashlib.sha256(CHART_JS.encode("utf-8")).digest()).decode("ascii")
WINDOW = 500                    # candles embedded per audit (zoom/pan in the browser; the audit itself uses more)
E = html.escape

CSS = """
:root { --down:#d03b3b; }
.cc-root { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:10px 12px 8px; margin:12px 0; }
.cc-top { display:flex; flex-wrap:wrap; gap:8px 14px; align-items:center; justify-content:space-between; margin-bottom:6px; }
.cc-title { font-size:15px; } .cc-title .cc-tf { color:var(--ink2); }
.cc-legend { display:flex; flex-wrap:wrap; gap:4px 10px; font-size:12px; color:var(--ink2); font-variant-numeric:tabular-nums; }
.cc-legend .cc-k { margin-right:3px; color:var(--muted); }
.cc-tools { display:flex; gap:4px; flex-wrap:wrap; }
.cc-btn { background:transparent; color:var(--ink2); border:1px solid var(--border); border-radius:6px; padding:3px 8px; font:inherit; font-size:12px; cursor:pointer; margin:0; }
.cc-btn:hover { color:var(--ink); border-color:var(--ink2); } .cc-btn.off { opacity:.45; text-decoration:line-through; }
.cc-body { display:grid; grid-template-columns:minmax(0, 1fr) 300px; gap:12px; }
@media (max-width:860px) { .cc-body { grid-template-columns:1fr; } }
.cc-canvas-wrap { position:relative; min-width:0; outline:none; border-radius:8px; overflow:hidden; }
.cc-canvas-wrap:focus-visible { box-shadow:0 0 0 2px var(--accent); }
.cc-canvas-wrap canvas { display:block; width:100%; cursor:crosshair; touch-action:none; }
.cc-tip { display:none; }
.cc-panel { font-size:13px; min-width:0; }
.cc-panel h4 { margin:14px 0 6px; font-size:12px; text-transform:uppercase; letter-spacing:.04em; color:var(--ink2); }
.cc-verdict { display:flex; align-items:center; gap:8px; font-weight:700; font-size:16px; }
.cc-verdict.good { color:var(--good); } .cc-verdict.warning { color:var(--warning); } .cc-verdict.critical { color:var(--critical); }
.cc-verdict.muted { color:var(--ink2); } .cc-vscore { margin-left:auto; font-variant-numeric:tabular-nums; color:var(--ink); }
.cc-meter { height:8px; background:var(--track); border-radius:4px; margin:6px 0 8px; overflow:hidden; }
.cc-meter i { display:block; height:100%; background:var(--accent); }
.cc-vtext { margin:0 0 6px; color:var(--ink); } .cc-flag { margin:4px 0; color:var(--warning); font-size:12px; }
table.cc-plan { width:100%; min-width:0; border-collapse:collapse; table-layout:fixed; }
.cc-plan td { padding:3px 0; border-bottom:1px solid var(--grid); vertical-align:top; overflow-wrap:anywhere; }
.cc-plan td:first-child { color:var(--ink2); padding-right:8px; width:44%; }
.cc-num { text-align:right; font-variant-numeric:tabular-nums; }
.cc-num.up { color:var(--good); } .cc-num.down { color:var(--critical); } .cc-num.accent { color:var(--accent); }
ul.cc-list { list-style:none; margin:0; padding:0; } .cc-list li { padding:4px 0; border-bottom:1px solid var(--grid); }
.cc-list li.ok b { color:var(--good); } .cc-list li.blocked b { color:var(--ink2); }
.cc-list li.forming b { color:var(--warning); } .cc-list li.bear b { color:var(--critical); }
.cc-muted { color:var(--ink2); margin:6px 0; } .cc-stamp { color:var(--muted); font-size:12px; margin-top:12px; }
.cc-help { color:var(--muted); font-size:12px; margin-top:8px; }
"""


def _json(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return str(o)


def price_decimals(price: float, tick: float = 0.0) -> int:
    """Decimals to print a price with: the exchange tick size when known, else by magnitude."""
    if tick and tick > 0:
        return int(max(0, min(10, round(-math.log10(tick)))))
    if price >= 1000:
        return 2
    if price >= 1:
        return 4
    if price >= 0.01:
        return 6
    return 8


def _num(x, rp: int):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(v) else round(v, rp)


def build(a: dict, df: pd.DataFrame, window: int = WINDOW, tick: float = 0.0) -> dict:
    """Chart data for one audit: `a` is the audit result (plan, signals, patterns, verdict) and `df` the
    candles it analysed, with indicators. Indices in the output refer to the embedded candle window."""
    tail = df.tail(window)
    off, n = len(df) - len(tail), len(tail)
    plan = a["plan"]
    price = float(plan["price"])
    dec = price_decimals(price, tick)
    rp = dec + 2
    ot = epoch_ms(tail.index)
    cols = [tail[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close", "volume")]
    candles = [[int(t), _num(o, rp), _num(h, rp), _num(l, rp), _num(c, rp), _num(v, 3)]
               for t, o, h, l, c, v in zip(ot, *cols)]
    ema = {k: [_num(x, rp) for x in tail[k].to_numpy(dtype=float)] for k in ("ema20", "ema50", "ema200") if k in tail.columns}
    hi, lo = pt.pivots(df) if len(df) > 2 * pt.K else (np.array([], int), np.array([], int))
    highs, lows = df["high"].to_numpy(dtype=float), df["low"].to_numpy(dtype=float)
    pivots = {"highs": [[int(i - off), _num(highs[i], rp)] for i in hi if i >= off],
              "lows": [[int(i - off), _num(lows[i], rp)] for i in lo if i >= off]}

    mid = (float(plan["entry_low"]) + float(plan["entry_high"])) / 2
    risk = mid - float(plan["stop"])
    p = {k: plan[k] for k in ("price", "entry_low", "entry_high", "stop", "target1", "target2", "stop_distance_pct",
                              "position_size_pct_of_account", "horizon", "resistance_20_bars", "resistance_90d") if k in plan}
    p["rr1"] = round((float(plan["target1"]) - mid) / risk, 2) if risk > 0 else None
    p["rr2"] = round((float(plan["target2"]) - mid) / risk, 2) if risk > 0 else None

    pats = a.get("patterns") or {}
    patterns = []
    for x in pats.get("pending_bullish", []):
        start = x.get("start")
        patterns.append({"kind": "bullish", "name": x["pattern"], "trigger": x["trigger_close_above"],
                         "invalidation": x["invalidation"], "expires_in_bars": x["expires_in_bars"],
                         "start": int(start - off) if isinstance(start, (int, np.integer)) and start >= off else None})
    for x in pats.get("fresh_bearish", []):
        patterns.append({"kind": "bearish", "name": x["pattern"], "bars_ago": x["bars_ago"], "level": x["level"]})
    markers = []
    for x in pats.get("recent_candles", []):
        i = n - 1 - int(x["bars_ago"])
        if i >= 0 and x.get("candles"):
            markers.append({"kind": "candle", "i": i, "text": ", ".join(x["candles"])})
    signals = []
    for s in a.get("signals", []):
        approved = s["decision"] == "APPROVED"
        signals.append({"strategy": s["strategy_id"], "approved": approved, "decision": s["decision"],
                        "confidence": s["confidence"], "risk": s["max_risk_fraction"], "entry_type": s["entry_type"],
                        "price": s["price"], "stop": s["stop_px"], "target": s["target_px"]})
    approved = [s for s in signals if s["approved"]]
    if approved and n:
        markers.append({"kind": "signal", "i": n - 1, "approved": True,
                        "text": ", ".join(s["strategy"] for s in approved[:3]) + (" +" if len(approved) > 3 else "")})
    elif signals and n:
        markers.append({"kind": "signal", "i": n - 1, "approved": False,
                        "text": f"{len(signals)} signal{'s' if len(signals) > 1 else ''} blocked"})
    levels = [{"price": plan[k], "label": lbl} for k, lbl in (("resistance_20_bars", "20-bar high"), ("resistance_90d", "90-day high"))
              if isinstance(plan.get(k), (int, float)) and price < plan[k] <= price * 1.3]
    reg, mr = a.get("regime") or {}, a.get("market_regime") or {}
    return {"symbol": a["symbol"], "timeframe": a["timeframe"], "tf_ms": TF_MS.get(a["timeframe"], TF_MS["4h"]),
            "exchange": "Binance Spot" + (" · offline data" if a.get("offline") else ""), "price_decimals": dec,
            "candles": candles, "ema": ema, "pivots": pivots, "plan": p, "patterns": patterns, "markers": markers,
            "signals": signals, "levels": levels, "verdict": a["verdict"], "score": a["score"],
            "verdict_text": a.get("verdict_text", ""), "flags": list(a.get("flags", [])),
            "structure": pats.get("structure"),
            "regime": {"trend": reg.get("trend_state"), "vol": reg.get("vol_state"), "btc": reg.get("btc"),
                       "market": mr.get("regime")} if reg else None,
            "audit_time_utc": a.get("audit_time_utc", ""), "data_until_utc": a.get("data_until_utc", ""), "window": n}


def embed_html(chart: dict, element_id: str = "coin-chart", inline_script: bool = False) -> str:
    """The chart container plus its data. With `inline_script` the renderer is embedded (file:// reports);
    otherwise the page loads /static/chart.js (the web app)."""
    data = json.dumps(chart, separators=(",", ":"), default=_json).replace("<", "\\u003c")
    frag = (f'<script type="application/json" id="{E(element_id)}-data">{data}</script>'
            f'<div class="coin-chart" id="{E(element_id)}" data-coin-chart="{E(element_id)}-data"></div>')
    return frag + (f"<script>{CHART_JS}</script>" if inline_script else '<script src="/static/chart.js"></script>')


def standalone_html(a: dict) -> str:
    """A self-contained page: the chart, the opportunity panel and a link back to the report."""
    ch = a["chart"]
    title = f"{ch['symbol']} {ch['timeframe']} — chart"
    report = a.get("report_path", "")
    back = f'<p class="sub"><a href="{E(report)}">← full report</a> · <a href="dashboard.html">dashboard</a></p>' if report else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{E(title)} · Coin Audit</title><style>{BASE_CSS}{CSS}</style></head><body>'
            f'<h1>{E(ch["symbol"])} · {E(ch["timeframe"])} — {E(ch["verdict"])} {E(str(ch["score"]))}/100</h1>{back}'
            + embed_html(ch, inline_script=True)
            + '<p class="foot">Read-only research on public market data · levels are reference points, not orders · not financial advice.</p>'
            '</body></html>')
