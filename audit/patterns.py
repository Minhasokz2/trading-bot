"""Chart-pattern engine: pivots -> formations -> confirmed breakouts.

Every pattern is built from CONFIRMED swing pivots (a pivot at bar i is only known at
bar i+K), so detection never uses future candles. A formation becomes a trade only when:
  prior context  (reversal patterns need a preceding trend)
  geometry       (ATR-adjusted tolerances, minimum size, line fit quality)
  candle close   beyond the neckline / boundary (a wick is not enough)
  volume         relative volume >= vol_mult on the breakout candle
  structure      breakout also clears the last swing high (bullish BOS)
  risk/reward    measured-move target / invalidation stop >= MIN_RR
  (optional)     retest entry: limit order at the broken level instead of chasing the close

Bullish formations become long signals (spot). Bearish formations cannot be traded on spot:
they are measured as research (would a short have worked?) and, when freshly completed,
they block new long signals.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

K = 3            # pivot half-window (confirmed K candles later)
MIN_RR = 1.5     # minimum reward:risk for an actionable signal
MAX_WAIT = 25    # candles a formation stays valid waiting for its breakout


# =================================================================== pivots
def pivots(df: pd.DataFrame, k: int = K):
    """Return (high_idx, low_idx): bars that are the extreme of a 2k+1 window."""
    h, lo = df["high"].values, df["low"].values
    n = len(df)
    hi_idx, lo_idx = [], []
    for i in range(k, n - k):
        w_h, w_l = h[i - k:i + k + 1], lo[i - k:i + k + 1]
        if h[i] == w_h.max() and (w_h == h[i]).sum() == 1:
            hi_idx.append(i)
        if lo[i] == w_l.min() and (w_l == lo[i]).sum() == 1:
            lo_idx.append(i)
    return np.array(hi_idx, int), np.array(lo_idx, int)


def tolerance(df: pd.DataFrame, i: int) -> float:
    """ATR-adjusted equality tolerance, bounded to 0.5%-1.5% of price."""
    return float(np.clip(0.5 * df["atr_pct"].iloc[i], 0.005, 0.015))


def last_pivot_high_before(hi_idx, t, k=K):
    ok = hi_idx[hi_idx + k <= t]
    return int(ok[-1]) if len(ok) else None


def structure(df, hi_idx, lo_idx, t, k=K) -> str:
    """'bullish' = higher high + higher low, 'bearish' = lower high + lower low (confirmed pivots)."""
    h = hi_idx[hi_idx + k <= t][-2:]
    l = lo_idx[lo_idx + k <= t][-2:]
    if len(h) < 2 or len(l) < 2:
        return "unknown"
    H, L = df["high"].values, df["low"].values
    if H[h[1]] > H[h[0]] and L[l[1]] > L[l[0]]:
        return "bullish"
    if H[h[1]] < H[h[0]] and L[l[1]] < L[l[0]]:
        return "bearish"
    return "mixed"


def last_pivot_index_before(piv_idx, n: int, k: int = K) -> np.ndarray:
    """For every bar t: the most recent pivot confirmed by t (pivot + k <= t), or -1 (vectorised)."""
    pos = np.searchsorted(np.asarray(piv_idx) + k, np.arange(n), side="right") - 1
    return np.where(pos >= 0, np.asarray(piv_idx)[np.maximum(pos, 0)], -1) if len(piv_idx) else np.full(n, -1)


def structure_series(df, hi_idx, lo_idx, k=K) -> np.ndarray:
    """structure(df, hi, lo, t) for every t at once (same labels, O(n) instead of O(n x pivots))."""
    n = len(df)
    H, L = df["high"].values, df["low"].values
    out = np.full(n, "unknown", dtype=object)
    t = np.arange(n)
    ch = np.searchsorted(np.asarray(hi_idx) + k, t, side="right")
    cl = np.searchsorted(np.asarray(lo_idx) + k, t, side="right")
    ok = (ch >= 2) & (cl >= 2)
    if not ok.any():
        return out
    h1, h0 = hi_idx[np.maximum(ch - 1, 0)], hi_idx[np.maximum(ch - 2, 0)]
    l1, l0 = lo_idx[np.maximum(cl - 1, 0)], lo_idx[np.maximum(cl - 2, 0)]
    hh, hl = H[h1] > H[h0], L[l1] > L[l0]
    lh, ll = H[h1] < H[h0], L[l1] < L[l0]
    out[ok & hh & hl] = "bullish"
    out[ok & lh & ll] = "bearish"
    out[ok & ~(hh & hl) & ~(lh & ll)] = "mixed"
    return out


def prior_downtrend(df, i, bars=30, atr_mult=3.0) -> bool:
    lo_ = max(0, i - bars)
    peak = df["high"].values[lo_:i + 1].max()
    return (peak - df["low"].values[i]) >= atr_mult * df["atr"].values[i] and \
        df["close"].values[i] <= df["ema50"].values[i]


def prior_uptrend(df, i, bars=30, atr_mult=3.0) -> bool:
    lo_ = max(0, i - bars)
    trough = df["low"].values[lo_:i + 1].min()
    return (df["high"].values[i] - trough) >= atr_mult * df["atr"].values[i] and \
        df["close"].values[i] >= df["ema50"].values[i]


# ============================================================ setup helpers
def _setup(name, start, valid_from, level, stop, target, side="long", expiry=None, line=None):
    """level: flat neckline price, or line=(i0, p0, slope) for sloped boundaries."""
    return {"name": name, "start": int(start), "valid_from": int(valid_from), "level": level,
            "line": line, "stop": float(stop), "target": float(target), "side": side,
            "expiry": int(expiry if expiry is not None else valid_from + MAX_WAIT)}


def _level_at(s, t):
    if s["line"] is not None:
        i0, p0, sl = s["line"]
        return p0 + sl * (t - i0)
    return s["level"]


def trigger(df: pd.DataFrame, setups: list, vol_mult: float = 1.2, entry: str = "close",
            min_rr: float = MIN_RR, need_bos: bool = True, hi_idx=None, stop_mode: str = "pattern"):
    """Turn formations into candle-close-confirmed signals.
    stop_mode 'pattern' = beyond the pattern extreme; 'tight' = 1.5 ATR beyond the broken level
    (whichever is closer), which is how most traders get >= 1.5R on measured-move targets.
    Returns arrays (entry, stop_px, tp_px, limit_px) and the list of fired events."""
    n = len(df)
    c, vr, a = df["close"].values, df["vol_ratio"].values, df["atr"].values
    H = df["high"].values
    e = np.zeros(n, bool)
    stop_px, tp_px, limit_px = (np.full(n, np.nan) for _ in range(3))
    best_rr = np.zeros(n)
    events = []
    for s in setups:
        if "stop" not in s:
            continue
        for t in range(s["valid_from"], min(s["expiry"], n - 1) + 1):
            lvl = _level_at(s, t)
            if s["side"] == "long":
                if c[t] <= s["stop"]:
                    break                                   # invalidated before breakout
                if c[t] <= lvl:
                    continue
                if not np.isfinite(vr[t]) or vr[t] < vol_mult:
                    break                                   # first close above lacked volume -> reject
                if need_bos and hi_idx is not None:
                    ph = last_pivot_high_before(hi_idx, t)
                    if ph is not None and c[t] <= H[ph] and ph > s["start"]:
                        break
                ref = lvl + 0.1 * a[t] if entry == "retest" else c[t]
                stp = max(s["stop"], lvl - 1.5 * a[t]) if stop_mode == "tight" else s["stop"]
                tgt = s["target"] if np.isfinite(s["target"]) else ref + 2 * (ref - stp)
                risk, reward = ref - stp, tgt - ref
                rr = reward / risk if risk > 0 else 0
                if rr >= min_rr and rr > best_rr[t]:
                    e[t], stop_px[t], tp_px[t], best_rr[t] = True, stp, tgt, rr
                    if entry == "retest":
                        limit_px[t] = ref
                    events.append({**s, "t": t, "rr": round(rr, 2)})
                break
            else:                                           # bearish: research events only
                if c[t] >= s["stop"]:
                    break
                if c[t] >= lvl:
                    continue
                if not np.isfinite(vr[t]) or vr[t] < vol_mult:
                    break
                events.append({**s, "t": t})
                break
    return (e, stop_px, tp_px, limit_px), events


# ======================================================= reversal patterns
def double_bottom(df, hi, lo, triple=False, side="long"):
    """side='long': double/triple bottom (W). side='short': double/triple top (M)."""
    H, L, a = df["high"].values, df["low"].values, df["atr"].values
    piv, opp = (lo, hi) if side == "long" else (hi, lo)
    P = L if side == "long" else H
    need = 3 if triple else 2
    name = ("triple_" if triple else "double_") + ("bottom" if side == "long" else "top")
    out = []
    for j in range(need - 1, len(piv)):
        pts = piv[j - need + 1:j + 1]
        if np.any(np.diff(pts) < 5) or pts[-1] - pts[0] > 150:
            continue
        vals = P[pts]
        tol = tolerance(df, pts[-1])
        if (vals.max() - vals.min()) / vals.mean() > tol:
            continue
        between = opp[(opp > pts[0]) & (opp < pts[-1])]
        if len(between) == 0:
            continue
        if side == "long":
            neck = H[between].max()
            if neck - vals.max() < 2 * a[pts[-1]] or not prior_downtrend(df, pts[0]):
                continue
            base = vals.min()
            out.append(_setup(name, pts[0], pts[-1] + K, neck, base - 0.2 * a[pts[-1]], neck + (neck - base),
                              expiry=pts[-1] + K + (pts[-1] - pts[0])))
        else:
            neck = L[between].min()
            if vals.min() - neck < 2 * a[pts[-1]] or not prior_uptrend(df, pts[0]):
                continue
            top = vals.max()
            out.append(_setup(name, pts[0], pts[-1] + K, neck, top + 0.2 * a[pts[-1]], neck - (top - neck),
                              side="short", expiry=pts[-1] + K + (pts[-1] - pts[0])))
    return out


def head_shoulders(df, hi, lo, side="long"):
    """side='long': inverse head & shoulders. side='short': head & shoulders."""
    H, L, a = df["high"].values, df["low"].values, df["atr"].values
    piv, opp = (lo, hi) if side == "long" else (hi, lo)
    P, Q = (L, H) if side == "long" else (H, L)
    out = []
    for j in range(2, len(piv)):
        ls, hd, rs = piv[j - 2], piv[j - 1], piv[j]
        if rs - ls > 150 or hd - ls < 3 or rs - hd < 3:
            continue
        tol = tolerance(df, rs)
        if side == "long" and not P[hd] < min(P[ls], P[rs]) - 0.5 * a[hd]:
            continue                                            # head must be the clear low
        if side == "short" and not P[hd] > max(P[ls], P[rs]) + 0.5 * a[hd]:
            continue                                            # head must be the clear high
        if abs(P[ls] - P[rs]) / P[hd] > 2 * tol:
            continue
        n1 = opp[(opp > ls) & (opp < hd)]
        n2 = opp[(opp > hd) & (opp < rs)]
        if len(n1) == 0 or len(n2) == 0:
            continue
        n1 = n1[np.argmax(Q[n1])] if side == "long" else n1[np.argmin(Q[n1])]
        n2 = n2[np.argmax(Q[n2])] if side == "long" else n2[np.argmin(Q[n2])]
        slope = (Q[n2] - Q[n1]) / (n2 - n1)
        if abs(Q[n2] - Q[n1]) > 3 * a[rs]:
            continue
        neck_hd = Q[n1] + slope * (hd - n1)
        depth = abs(neck_hd - P[hd])
        if side == "long":
            if not prior_downtrend(df, ls):
                continue
            neck_now = Q[n1] + slope * (rs + K - n1)
            out.append(_setup("inverse_head_shoulders", ls, rs + K, None, P[rs] - 0.2 * a[rs], neck_now + depth,
                              line=(n1, Q[n1], slope)))
        else:
            if not prior_uptrend(df, ls):
                continue
            neck_now = Q[n1] + slope * (rs + K - n1)
            out.append(_setup("head_shoulders", ls, rs + K, None, P[rs] + 0.2 * a[rs], neck_now - depth,
                              side="short", line=(n1, Q[n1], slope)))
    return out


def rounding(df, window=60, side="long", with_handle=False):
    """Rounding bottom (U), cup & handle, or rounding top (inverted U) via a quadratic fit."""
    c, H, L, v, a = (df[k].values for k in ("close", "high", "low", "volume", "atr"))
    n = len(df)
    x = np.linspace(-1, 1, window)
    out, seen = [], set()
    for r in range(window, n, 2):
        y = c[r - window:r]
        if np.isnan(a[r - 1]):
            continue
        coef = np.polyfit(x, y, 2)
        fit = np.polyval(coef, x)
        ss = ((y - y.mean()) ** 2).sum()
        r2 = 1 - ((y - fit) ** 2).sum() / ss if ss > 0 else 0
        vx = -coef[1] / (2 * coef[0]) if coef[0] != 0 else 9
        if r2 < 0.7 or abs(vx) > 0.4:
            continue
        seg = window // 6
        if side == "long" and coef[0] > 0:
            rim_l = H[r - window:r - window + seg].max()
            low = L[r - window:r].min()
            depth = rim_l - low
            if depth < 3 * a[r - 1]:
                continue
            thirds = np.array_split(v[r - window:r], 3)
            if not (thirds[1].mean() < thirds[0].mean() and thirds[2].mean() > thirds[1].mean()):
                continue                                         # volume dries up at the bottom, improves on the right
            if not with_handle:
                key = ("rb", round(rim_l, 6))
                if key not in seen:
                    seen.add(key)
                    out.append(_setup("rounding_bottom", r - window, r, rim_l, max(low, rim_l - 3 * a[r - 1]),
                                      rim_l + depth, expiry=r + 10))
            else:
                right_rim = H[r - seg:r].max()
                if right_rim < rim_l - 0.25 * depth:
                    continue                                     # right side has not recovered to the rim
                hs = r + 3                                       # handle needs >= 3 candles
                if hs >= n:
                    continue
                h_low = L[r:hs + 1].min()
                if rim_l - h_low > 0.5 * depth or v[r:hs + 1].mean() > thirds[0].mean():
                    continue                                     # handle too deep or not quiet
                rim = max(rim_l, right_rim)
                key = ("ch", round(rim, 6))
                if key not in seen:
                    seen.add(key)
                    out.append(_setup("cup_and_handle", r - window, hs, rim, h_low - 0.2 * a[r - 1], rim + depth,
                                      expiry=r + 20))
        elif side == "short" and coef[0] < 0:
            rim_l = L[r - window:r - window + seg].min()
            high = H[r - window:r].max()
            height = high - rim_l
            if height < 3 * a[r - 1] or ("rt", round(rim_l, 6)) in seen:
                continue
            seen.add(("rt", round(rim_l, 6)))
            out.append(_setup("rounding_top", r - window, r, rim_l, min(high, rim_l + 3 * a[r - 1]),
                              rim_l - height, side="short", expiry=r + 10))
    return out


def v_bottom(df, hi):
    """Sharp selloff and sharp rebound — needs reclaim + BOS + volume (dead-cat risk).
    A bar is a candidate bottom only once the NEXT bar closes higher with a higher low (known one
    bar later, still causal); a straight decline never arms a setup mid-way (v6 fix)."""
    H, L, c, a = (df[k].values for k in ("high", "low", "close", "atr"))
    n = len(df)
    out, last = [], -100
    for v_ in range(15, n - 1):
        if L[v_] != L[v_ - 10:v_ + 1].min() or v_ - last < 20:
            continue
        if not (c[v_ + 1] > c[v_] and L[v_ + 1] > L[v_]):
            continue                                            # not a bottom yet
        p = v_ - 10 + int(np.argmax(H[v_ - 10:v_ + 1]))
        drop = H[p] - L[v_]
        if drop < 6 * a[v_] or v_ - p > 10:
            continue
        reclaim = L[v_] + 0.5 * drop
        out.append(_setup("v_bottom", p, v_ + 1, reclaim, L[v_] - 0.2 * a[v_], H[p], expiry=v_ + 10))
        last = v_
    return out


# ===================================================== continuation patterns
def flags(df, side="long", pole_atr=4.0):
    """Bull/bear flag, pennant and high tight flag: impulse pole + controlled consolidation."""
    H, L, c, a = (df[k].values for k in ("high", "low", "close", "atr"))
    n = len(df)
    out, used = [], -100
    for t1 in range(12, n - 3):                                # v6: was n - 4 (missed the newest pole)
        if t1 < used or np.isnan(a[t1]):
            continue
        found = False
        if side == "long":
            if H[t1] != H[t1 - 12:t1 + 1].max():
                continue
            base = L[t1 - 12:t1 + 1].min()
            pole = H[t1] - base
        else:
            if L[t1] != L[t1 - 12:t1 + 1].min():
                continue
            base = H[t1 - 12:t1 + 1].max()
            pole = base - L[t1]
        if pole < pole_atr * a[t1]:
            continue
        for t in range(t1 + 3, min(t1 + 16, n)):
            seg_h, seg_l = H[t1 + 1:t], L[t1 + 1:t]
            if len(seg_h) < 2:
                continue
            if side == "long":
                retr = (H[t1] - seg_l.min()) / pole
                if retr > 0.5 or seg_h.max() > H[t1]:
                    break
                tight = retr <= 0.25 and pole >= 8 * a[t1]
                slope_h = np.polyfit(np.arange(len(seg_h)), seg_h, 1)[0]
                slope_l = np.polyfit(np.arange(len(seg_l)), seg_l, 1)[0]
                name = "high_tight_flag" if tight else "bull_pennant" if (slope_h < 0 < slope_l) else "bull_flag"
                out.append(_setup(name, t1 - 12, t, seg_h.max(), seg_l.min() - 0.2 * a[t], seg_h.max() + pole,
                                  expiry=t))
                found = True
            else:
                retr = (seg_h.max() - L[t1]) / pole
                if retr > 0.5 or seg_l.min() < L[t1]:
                    break
                slope_h = np.polyfit(np.arange(len(seg_h)), seg_h, 1)[0]
                slope_l = np.polyfit(np.arange(len(seg_l)), seg_l, 1)[0]
                name = "bear_pennant" if (slope_h < 0 < slope_l) else "bear_flag"
                out.append(_setup(name, t1 - 12, t, seg_l.min(), seg_h.max() + 0.2 * a[t], seg_l.min() - pole,
                                  side="short", expiry=t))
                found = True
        if found:
            used = t1 + 5                                       # only a pole that produced a flag claims the area
    return out


def _fit(idx, vals):
    if len(idx) < 2:
        return None
    sl, ic = np.polyfit(idx, vals, 1)
    resid = np.abs(vals - (sl * idx + ic))
    return sl, ic, resid.max()


def converging(df, hi, lo, window=60):
    """Triangles (ascending / descending / symmetrical), wedges (rising / falling) and channels.
    Needs >= 2 touches per boundary, a good line fit, and contracting bands."""
    H, L, a, bbw = (df[k].values for k in ("high", "low", "atr", "bbw"))
    n = len(df)
    out, seen = [], set()
    events = sorted(set((hi + K).tolist() + (lo + K).tolist()))
    for t in events:
        if t >= n:
            continue
        h = hi[(hi + K <= t) & (hi >= t - window)][-3:]
        l = lo[(lo + K <= t) & (lo >= t - window)][-3:]
        if len(h) < 2 or len(l) < 2:
            continue
        fu, fl = _fit(h.astype(float), H[h]), _fit(l.astype(float), L[l])
        if fu is None or fl is None:
            continue
        su, iu, ru = fu
        sl_, il, rl = fl
        at = a[t]
        if max(ru, rl) > 0.6 * at:
            continue
        start = min(h[0], l[0])
        span = t - start
        up_move, lo_move = su * span, sl_ * span
        flat = lambda m: abs(m) < 1.0 * at
        width_start = (su * start + iu) - (sl_ * start + il)
        width_now = (su * t + iu) - (sl_ * t + il)
        if width_now <= 0 or width_start <= 0:
            continue
        contracting = width_now < width_start * 0.85 and bbw[t] <= bbw[start]
        name = None
        if contracting and flat(up_move) and lo_move > at:
            name, bias = "ascending_triangle", "long"
        elif contracting and up_move < -at and lo_move > at:
            name, bias = "symmetrical_triangle", "both"
        elif contracting and flat(lo_move) and up_move < -at:
            name, bias = "descending_triangle", "short"
        elif contracting and up_move < 0 and lo_move < 0 and su < sl_:
            name, bias = "falling_wedge", "long"
        elif contracting and up_move > 0 and lo_move > 0 and sl_ > su:
            name, bias = "rising_wedge", "short"
        elif not contracting and abs(width_now - width_start) < 0.8 * at and lo_move > at:
            name, bias = "ascending_channel", "channel"
        if name is None:
            continue
        key = (name, tuple(int(x) for x in h), tuple(int(x) for x in l))   # one setup per touch set
        if key in seen:
            continue                                            # (v6: a later touch re-forms the pattern)
        seen.add(key)
        height = width_start
        up_line, lo_line = (t, su * t + iu, su), (t, sl_ * t + il, sl_)
        if bias in ("long", "both"):
            stop = (sl_ * t + il) - 0.2 * at
            out.append(_setup(name, start, t, None, stop, (su * t + iu) + height, line=up_line))
        if bias in ("short", "both"):
            stop = (su * t + iu) + 0.2 * at
            out.append(_setup(name, start, t, None, stop, (sl_ * t + il) - height, side="short", line=lo_line))
        if bias == "channel":
            out.append({"name": name, "channel": (up_line, lo_line), "valid_from": t, "expiry": t + 20,
                        "start": start})
    return out


def range_box(df, hi, lo, window=50):
    """Rectangle / horizontal range: >= 2 touches at a flat top and a flat bottom."""
    H, L, c, a = (df[k].values for k in ("high", "low", "close", "atr"))
    n = len(df)
    boxes, seen = [], set()
    for t in sorted(set((hi + K).tolist() + (lo + K).tolist())):
        if t >= n:
            continue
        h = hi[(hi + K <= t) & (hi >= t - window)]
        l = lo[(lo + K <= t) & (lo >= t - window)]
        if len(h) < 2 or len(l) < 2:
            continue
        top, bot = H[h].max(), L[l].min()
        tol = tolerance(df, t) * top
        touches_t = (np.abs(H[h] - top) <= max(tol, 0.3 * a[t])).sum()
        touches_b = (np.abs(L[l] - bot) <= max(tol, 0.3 * a[t])).sum()
        height = top - bot
        if touches_t < 2 or touches_b < 2 or not (3 * a[t] <= height <= 14 * a[t]):
            continue
        s = min(h.min(), l.min())
        if np.any(c[s:t + 1] > top + 0.5 * a[t]) or np.any(c[s:t + 1] < bot - 0.5 * a[t]):
            continue
        key = (round(top, 10), round(bot, 10), t)               # v6: each pivot event re-arms the box
        if key in seen:
            continue
        seen.add(key)
        boxes.append({"start": s, "valid_from": t, "expiry": t + MAX_WAIT, "top": top, "bottom": bot,
                      "height": height})
    return boxes


def descending_trendline(df, hi, lo):
    """Downtrend line through the last two falling swing highs (>= 15 candles apart)."""
    H, L, c, a = (df[k].values for k in ("high", "low", "close", "atr"))
    out = []
    for j in range(1, len(hi)):
        p1, p2 = hi[j - 1], hi[j]
        if p2 - p1 < 15 or H[p1] - H[p2] < a[p2]:
            continue
        slope = (H[p2] - H[p1]) / (p2 - p1)
        line_vals = H[p1] + slope * (np.arange(p1, p2 + 1) - p1)
        if np.any(c[p1:p2 + 1] > line_vals + 0.1 * a[p2]):
            continue                                            # the line must not already be broken
        pl = lo[(lo > p1) & (lo + K <= p2 + K)]
        if len(pl) == 0:
            continue
        expiry = p2 + K + 30
        stop = L[pl[-1]] - 0.2 * a[p2]
        out.append(_setup("trendline_break", p1, p2 + K, None, stop, np.inf, line=(p1, H[p1], slope),
                          expiry=expiry))
        # v6: a lower low AFTER the second high is normal in a downtrend; each new confirmed pivot low
        # re-arms the same line with the updated invalidation (still causal: known at q + K)
        for q in lo[(lo > p2) & (lo + K <= expiry)]:
            t0 = q + K
            if t0 >= len(df):
                break
            line_q = H[p1] + slope * (np.arange(p1, t0 + 1) - p1)
            if np.any(c[p1:t0 + 1] > line_q + 0.1 * a[p2]):
                break                                           # line already broken -> later setups moot
            out.append(_setup("trendline_break", p1, t0, None, L[q] - 0.2 * a[q], np.inf,
                              line=(p1, H[p1], slope), expiry=expiry))
    return out


def failure_swing(df):
    """Bullish RSI failure swing: RSI <30, bounce to X, higher RSI low above 30, break above X."""
    r, L, a = df["rsi"].values, df["low"].values, df["atr"].values
    n = len(df)
    out, i = [], 20
    while i < n - 1:
        if r[i] < 30:
            j = i
            while j < n - 1 and r[j] < 30:
                j += 1
            peak_i, peak = j, r[j]
            k = j
            while k < min(n - 1, j + 30):
                if r[k] > peak:
                    peak_i, peak = k, r[k]
                if k > peak_i + 1 and 30 < r[k] < peak - 5:
                    m = k
                    while m < min(n - 1, k + 20) and r[m] <= peak:
                        m += 1
                    if m < n and r[m] > peak and r[m - 1] > 30:
                        low = L[i:m + 1].min()
                        out.append({"t": m, "stop": low - 0.2 * a[m]})
                    break
                k += 1
            i = k + 1
        else:
            i += 1
    return out


# ============================================================== candlesticks
def candles(df: pd.DataFrame) -> dict[str, np.ndarray]:
    o, h, l, c = (df[k].values for k in ("open", "high", "low", "close"))
    body = np.abs(c - o)
    rng = np.maximum(h - l, 1e-12)
    up_w = h - np.maximum(o, c)
    lo_w = np.minimum(o, c) - l
    bull, bear = c > o, c < o
    po, pc = np.r_[np.nan, o[:-1]], np.r_[np.nan, c[:-1]]
    ppo, ppc = np.r_[np.nan, np.nan, o[:-2]], np.r_[np.nan, np.nan, c[:-2]]
    pbody = np.abs(pc - po)
    ph, pl = np.r_[np.nan, h[:-1]], np.r_[np.nan, l[:-1]]
    trend_dn = c < pd.Series(c).rolling(10).mean().values
    trend_up = ~trend_dn
    small = body <= 0.3 * rng
    out = {
        "bullish_engulfing": bull & (pc < po) & (c >= po) & (o <= pc) & (body > pbody),
        "bearish_engulfing": bear & (pc > po) & (o >= pc) & (c <= po) & (body > pbody),
        "hammer": trend_dn & (lo_w >= 2 * body) & (up_w <= 0.3 * body + 1e-12) & (body > 0),
        "hanging_man": trend_up & (lo_w >= 2 * body) & (up_w <= 0.3 * body + 1e-12) & (body > 0),
        "inverted_hammer": trend_dn & (up_w >= 2 * body) & (lo_w <= 0.3 * body + 1e-12) & (body > 0),
        "shooting_star": trend_up & (up_w >= 2 * body) & (lo_w <= 0.3 * body + 1e-12) & (body > 0),
        "morning_star": (ppc < ppo) & (np.abs(pc - po) <= 0.3 * np.abs(ppc - ppo)) & bull & (c > (ppo + ppc) / 2),
        "evening_star": (ppc > ppo) & (np.abs(pc - po) <= 0.3 * np.abs(ppc - ppo)) & bear & (c < (ppo + ppc) / 2),
        "piercing_line": (pc < po) & bull & (o < pc) & (c > (po + pc) / 2) & (c < po),
        "dark_cloud_cover": (pc > po) & bear & (o > pc) & (c < (po + pc) / 2) & (c > po),
        "three_white_soldiers": bull & (pc > po) & (ppc > ppo) & (c > pc) & (pc > ppc),
        "three_black_crows": bear & (pc < po) & (ppc < ppo) & (c < pc) & (pc < ppc),
        "tweezer_bottom": trend_dn & (np.abs(l - pl) <= 0.001 * c) & (pc < po) & bull,
        "tweezer_top": trend_up & (np.abs(h - ph) <= 0.001 * c) & (pc > po) & bear,
        "bullish_harami": (pc < po) & bull & (o > pc) & (c < po) & (body < pbody),
        "bearish_harami": (pc > po) & bear & (o < pc) & (c > po) & (body < pbody),
        "bullish_marubozu": bull & (body >= 0.9 * rng),
        "bearish_marubozu": bear & (body >= 0.9 * rng),
        "doji": body <= 0.1 * rng,
        "spinning_top": small & (body > 0.1 * rng) & (up_w > body) & (lo_w > body),
        "inside_bar": (h < ph) & (l > pl),
    }
    return {k: np.nan_to_num(v.astype(float)).astype(bool) for k, v in out.items()}


BULL_CANDLES = ["bullish_engulfing", "hammer", "inverted_hammer", "morning_star", "piercing_line",
                "three_white_soldiers", "tweezer_bottom", "bullish_harami", "bullish_marubozu"]
BEAR_CANDLES = ["bearish_engulfing", "shooting_star", "hanging_man", "evening_star", "dark_cloud_cover",
                "three_black_crows", "tweezer_top", "bearish_harami", "bearish_marubozu"]
NEUTRAL_CANDLES = ["doji", "spinning_top", "inside_bar"]


# ================================================================ SMC helpers
def smc_setups(df, hi, lo, need_divergence=False):
    """Liquidity sweep of a swing low -> bullish BOS/CHoCH within 10 candles with an FVG ->
    order block = last bearish candle before the displacement; entry on the OB retest."""
    o, H, L, c, a, r = (df[k].values for k in ("open", "high", "low", "close", "atr", "rsi"))
    n = len(df)
    out = []
    last_hi = last_pivot_index_before(hi, n)                     # v6: precomputed instead of per-bar filtering
    struct = structure_series(df, hi, lo)
    for j in range(1, len(lo)):
        prev_low = lo[j - 1]
        for t in range(lo[j - 1] + K + 1, min(lo[j - 1] + 60, n - 1)):
            if not (L[t] < L[prev_low] and c[t] > L[prev_low]):
                continue
            if need_divergence and not (r[t] > r[prev_low]):
                break
            ph = int(last_hi[t])
            if ph < 0:
                break
            bos_level = H[ph]
            choch = struct[t] == "bearish"
            for u in range(t + 1, min(t + 11, n)):
                if c[u] > bos_level:
                    fvg = any(L[z] > H[z - 2] for z in range(max(t + 2, u - 3), u + 1))
                    if not fvg:
                        break
                    ob = next((z for z in range(u - 1, t - 1, -1) if c[z] < o[z]), None)
                    if ob is None:
                        break
                    out.append({"t": u, "ob_top": max(o[ob], c[ob]), "ob_low": L[ob], "sweep_low": L[t],
                                "choch": choch})
                    break
            break
    return out


# ============================================================ live scan + bearish research
def _short_outcome(df, ev, cost, max_bars=40):
    """Would a short have worked? Enter next open, stop above the pattern, measured-move target."""
    o, H, L, c = (df[k].values for k in ("open", "high", "low", "close"))
    t = ev["t"]
    if t + 1 >= len(df):
        return None
    px, stop, tgt = o[t + 1], ev["stop"], ev["target"]
    risk = stop - px
    if risk <= 0:
        return None
    for j in range(t + 1, min(t + 1 + max_bars, len(df))):
        if H[j] >= stop:
            return (px - max(stop, o[j])) / risk - 2 * cost * px / risk
        if L[j] <= tgt:
            return (px - min(tgt, o[j])) / risk - 2 * cost * px / risk
    return (px - c[min(t + max_bars, len(df) - 1)]) / risk - 2 * cost * px / risk


def scan(df: pd.DataFrame, cost: float) -> dict:
    hi, lo = pivots(df)
    n = len(df)
    bear_sets = (double_bottom(df, hi, lo, side="short") + double_bottom(df, hi, lo, triple=True, side="short")
                 + head_shoulders(df, hi, lo, side="short") + rounding(df, 60, side="short")
                 + flags(df, side="short") + [s for s in converging(df, hi, lo) if s.get("side") == "short"])
    _, bear_ev = trigger(df, bear_sets, vol_mult=1.2)
    research = {}
    for ev in bear_ev:
        r = _short_outcome(df, ev, cost)
        if r is None:
            continue
        research.setdefault(ev["name"], []).append(r)
    bear_table = [{"pattern": k, "events": len(v), "hit_rate": round(float(np.mean(np.array(v) > 0)), 2),
                   "avg_R_short": round(float(np.mean(v)), 2)} for k, v in sorted(research.items())]
    fresh_bear = [{"pattern": ev["name"], "bars_ago": n - 1 - ev["t"], "level": round(float(_level_at(ev, ev["t"])), 8)}
                  for ev in bear_ev if ev["t"] >= n - 3]

    bull_sets = (double_bottom(df, hi, lo) + double_bottom(df, hi, lo, triple=True) + head_shoulders(df, hi, lo)
                 + rounding(df, 60) + rounding(df, 60, with_handle=True) + flags(df)
                 + [s for s in converging(df, hi, lo) if s.get("side") == "long"]
                 + [_setup("range_breakout", b["start"], b["valid_from"], b["top"], b["bottom"] + 0.5 * b["height"],
                           b["top"] + b["height"], expiry=b["expiry"]) for b in range_box(df, hi, lo)]
                 + descending_trendline(df, hi, lo))
    c = df["close"].values
    pending = []
    for s in bull_sets:
        if s["valid_from"] <= n - 1 <= s["expiry"]:
            seg = c[s["valid_from"]:n]
            lvl_now = _level_at(s, n - 1)
            if np.all(seg > s["stop"]) and np.all(seg <= [_level_at(s, t) for t in range(s["valid_from"], n)]):
                pending.append({"pattern": s["name"], "trigger_close_above": round(float(lvl_now), 8),
                                "invalidation": round(s["stop"], 8), "expires_in_bars": s["expiry"] - (n - 1),
                                "start": int(s["start"])})
    cd = candles(df)
    recent = []
    for back in range(3):
        i = n - 1 - back
        names = [k for k, v in cd.items() if v[i]]
        if names:
            recent.append({"bars_ago": back, "candles": names})
    struct = structure(df, hi, lo, n - 1)
    return {"bearish_research": bear_table, "fresh_bearish": fresh_bear, "pending_bullish": pending[:10],
            "recent_candles": recent, "structure": struct,
            "not_implemented": ["Adam & Eve top/bottom", "Bump and run", "Inverse cup & handle",
                                "Measured-move projection (used only as targets)"]}
