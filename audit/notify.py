"""Optional push notifications for approved signals (v6). Read-only: it only sends text.

Configure with environment variables — nothing is stored in the repository:
  DISCORD_WEBHOOK_URL                      a Discord channel webhook
  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID    a Telegram bot and the chat to post to
Without any of them, `send` prints the message and returns [].
"""
from __future__ import annotations

import os

import requests

from settings import CFG

VERDICT_RANK = {"AVOID": 0, "NEUTRAL": 1, "WATCHLIST": 2, "FAVORABLE": 3}


def channels() -> dict:
    out = {}
    if os.environ.get("DISCORD_WEBHOOK_URL"):
        out["discord"] = os.environ["DISCORD_WEBHOOK_URL"]
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        out["telegram"] = (os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"])
    return out


def send(text: str, post=requests.post, timeout: int = 15) -> list[str]:
    """Deliver `text` to every configured channel; returns the channels that accepted it."""
    ok = []
    ch = channels()
    if not ch:
        print("  (no notification channel configured — set DISCORD_WEBHOOK_URL or TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID)")
        return ok
    if "discord" in ch:
        try:
            r = post(ch["discord"], json={"content": text[:1900]}, timeout=timeout)
            if 200 <= r.status_code < 300:
                ok.append("discord")
        except requests.RequestException as e:
            print(f"  (discord notification failed: {e})")
    if "telegram" in ch:
        token, chat = ch["telegram"]
        try:
            r = post(f"https://api.telegram.org/bot{token}/sendMessage",
                     json={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True}, timeout=timeout)
            if 200 <= r.status_code < 300:
                ok.append("telegram")
        except requests.RequestException as e:
            print(f"  (telegram notification failed: {e})")
    return ok


def should_notify(verdict: str, min_verdict: str | None = None) -> bool:
    m = (min_verdict or CFG["notify"]["min_verdict"]).upper()
    return VERDICT_RANK.get(verdict, 0) >= VERDICT_RANK.get(m, 3)


def signal_message(a: dict) -> str:
    """Compact, phone-friendly summary of one audit (used by --notify and the watch loop)."""
    p = a["plan"]
    approved = [s for s in a["signals"] if s["decision"] == "APPROVED"]
    lines = [f"{a['symbol']} {a['timeframe']}: {a['verdict']} {a['score']}/100 ({a['audit_time_utc']} UTC)"]
    reg = a.get("regime", {})
    if reg:
        lines.append(f"regime {reg.get('trend_state')}/{reg.get('vol_state')} vol, BTC {reg.get('btc')}")
    mr = a.get("market_regime")
    if mr:
        lines.append(f"market: {mr['regime']}, alt score {mr['alt_score']:.0f}")
    for s in approved:
        lines.append(f"APPROVED {s['strategy_id']} conf {s['confidence']:.0%} risk {s['max_risk_fraction']:.2%}")
    lines.append(f"entry {p['entry_low']:.6g}-{p['entry_high']:.6g} stop {p['stop']:.6g} "
                 f"T1 {p['target1']:.6g} T2 {p['target2']:.6g}")
    for f in a.get("flags", [])[:3]:
        lines.append("! " + f)
    lines.append("research tool, not financial advice")
    return "\n".join(lines)
