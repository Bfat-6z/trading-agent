"""Simple risk-of-ruin and recovery pressure model for paper readiness."""
from __future__ import annotations

from math import pow
from typing import Any


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def recovery_gain_required(drawdown_fraction: float) -> float:
    if drawdown_fraction >= 1:
        return 999.0
    return drawdown_fraction / max(1e-9, 1 - drawdown_fraction)


def estimate_risk_of_ruin(win_rate: Any, avg_win: Any, avg_loss: Any, risk_fraction: Any, losing_streak: int = 0) -> dict:
    wr = min(0.999, max(0.001, safe_float(win_rate, 0.5)))
    aw = abs(safe_float(avg_win, 1.0))
    al = abs(safe_float(avg_loss, 1.0)) or 1.0
    edge = wr * aw - (1 - wr) * al
    risk = max(0.0, safe_float(risk_fraction, 0.01))
    base = (1 - wr) ** max(1, losing_streak + 3)
    penalty = 0.25 if edge <= 0 else 0.0
    risk_score = min(1.0, base + penalty + risk * 5)
    return {"edge": round(edge, 8), "risk_score": round(risk_score, 6), "status": "critical" if risk_score >= 0.5 else "warn" if risk_score >= 0.25 else "ok", "recovery_gain_after_10pct_dd": round(recovery_gain_required(0.10), 6)}
