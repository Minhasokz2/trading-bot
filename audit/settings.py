"""Tunable settings with safe defaults, optionally overridden by audit/settings.toml.

Only the keys present in the TOML file are overridden; everything else keeps the default below.
Modules read `CFG` at import time, so edit the file and re-run the audit."""
from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path

__version__ = "6.2.0"

DEFAULTS: dict = {
    "costs": {"fee": 0.001, "slippage": 0.0005, "maker_fee": 0.001, "futures_taker": 0.0005},
    "gates": {"min_profit_factor": 1.20, "min_trades": 20, "max_p_value": 0.10, "max_pbo": 0.50,
              "min_dsr": 0.90, "candidate_min_gates": 8, "fdr_alpha": 0.10},
    "risk": {"accepted": 0.003, "candidate": 0.0015, "rejected": 0.0, "plan_risk_per_trade": 0.01,
             "portfolio_heat": 0.015, "max_positions": 5, "max_per_cluster": 2, "cluster_corr": 0.70,
             "kelly_fraction": 0.5, "kelly_quantile": 0.20, "account_usd": 10_000.0},
    "weights": {"Liquidity": 10, "Trend (multi-timeframe)": 13, "Momentum": 6, "Volatility & risk": 5,
                "Market regime & relative strength": 9, "Order flow & volume": 6,
                "Crypto-wide & macro regime": 15, "Strategy library (validated)": 28, "ML meta-labeler": 8},
    "verdict": {"favorable": 70, "watchlist": 55, "neutral": 40, "min_volume_usd": 1_000_000},
    "market": {"universe_size": 60, "cache_hours": 12},
    "scan": {"universe": 40, "min_volume_usd": 5_000_000, "jobs": 1},
    "meta": {"min_events": 150, "conformal_alpha": 0.20, "drift_psi_warn": 0.25},
    "notify": {"min_verdict": "FAVORABLE"},
}
ROOT = Path(__file__).resolve().parent
# Where reports, logs and caches live. Locally that is the audit/ folder; when hosted (Render, Docker) point
# COIN_AUDIT_DATA_DIR at the persistent disk (e.g. /data) so nothing is lost on redeploy.
DATA_DIR = Path(os.environ.get("COIN_AUDIT_DATA_DIR") or ROOT)


def _settings_path() -> Path:
    if os.environ.get("COIN_AUDIT_SETTINGS"):
        return Path(os.environ["COIN_AUDIT_SETTINGS"])
    if (DATA_DIR / "settings.toml").exists():                  # an edited copy on the persistent disk wins
        return DATA_DIR / "settings.toml"
    return ROOT / "settings.toml"


PATH = _settings_path()


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: Path | None = None) -> dict:
    p = Path(path) if path else PATH
    if p.exists():
        with p.open("rb") as fh:
            return _merge(DEFAULTS, tomllib.load(fh))
    return copy.deepcopy(DEFAULTS)


CFG = load()
