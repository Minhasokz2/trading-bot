"""Static HTML dashboard of the latest audit per coin (v6). No server, no JavaScript dependencies:
open audit/reports/dashboard.html in a browser. Rebuilt by `--dashboard` and by the watch loop.

Design: stat tiles for the headline counts, one table row per coin/timeframe with the verdict as
icon + label (never colour alone), the score as a meter on a same-hue track, a 12-point score
sparkline, and links to the full reports. Light and dark themes from the same tokens."""
from __future__ import annotations

import csv
import html
import json
from datetime import datetime, timezone
from pathlib import Path

VERDICT = {"FAVORABLE": ("good", "✔"), "WATCHLIST": ("warning", "◐"), "NEUTRAL": ("muted", "○"),
           "AVOID": ("critical", "✖")}

CSS = """
:root { color-scheme: light; --surface:#fcfcfb; --plane:#f9f9f7; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --border:rgba(11,11,11,.10); --accent:#2a78d6; --track:#cde2fb; --good:#0ca30c; --warning:#fab219;
  --critical:#d03b3b; --up:#006300; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { color-scheme: dark; --surface:#1a1a19;
  --plane:#0d0d0d; --ink:#ffffff; --ink2:#c3c2b7; --muted:#898781; --grid:#2c2c2a; --border:rgba(255,255,255,.10);
  --accent:#3987e5; --track:#0d366b; --up:#0ca30c; } }
:root[data-theme="dark"] { color-scheme: dark; --surface:#1a1a19; --plane:#0d0d0d; --ink:#ffffff; --ink2:#c3c2b7;
  --muted:#898781; --grid:#2c2c2a; --border:rgba(255,255,255,.10); --accent:#3987e5; --track:#0d366b; --up:#0ca30c; }
* { box-sizing: border-box; }
body { margin:0; padding:24px 16px; background:var(--plane); color:var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif; font-size:14px; line-height:1.45; }
h1 { font-size:20px; margin:0 0 4px; } h2 { font-size:16px; margin:28px 0 8px; }
.sub { color:var(--ink2); margin:0 0 16px; }
.tiles { display:grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap:12px; }
.tile { background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:12px 14px; }
.tile .label { color:var(--ink2); font-size:12px; } .tile .value { font-size:26px; font-weight:600; margin-top:2px; }
.tile .delta { font-size:12px; color:var(--ink2); }
.wrap { overflow-x:auto; background:var(--surface); border:1px solid var(--border); border-radius:8px; }
table { border-collapse:collapse; width:100%; min-width:900px; }
th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--grid); vertical-align:middle; }
th { color:var(--ink2); font-weight:600; font-size:12px; }
td.num { font-variant-numeric: tabular-nums; text-align:right; }
.v { display:inline-flex; align-items:center; gap:6px; font-weight:600; }
.v .dot { width:10px; height:10px; border-radius:50%; display:inline-block; border:2px solid var(--surface); box-shadow:0 0 0 1px var(--border); }
.v.good .dot { background:var(--good); } .v.warning .dot { background:var(--warning); }
.v.critical .dot { background:var(--critical); } .v.muted .dot { background:var(--muted); }
.meter { position:relative; width:110px; height:8px; background:var(--track); border-radius:4px; display:inline-block; vertical-align:middle; margin-right:8px; }
.meter i { position:absolute; left:0; top:0; bottom:0; background:var(--accent); border-radius:4px; }
.spark { vertical-align:middle; }
.sig { color:var(--up); font-weight:600; } .flag { color:var(--ink2); font-size:12px; }
a { color:var(--accent); text-decoration:none; } a:hover { text-decoration:underline; }
.foot { color:var(--muted); font-size:12px; margin-top:20px; }
@media (max-width:600px) { body { padding:16px 16px; } .tile .value { font-size:22px; } }
"""


def _latest_reports(reports_dir: Path) -> list[dict]:
    latest: dict = {}
    for p in sorted(reports_dir.glob("*_*_*.json")):
        try:
            a = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if "symbol" not in a or "timeframe" not in a:
            continue
        key = (a["symbol"], a["timeframe"])
        if key not in latest or a.get("audit_time_utc", "") >= latest[key]["audit_time_utc"]:
            a["_md"] = p.with_suffix(".md").name
            latest[key] = a
    return sorted(latest.values(), key=lambda a: (-a.get("score", 0), a["symbol"]))


def _history(audits_csv: Path) -> dict:
    hist: dict = {}
    if not audits_csv.exists():
        return hist
    for r in csv.DictReader(audits_csv.open(encoding="utf-8")):
        try:
            hist.setdefault((r["symbol"], r["timeframe"]), []).append(
                (r["audit_time_utc"], float(r["score"]), r.get("outcome", ""), r.get("outcome_return_pct", "")))
        except (KeyError, ValueError):
            continue
    return hist


def sparkline(values: list[float], w: int = 84, h: int = 22) -> str:
    """Inline SVG: 12 points in the muted ink, the current point in the accent."""
    v = values[-12:]
    if len(v) < 2:
        return ""
    lo, hi = min(v), max(v)
    span = (hi - lo) or 1.0
    pts = [(2 + i * (w - 4) / (len(v) - 1), h - 2 - (x - lo) / span * (h - 4)) for i, x in enumerate(v)]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    cx, cy = pts[-1]
    return (f'<svg class="spark" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="score history">'
            f'<title>last {len(v)} scores: {", ".join(f"{x:.0f}" for x in v)}</title>'
            f'<polyline fill="none" stroke="#898781" stroke-width="2" stroke-linejoin="round" points="{poly}"/>'
            f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3" fill="var(--accent)"/></svg>')


def _tile(label: str, value, delta: str = "") -> str:
    return (f'<div class="tile"><div class="label">{html.escape(label)}</div><div class="value">{html.escape(str(value))}</div>'
            f'<div class="delta">{html.escape(delta)}</div></div>')


def build(reports_dir: Path, audits_csv: Path, signals_csv: Path | None = None, out: Path | None = None) -> Path:
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    out = out or reports_dir / "dashboard.html"
    latest = _latest_reports(reports_dir)
    hist = _history(Path(audits_csv))
    graded = [h for rows in hist.values() for h in rows if h[2]]
    wins = sum(1 for h in graded if h[3] not in ("",) and float(h[3]) > 0)
    approved = sum(1 for a in latest for s in a.get("signals", []) if s.get("decision") == "APPROVED")
    counts = {k: sum(1 for a in latest if a.get("verdict") == k) for k in VERDICT}
    scans = sorted(reports_dir.glob("scan_*.md"))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    tiles = [_tile("Coins audited", len(latest), "latest audit per coin and timeframe"),
             _tile("Favorable", counts["FAVORABLE"], f"{counts['WATCHLIST']} watchlist · {counts['NEUTRAL']} neutral · {counts['AVOID']} avoid"),
             _tile("Approved signals", approved, "validated, regime-cleared, on the last closed candle"),
             _tile("Graded audits", len(graded), f"{wins / len(graded):.0%} positive at exit" if graded else "run --review to grade")]
    rows = []
    for a in latest:
        cls, icon = VERDICT.get(a.get("verdict", ""), ("muted", "○"))
        reg, mr, p = a.get("regime", {}), a.get("market_regime") or {}, a.get("plan", {})
        sig = [s["strategy_id"] for s in a.get("signals", []) if s.get("decision") == "APPROVED"]
        acc = sum(1 for r in a.get("strategies", []) if r.get("status") == "ACCEPTED")
        cand = sum(1 for r in a.get("strategies", []) if r.get("status") == "CANDIDATE")
        scores = [h[1] for h in sorted(hist.get((a["symbol"], a["timeframe"]), []))]
        flags = "; ".join(f[:60] for f in a.get("flags", [])[:2])
        alt_txt = f" · alt {float(mr.get('alt_score') or 0):.0f}" if mr else ""
        rows.append(
            f'<tr><td><a href="{html.escape(a["_md"])}">{html.escape(a["symbol"])}</a> <span class="flag">{a["timeframe"]}</span></td>'
            f'<td>{html.escape(a.get("audit_time_utc", ""))}</td>'
            f'<td><span class="v {cls}"><span class="dot"></span>{icon} {html.escape(a.get("verdict", ""))}</span></td>'
            f'<td class="num"><span class="meter" title="score {a.get("score", 0)}/100"><i style="width:{max(0, min(100, a.get("score", 0)))}%"></i></span>{a.get("score", 0):.0f}</td>'
            f'<td>{sparkline(scores)}</td>'
            f'<td>{html.escape(str(reg.get("trend_state", "")))}/{html.escape(str(reg.get("vol_state", "")))} vol · BTC {html.escape(str(reg.get("btc", "")))}</td>'
            f'<td>{html.escape(mr.get("regime", "n/a"))}{alt_txt}</td>'
            f'<td class="num">{acc} / {cand}</td>'
            f'<td>{"<span class=sig>" + html.escape(", ".join(sig)) + "</span>" if sig else "—"}</td>'
            f'<td class="num">{p.get("entry_low", 0):.6g}–{p.get("entry_high", 0):.6g}<br><span class="flag">stop {p.get("stop", 0):.6g} · T1 {p.get("target1", 0):.6g}</span></td>'
            f'<td class="flag">{html.escape(flags)}</td></tr>')
    table = ("<div class=wrap><table><thead><tr><th>Coin</th><th>Audited (UTC)</th><th>Verdict</th><th>Score</th>"
             "<th>Trend</th><th>Coin regime</th><th>Market regime</th><th>Accepted / cand.</th><th>Approved signals</th>"
             "<th>Plan (entry · stop · T1)</th><th>Flags</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")
    scan_html = ""
    if scans:
        scan_html = "<h2>Market scans</h2><ul>" + "".join(
            f'<li><a href="{html.escape(s.name)}">{html.escape(s.stem)}</a></li>' for s in scans[-5:][::-1]) + "</ul>"
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Coin audit dashboard</title><style>{CSS}</style></head><body>
<h1>Coin audit dashboard</h1><p class="sub">Latest audit per coin · rebuilt {now} UTC · read-only research, not financial advice</p>
<div class="tiles">{''.join(tiles)}</div>
<h2>Latest audits</h2>{table if rows else '<p class="sub">No reports yet — run an audit first.</p>'}
{scan_html}
<p class="foot">Verdict thresholds: FAVORABLE ≥ 70 · WATCHLIST 55–70 · NEUTRAL 40–55 · AVOID &lt; 40. Score meter = audit score / 100. Sparkline = the last 12 audit scores of that coin.</p>
</body></html>"""
    out.write_text(doc, encoding="utf-8")
    return out
