"""Paper capital allocation policy from setup rankings."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
ALLOCATION_LATEST = ROOT / "state" / "agent_memory" / "capital_allocation_latest.json"
MIN_REDUCED_PAPER_RISK_FRACTION = 0.02
MAX_PAPER_RISK_FRACTION = 0.05

TIER_RISK_BOUNDS = {
    "exploration_paper": (0.015, 0.035),
    "reduced_paper": (MIN_REDUCED_PAPER_RISK_FRACTION, 0.04),
    "tiny_paper": (0.01, 0.025),
    "normal_paper": (0.03, MAX_PAPER_RISK_FRACTION),
}


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def adaptive_paper_risk_fraction(row: dict[str, Any], account: dict[str, Any], tier: str, multiplier: float) -> tuple[float, dict[str, Any]]:
    low, high = TIER_RISK_BOUNDS.get(tier, (0.0, 0.0))
    if high <= 0:
        return 0.0, {"mode": "blocked"}
    equity = max(0.0, safe_float(account.get("equity"), 100.0))
    open_margin = max(0.0, safe_float(account.get("open_margin")))
    open_margin_pct = open_margin / equity if equity > 0 else 0.0
    evidence_expectancy = safe_float(row.get("evidence_expectancy"), safe_float(row.get("expectancy")))
    rank_score = safe_float(row.get("rank_score"))
    sample_confidence = clamp(safe_float(row.get("sample_confidence"), 0.5), 0.0, 1.0)
    bad_loss_rate = clamp(safe_float(row.get("bad_loss_rate")), 0.0, 1.0)
    instability_rate = clamp(safe_float(row.get("parameter_instability_rate")), 0.0, 1.0)
    base = {
        "normal_paper": 0.0352,
        "exploration_paper": 0.025,
        "reduced_paper": 0.025,
        "tiny_paper": 0.015,
    }.get(tier, 0.0)
    edge_boost = clamp(evidence_expectancy, 0.0, 0.07) * 0.12
    rank_boost = clamp(rank_score, 0.0, 2.0) * 0.002
    confidence_boost = max(0.0, sample_confidence - 0.5) * 0.005
    multiplier_penalty = max(0.0, 1.0 - multiplier) * 0.002
    quality_penalty = bad_loss_rate * 0.003 + instability_rate * 0.002
    exposure_penalty = 0.0
    if open_margin_pct >= 0.70:
        exposure_penalty = 0.03
    elif open_margin_pct >= 0.55:
        exposure_penalty = 0.02
    elif open_margin_pct >= 0.40:
        exposure_penalty = 0.01
    raw = base + edge_boost + rank_boost + confidence_boost - multiplier_penalty - quality_penalty - exposure_penalty
    risk_fraction = clamp(raw, low, high)
    risk_fraction = min(risk_fraction, MAX_PAPER_RISK_FRACTION)
    return risk_fraction, {
        "mode": "adaptive_paper",
        "tier_floor": low,
        "tier_cap": high,
        "base": round(base, 6),
        "edge_boost": round(edge_boost, 6),
        "rank_boost": round(rank_boost, 6),
        "confidence_boost": round(confidence_boost, 6),
        "multiplier_penalty": round(multiplier_penalty, 6),
        "quality_penalty": round(quality_penalty, 6),
        "exposure_penalty": round(exposure_penalty, 6),
        "open_margin_pct": round(open_margin_pct, 6),
        "raw_risk_fraction": round(raw, 6),
    }


def allocate_capital(setup_id: str, rankings: list[dict[str, Any]], account: dict[str, Any], exploration_allowed: bool = False, output_path: Path = ALLOCATION_LATEST) -> dict[str, Any]:
    equity = safe_float(account.get("equity"), 100.0)
    lookup = {str(row.get("setup_id")): row for row in rankings}
    row = lookup.get(setup_id)
    errors = []
    tier = "shadow_only"
    risk_fraction = 0.0
    sizing_factors: dict[str, Any] = {"mode": "blocked"}
    if not row:
        errors.append("setup_not_ranked")
    else:
        if row.get("paper_only_retired"):
            errors.append("setup_paper_only_retired")
        under_sampled = bool(row.get("under_sampled"))
        if under_sampled and not exploration_allowed:
            errors.append("setup_under_sampled")
        evidence_expectancy = safe_float(row.get("evidence_expectancy"), safe_float(row.get("expectancy")))
        if evidence_expectancy <= 0:
            errors.append("non_positive_expectancy")
        if not errors and under_sampled and exploration_allowed:
            tier = "exploration_paper"
            risk_fraction, sizing_factors = adaptive_paper_risk_fraction(row, account, tier, 1.0)
        elif not errors:
            rank_score = safe_float(row.get("rank_score"))
            hint = str(row.get("allocation_hint") or "")
            multiplier = max(0.0, min(1.0, safe_float(row.get("risk_multiplier"), 1.0)))
            if hint == "skip":
                errors.append("allocation_hint_skip")
            elif hint == "reduced":
                tier = "reduced_paper"
                risk_fraction, sizing_factors = adaptive_paper_risk_fraction(row, account, tier, multiplier)
            else:
                tier = "normal_paper" if rank_score >= 1.0 and hint == "normal" else "tiny_paper"
                risk_fraction, sizing_factors = adaptive_paper_risk_fraction(row, account, tier, multiplier or 1.0)
    if errors == ["setup_under_sampled"] and exploration_allowed and row and safe_float(row.get("evidence_expectancy"), safe_float(row.get("expectancy"))) >= 0:
        tier = "exploration_paper"
        risk_fraction, sizing_factors = adaptive_paper_risk_fraction(row, account, tier, 1.0)
        errors = []
    payload = {"schema_version": SCHEMA_VERSION, "allocated_at": utc_now(), "setup_id": setup_id, "allowed": not errors, "tier": tier, "risk_fraction": round(risk_fraction, 6), "max_loss_usdt": round(equity * risk_fraction, 6), "errors": errors, "rank_reasons": (row or {}).get("rank_reasons", []), "allocation_hint": (row or {}).get("allocation_hint"), "sizing_mode": sizing_factors.get("mode"), "sizing_factors": sizing_factors, "can_trade_live": False}
    write_json_atomic(output_path, payload)
    return payload
