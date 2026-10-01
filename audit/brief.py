"""The trader brief: what the audit found, in the words a trader uses and in the order a trader needs them.

`build(a)` turns an audit result into a small structure — what to do now, the setup in one paragraph, the
plan with sizes, what would make it a buy, what would kill it, what is for and against, which strategies have
a real edge here, risk notes and how much to trust it. `to_markdown` puts it at the top of the report,
`to_text` is the notification / terminal version, and the web app renders the structure as its own card.
Numbers come from the checks' metrics, never re-computed, so the brief always agrees with the report."""
from __future__ import annotations

ACTIONS = {
    "BUY_ZONE": ("Buy zone is live", "good"),
    "WAIT_PULLBACK": ("Wait for the pullback", "warning"),
    "WAIT_BREAKOUT": ("Wait for the breakout", "warning"),
    "WATCH": ("Nothing to do yet — watch", "muted"),
    "AVOID": ("Stay out", "critical"),
    "STALE": ("No action — the data is stale", "critical"),
}
STATUS_WORD = {"ACCEPTED": "validated", "CANDIDATE": "promising but not fully validated", "REJECTED": "failed validation"}


def fmt_px(p) -> str:
    try:
        p = float(p)
    except (TypeError, ValueError):
        return "—"
    if p >= 100:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:,.4f}"
    return f"{p:.8f}".rstrip("0").rstrip(".")


def money(x) -> str:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "—"
    if abs(x) >= 1e9:
        return f"${x / 1e9:,.1f}B"
    if abs(x) >= 1e6:
        return f"${x / 1e6:,.1f}M"
    if abs(x) >= 1e4:
        return f"${x / 1e3:,.0f}k"
    return f"${x:,.0f}"


def pct(a, b) -> float:
    return (float(a) / float(b) - 1.0) * 100.0 if b else 0.0


def _tf_word(m: dict) -> str:
    up = sum([bool(m.get("close_gt_ema50")), any(v for k, v in m.items() if k.startswith("ema50_gt_")), bool(m.get("di_bullish"))])
    return "up" if up >= 3 else "mostly up" if up == 2 else "mostly down" if up == 1 else "down"


def _trend_sentence(tr: dict, tf: str) -> str:
    m = tr.get("metrics") or {}
    d1, own = m.get("1d") or {}, m.get(tf) or m.get("4h") or {}
    if not d1:
        return ""
    dw, ow = _tf_word(d1), _tf_word(own)
    s = f"The daily trend is {dw} and the {tf} trend is {ow}"
    if dw.endswith("down") and ow.endswith("up"):
        s += " — a bounce inside a larger downtrend, which is where most failed longs come from"
    elif dw.endswith("up") and ow.endswith("down"):
        s += " — a pullback inside a larger uptrend, the kind of dip that is worth watching"
    adx = own.get("adx")
    if adx is not None:
        s += f"; the {tf} trend strength is {'strong' if adx >= 30 else 'moderate' if adx >= 20 else 'weak (choppy)'} (ADX {adx:.0f})"
    return s + "."


def _momentum_sentence(mo: dict, tf: str) -> str:
    m = mo.get("metrics") or {}
    r, roc = m.get("rsi_tf"), m.get("roc_20_pct")
    if r is None:
        return ""
    if r >= 78:
        w = "overbought — buying here is chasing"
    elif r > 68:
        w = "stretched"
    elif r >= 50:
        w = "healthy"
    elif r >= 40:
        w = "soft"
    elif r >= 30:
        w = "weak"
    else:
        w = "oversold — a bounce is possible but the trend is against you"
    macd = m.get("macd_hist_tf")
    s = f"Momentum is {w} ({tf} RSI {r:.0f}"
    if macd is not None:
        s += f", MACD {'positive' if macd > 0 else 'negative'}"
    if roc is not None:
        s += f"; {roc:+.1f}% over the last 20 candles"
    return s + ")."


def _vol_sentence(vo: dict) -> tuple[str, str]:
    m = vo.get("metrics") or {}
    atr = m.get("atr_pct_1d")
    if atr is None:
        return "", "normal"
    level = "low" if atr < 1.5 else "normal" if atr <= 6 else "high" if atr <= 10 else "extreme"
    s = f"It moves about {atr:.1f}% a day ({level} volatility"
    har = m.get("har_rv") or {}
    if har.get("forecast_daily_vol_pct"):
        s += f"; the volatility model expects about {har['forecast_daily_vol_pct']:.1f}% for the next day"
    dd = m.get("max_dd_90d_pct")
    if dd is not None and dd <= -50:
        s += f"; it lost {abs(dd):.0f}% at one point in the last 90 days"
    return s + ").", level


def _rs_sentence(rs: dict) -> str:
    m = rs.get("metrics") or {}
    if "ret_30d_pct" not in m:
        return f"BTC is {'risk-on' if m.get('btc_risk_on') else 'risk-off (below its long daily average), which caps every altcoin long'}."
    r30, b30, r90, b90 = m["ret_30d_pct"], m["btc_ret_30d_pct"], m["ret_90d_pct"], m["btc_ret_90d_pct"]
    lead = r30 > b30 and r90 > b90
    lag = r30 < b30 and r90 < b90
    rel = "has been leading BTC" if lead else "has been lagging BTC" if lag else "is mixed against BTC"
    s = f"It {rel} ({r30:+.0f}% vs {b30:+.0f}% over 30 days, {r90:+.0f}% vs {b90:+.0f}% over 90), and BTC itself is "
    s += "risk-on." if m.get("btc_risk_on") else "risk-off — below its long daily average — which caps every altcoin long at WATCHLIST."
    return s


def _flow_sentence(fl: dict) -> str:
    m = fl.get("metrics") or {}
    tk, vr, p7 = m.get("taker_buy_share_7d"), m.get("volume_7d_vs_30d"), m.get("price_7d_pct")
    if tk is None:
        return ""
    side = "buyers are in control" if tk >= 0.54 else "sellers are in control" if tk <= 0.46 else "buyers and sellers are balanced"
    vol = "expanding" if (vr or 1) >= 1.2 else "fading" if (vr or 1) <= 0.8 else "normal"
    s = f"Volume is {vol} ({vr:.2f}× the 30-day average) and {side} ({tk:.0%} of trades hit the ask over 7 days"
    if p7 is not None:
        s += f"; price {p7:+.1f}% on the week"
    return s + ")."


def _liq_sentence(li: dict, ex: dict) -> str:
    m = li.get("metrics") or {}
    if "quote_volume_24h" not in m:
        return ""
    s = f"{money(m['quote_volume_24h'])} traded in 24h, spread {m.get('spread_bps', 0):.1f} bps, {money(m.get('depth_1pct_usd', 0))} of orders within 1% of the price"
    if ex.get("max_order_usd_at_backtest_slippage"):
        s += f" — orders up to about {money(ex['max_order_usd_at_backtest_slippage'])} keep the slippage the backtest assumed"
    return s + "."


def _one_liner(c: dict, tf: str) -> str:
    """One plain sentence per check, from its metrics (what a trader would say reading the same numbers)."""
    n, m = c["name"], c.get("metrics") or {}
    if n == "Liquidity":
        return f"Liquidity: {money(m.get('quote_volume_24h', 0))}/day, spread {m.get('spread_bps', 0):.1f} bps" + \
            (" — thin, slippage will hurt" if c["status"] == "fail" else " — fine" if c["status"] == "pass" else " — acceptable")
    if n == "Trend (multi-timeframe)":
        d1 = (m.get("1d") or {})
        own = m.get(tf) or m.get("4h") or {}
        return f"Trend: daily {_tf_word(d1)}, {tf} {_tf_word(own)}" if d1 else "Trend: unknown"
    if n == "Momentum":
        return f"Momentum: {tf} RSI {m.get('rsi_tf', 0):.0f}, {m.get('roc_20_pct', 0):+.1f}% over 20 candles"
    if n == "Volatility & risk":
        return f"Volatility: {m.get('atr_pct_1d', 0):.1f}% typical daily move" + (f", {m.get('max_dd_90d_pct'):.0f}% worst 90-day drawdown" if m.get("max_dd_90d_pct") is not None else "")
    if n == "Market regime & relative strength":
        if "ret_30d_pct" in m:
            return f"Versus BTC: {m['ret_30d_pct']:+.0f}% vs {m['btc_ret_30d_pct']:+.0f}% (30d); BTC {'risk-on' if m.get('btc_risk_on') else 'risk-off'}"
        return f"BTC {'risk-on' if m.get('btc_risk_on') else 'risk-off'}"
    if n == "Order flow & volume":
        return f"Flow: {m.get('taker_buy_share_7d', 0.5):.0%} taker-buy over 7 days, volume {m.get('volume_7d_vs_30d', 1):.2f}× normal"
    if n == "Crypto-wide & macro regime":
        return f"Market regime: {m['regime']} ({m.get('score', 0):.0f}/100)" if m.get("regime") else "Market regime: not available"
    if n == "Strategy library (validated)":
        return f"Strategies: {m.get('accepted', 0)} validated, {m.get('candidates', 0)} candidates, {m.get('active_approved', 0)} firing now"
    if n == "ML meta-labeler":
        return "ML filter: " + ("rates the live signals" if m.get("has_edge") else "no proven edge here" if m.get("available") else "skipped")
    return n


def _horizon_days(p: dict) -> str:
    h = str(p.get("horizon") or "")
    return h.split("(")[0].strip() if "(" in h else h


def build(a: dict) -> dict:
    p, tf, v = a["plan"], a["timeframe"], a["verdict"]
    price = float(p["price"])
    base = a.get("base") or a["symbol"][:-4]
    quote = a.get("quote") or a["symbol"][-4:]
    checks = {c["name"]: c for c in a.get("checks", [])}
    signals = a.get("signals") or []
    approved = [s for s in signals if s.get("decision") == "APPROVED"]
    blocked = [s for s in signals if s.get("decision") != "APPROVED"]
    pats = a.get("patterns") or {}
    pending, bearish = pats.get("pending_bullish") or [], pats.get("fresh_bearish") or []
    flags = list(a.get("flags") or [])
    stale = any(str(f).startswith("STALE") for f in flags)
    rows = a.get("strategies") or []
    acc = [r for r in rows if r.get("status") == "ACCEPTED"]
    cand = [r for r in rows if r.get("status") == "CANDIDATE"]
    rsm = (checks.get("Market regime & relative strength") or {}).get("metrics") or {}
    btc_on = bool(rsm.get("btc_risk_on", True))
    ex = p.get("execution") or {}
    lo, hi, stop, t1, t2 = (float(p[k]) for k in ("entry_low", "entry_high", "stop", "target1", "target2"))
    mid = (lo + hi) / 2
    risk = mid - stop
    rr1 = (t1 - mid) / risk if risk > 0 else 0.0
    rr2 = (t2 - mid) / risk if risk > 0 else 0.0
    in_zone = lo * 0.998 <= price <= hi * 1.002

    # ---- what to do now
    if stale:
        code = "STALE"
    elif v == "AVOID":
        code = "AVOID"
    elif approved:
        code = "BUY_ZONE" if in_zone else "WAIT_PULLBACK"
    elif pending:
        code = "WAIT_BREAKOUT"
    else:
        code = "WATCH"
    label, tone = ACTIONS[code]
    names = ", ".join(s["strategy_id"] for s in approved[:3])
    if code == "BUY_ZONE":
        text = (f"{len(approved)} validated signal{'s' if len(approved) > 1 else ''} ({names}) fired on the last closed {tf} candle and the "
                f"backdrop agrees. Buy between {fmt_px(lo)} and {fmt_px(hi)} {quote}, stop {fmt_px(stop)} ({pct(stop, price):+.1f}%), "
                f"first target {fmt_px(t1)} ({pct(t1, price):+.1f}%, {rr1:.1f}R). Expected hold {_horizon_days(p)}.")
    elif code == "WAIT_PULLBACK":
        text = (f"A validated signal is live ({names}) but price ({fmt_px(price)}) is above the entry zone "
                f"{fmt_px(lo)}–{fmt_px(hi)}. Do not chase: buy only if price comes back into the zone, otherwise skip it.")
    elif code == "WAIT_BREAKOUT":
        x = pending[0]
        text = (f"No validated signal yet. A {x['pattern'].replace('_', ' ')} is forming: a {tf} candle closing above "
                f"{fmt_px(x['trigger_close_above'])} on above-average volume would complete it; it is void below "
                f"{fmt_px(x['invalidation'])} and expires in {x['expires_in_bars']} candles. Set an alert at the trigger and wait for the close.")
    elif code == "AVOID":
        neg = [c for c in a.get("checks", []) if c.get("status") == "fail"]
        why = "; ".join(_one_liner(c, tf) for c in neg[:2]) or "the score is too low"
        text = f"The backdrop is against a spot long ({why}). Stay out and re-check after the next daily close."
    elif code == "STALE":
        text = "The last candle is hours old — the data feed stalled. No level in this report is valid until a fresh candle arrives."
    else:
        text = ("No validated signal and nothing forming on the chart. There is nothing to do on this coin right now; "
                "the levels below are reference points for when something changes.")
    if code in ("WATCH", "WAIT_BREAKOUT") and not btc_on:
        text += " BTC is risk-off, which by itself blocks new altcoin longs."

    # ---- the setup in one paragraph
    vol_s, vol_level = _vol_sentence(checks.get("Volatility & risk") or {})
    summary = " ".join(s for s in [
        _trend_sentence(checks.get("Trend (multi-timeframe)") or {}, tf),
        _momentum_sentence(checks.get("Momentum") or {}, tf),
        vol_s,
        _rs_sentence(checks.get("Market regime & relative strength") or {}),
        _flow_sentence(checks.get("Order flow & volume") or {}),
    ] if s)
    mr = a.get("market_regime") or {}
    if mr.get("regime"):
        summary += f" The wider crypto market reads as \"{mr['regime']}\": {mr.get('behaviour', '')}".rstrip() + ("" if summary.endswith(".") else ".")

    # ---- the plan
    account = float(p.get("account_usd") or 0)
    risk_frac = float(p.get("risk_fraction_used") or 0)
    plan = [f"Entry zone {fmt_px(lo)} – {fmt_px(hi)} {quote} (last close {fmt_px(price)})",
            f"Stop {fmt_px(stop)} — {abs(pct(stop, price)):.1f}% below the close, under the recent swing low",
            f"Target 1 {fmt_px(t1)} ({pct(t1, price):+.1f}%, {rr1:.1f}R) · Target 2 {fmt_px(t2)} ({pct(t2, price):+.1f}%, {rr2:.1f}R)",
            f"Expected hold {_horizon_days(p)}; first resistance {fmt_px(p.get('resistance_20_bars'))} (20-candle high)"]
    if ex.get("position_usd") and account:
        plan.append(f"Size for a {money(account)} account risking {risk_frac:.1%}: about {money(ex['position_usd'])} "
                    f"({float(ex.get('units', 0)):.4g} {base}, {ex.get('position_pct_of_account', 0):.1f}% of the account); "
                    f"the stop costs {money(account * risk_frac)}. Slippage at that size ≈ {ex.get('slippage_bps_at_size', 0):.1f} bps.")
    elif p.get("position_size_pct_of_account") is not None:
        plan.append(f"Size for 1% account risk: {p['position_size_pct_of_account']}% of the account.")
    if code not in ("BUY_ZONE", "WAIT_PULLBACK"):
        plan.append("These are reference levels — there is no approved signal, so no position is suggested.")

    # ---- what would make this a buy / what would kill it
    would_buy, would_kill = [], []
    if code not in ("BUY_ZONE",):
        for x in pending[:3]:
            would_buy.append(f"a {tf} close above {fmt_px(x['trigger_close_above'])} with at least 1.2× the average volume "
                             f"({x['pattern'].replace('_', ' ')}; void below {fmt_px(x['invalidation'])})")
        if code == "WAIT_PULLBACK":
            would_buy.append(f"price back inside {fmt_px(lo)}–{fmt_px(hi)} while the signal is still valid")
        if not btc_on:
            would_buy.append("BTC back above its long daily average (risk-on) — until then altcoin longs are capped")
        if not acc and not cand:
            would_buy.append(f"a strategy with a validated edge firing — none of the {len(rows)} tested has one on this coin today")
        elif not approved:
            would_buy.append("one of the validated strategies below firing on a closed candle in a matching regime")
        trm = ((checks.get("Trend (multi-timeframe)") or {}).get("metrics") or {}).get("1d") or {}
        if trm and not trm.get("close_gt_ema50"):
            would_buy.append("the daily trend turning up (price above a rising 50-day average)")
        if bearish:
            would_buy.append(f"price reclaiming {fmt_px(bearish[0]['level'])}, the level the {bearish[0]['pattern'].replace('_', ' ')} broke")
        if stale:
            would_buy.insert(0, "a fresh candle — the feed must catch up first")
    would_kill.append(f"a {tf} close below the stop {fmt_px(stop)} ({pct(stop, price):+.1f}%)")
    for x in pending[:2]:
        would_kill.append(f"the {x['pattern'].replace('_', ' ')} is void below {fmt_px(x['invalidation'])}")
    for x in bearish[:2]:
        would_kill.append(f"a bearish {x['pattern'].replace('_', ' ')} completed {x['bars_ago']} candles ago — new longs are blocked")
    if btc_on:
        would_kill.append("BTC turning risk-off (below its long daily average) blocks new entries")
    for s in blocked[:3]:
        reason = str(s.get("decision", "")).replace("BLOCKED: ", "")
        would_kill.append(f"{s['strategy_id']} fired but is blocked: {reason}")

    # ---- for / against
    plus, minus = [], []
    for c in a.get("checks", []):
        if c.get("weight_mult", 1.0) == 0.0:
            continue
        line = _one_liner(c, tf)
        (plus if c.get("status") == "pass" else minus if c.get("status") == "fail" else plus if c.get("score", 0) >= 55 else minus).append(line)

    # ---- the strategies with an edge on this coin
    edge = []
    for r in acc + cand:
        wf, bm = r.get("wf") or {}, r.get("benchmark") or {}
        line = (f"{r['name']} — {STATUS_WORD[r['status']]}: {wf.get('expectancy_pct', 0):+.2f}% per trade after costs over "
                f"{wf.get('trades', 0)} walk-forward trades (wins {wf.get('win_rate', 0):.0%}, profit factor {wf.get('profit_factor', 0)}), "
                f"about {bm.get('trades_per_year', 0):.0f} trades a year")
        if r.get("signal_now"):
            dec = next((s for s in signals if s["strategy_id"] == r["id"]), None)
            line += " · FIRING NOW" + ("" if not dec or dec["decision"] == "APPROVED" else f" but blocked ({dec['decision'].replace('BLOCKED: ', '')})")
        else:
            line += " · no signal on the last candle"
        edge.append(line)
    if not edge:
        edge.append(f"None of the {len(rows)} strategies tested survived the 12 validation gates on this coin — the backtests show no "
                    f"repeatable edge here right now. Everything above is a read of the chart, not a tested trade.")

    # ---- risk notes
    risk_notes = [s for s in [_liq_sentence(checks.get("Liquidity") or {}, ex)] if s]
    if vol_level in ("high", "extreme"):
        risk_notes.append(f"{vol_level.capitalize()} volatility: use the size above or smaller; a normal day can move more than a tight stop.")
    if ex.get("exceeds_1pct_depth"):
        risk_notes.append("The suggested size is larger than the order book within 1% — split the order or size down.")
    tr = a.get("track_record") or {}
    for k, t in tr.items():
        if t.get("drift"):
            risk_notes.append(f"{k} has done worse live ({t['live_win_rate']:.0%} of {t['n']} graded signals) than in its backtest "
                              f"({t['backtest_win_rate']:.0%}) — its signals are blocked until that recovers.")
    for f in flags:
        if not f.startswith("STALE"):
            risk_notes.append(f)

    # ---- confidence
    meta = a.get("meta") or {}
    if stale:
        level, conf = "none", "The data is stale; nothing here can be trusted until the feed catches up."
    elif approved and len(acc) >= 2:
        level, conf = "high", f"{len(acc)} validated strategies agree with the setup and one is firing."
    elif approved or acc:
        level, conf = "medium", (f"{len(acc)} validated and {len(cand)} candidate strategies out of {len(rows)} tested; "
                                 + ("one is firing now." if approved else "none is firing now."))
    elif cand:
        level, conf = "low", f"Only candidate strategies ({len(cand)}) passed on this coin — treat any trade as small and experimental."
    else:
        level, conf = "low", "No strategy passed validation on this coin; the read above is descriptive, not a tested edge."
    if meta.get("available") and meta.get("has_edge"):
        conf += " The ML filter has a proven edge on this coin's signals and rates each live one."
    elif meta.get("available"):
        conf += " The ML filter found no proven edge here and is ignored."

    return {"headline": f"{base}/{quote} · {tf} — {v} {a['score']:.0f}/100", "action": {"code": code, "label": label, "tone": tone, "text": text},
            "summary": summary, "plan": plan, "would_buy": would_buy, "would_kill": would_kill, "plus": plus, "minus": minus,
            "edge": edge, "risk": risk_notes, "confidence": {"level": level, "text": conf},
            "levels": {"price": price, "entry_low": lo, "entry_high": hi, "stop": stop, "target1": t1, "target2": t2,
                       "rr1": round(rr1, 2), "rr2": round(rr2, 2)}}


def to_markdown(b: dict) -> list[str]:
    L = ["## Trader brief", "", f"**{b['action']['label'].upper()}** — {b['action']['text']}", "",
         "**The setup.** " + b["summary"], "", "**The plan**", ""]
    L += [f"- {x}" for x in b["plan"]]
    if b["would_buy"]:
        L += ["", "**What would make this a buy**", ""] + [f"- {x}" for x in b["would_buy"]]
    L += ["", "**What would kill it**", ""] + [f"- {x}" for x in b["would_kill"]]
    L += ["", "**For**: " + ("; ".join(b["plus"]) if b["plus"] else "nothing stands out") + ".",
          "", "**Against**: " + ("; ".join(b["minus"]) if b["minus"] else "nothing stands out") + ".",
          "", "**Strategies with an edge on this coin**", ""] + [f"- {x}" for x in b["edge"]]
    if b["risk"]:
        L += ["", "**Risk notes**", ""] + [f"- {x}" for x in b["risk"]]
    L += ["", f"**Confidence: {b['confidence']['level']}.** {b['confidence']['text']}", ""]
    return L


def to_text(b: dict, max_len: int = 1800) -> str:
    lines = [b["headline"], f"{b['action']['label'].upper()}: {b['action']['text']}", ""]
    lines += b["plan"][:4]
    if b["would_buy"]:
        lines += ["", "Would become a buy if: " + "; ".join(b["would_buy"][:3])]
    lines += ["", "Kill: " + "; ".join(b["would_kill"][:2]), f"Confidence {b['confidence']['level']}: {b['confidence']['text']}"]
    out = "\n".join(lines)
    return out if len(out) <= max_len else out[: max_len - 1] + "…"
