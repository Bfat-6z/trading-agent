"""Autonomous paper decision brain foundation.

This module chooses paper-only actions from prepared candidates. It does not
fetch markets, place exchange orders, or enable live execution.
"""
from __future__ import annotations

from math import floor
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, write_json_atomic
from capital_allocation_policy import allocate_capital
from dont_do_memory import evaluate_candidate
from host_runtime_monitor import paper_opens_paused_by_runtime
from live_permission_firewall import paper_action_allowed
from memory_retrieval import active_recall_for_decision
from paper_portfolio_manager import DEFAULT_MAX_RISK_FRACTION, evaluate_paper_order
from preflight_guard import run_preflight
from setup_ranker import rank_setups
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
BRAIN_LATEST = MEMORY_DIR / "paper_trading_brain_latest.json"
BRAIN_HISTORY = MEMORY_DIR / "paper_trading_brain_history.jsonl"
PAPER_RISK_STATE = MEMORY_DIR / "paper_risk_state.json"
MAX_PAPER_MARGIN_FRACTION = 0.45
MIN_ADAPTIVE_LEVERAGE = 3.0
MAX_ADAPTIVE_LEVERAGE = 50.0
TAKER_FEE_RATE = 0.0005
MAINTENANCE_MARGIN_RATE = 0.005
MAX_DAILY_LOSS_FRACTION = 0.05
MAX_DRAWDOWN_FRACTION = 0.10
LIQUIDATION_DISTANCE_FLOOR = 0.005
MAX_STRESS_LOSS_MULTIPLIER = 1.5

TIER_LEVERAGE_CAPS = {
    "tiny_paper": 8.0,
    "exploration_paper": 15.0,
    "reduced_paper": 25.0,
    "normal_paper": MAX_ADAPTIVE_LEVERAGE,
}


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def stop_distance_fraction(side: str, entry: Any, sl: Any) -> float:
    entry_f = safe_float(entry)
    sl_f = safe_float(sl)
    side_up = str(side or "").upper()
    if entry_f <= 0 or sl_f <= 0:
        return 0.0
    if side_up == "LONG":
        return max(0.0, (entry_f - sl_f) / entry_f)
    if side_up == "SHORT":
        return max(0.0, (sl_f - entry_f) / entry_f)
    return 0.0

def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))

def adaptive_paper_leverage(candidate: dict[str, Any], allocation: dict[str, Any], account: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    tier = str(allocation.get("tier") or "")
    tier_cap = TIER_LEVERAGE_CAPS.get(tier, 5.0)
    patch_cap = safe_float(candidate.get("paper_only_leverage_cap"), 0.0)
    if patch_cap > 0:
        tier_cap = min(tier_cap, patch_cap)
    max_risk_fraction = safe_float(DEFAULT_MAX_RISK_FRACTION, 0.05)
    risk_fraction = clamp(safe_float(allocation.get("risk_fraction")), 0.0, max_risk_fraction)
    risk_norm = risk_fraction / max_risk_fraction if risk_fraction > 0 and max_risk_fraction > 0 else 0.0
    score = safe_float(candidate.get("score"), safe_float(candidate.get("setup_score")))
    score_norm = clamp((score - 7.0) / 3.0, 0.0, 1.0)
    stop_fraction = stop_distance_fraction(str(candidate.get("side") or ""), candidate.get("entry"), candidate.get("sl"))
    if stop_fraction <= 0:
        stop_boost = -2.0
    elif stop_fraction <= 0.015:
        stop_boost = 4.0
    elif stop_fraction <= 0.025:
        stop_boost = 2.0
    elif stop_fraction <= 0.04:
        stop_boost = 0.0
    else:
        stop_boost = -2.0
    equity = max(0.0, safe_float(account.get("equity"), 100.0))
    open_margin = max(0.0, safe_float(account.get("open_margin")))
    open_margin_pct = open_margin / equity if equity > 0 else 0.0
    exposure_penalty = 3.0 if open_margin_pct >= 0.70 else 2.0 if open_margin_pct >= 0.55 else 1.0 if open_margin_pct >= 0.40 else 0.0
    requested = safe_float(candidate.get("leverage"), 0.0)
    high_conviction_boost = 0.0
    if tier == "normal_paper" and risk_norm >= 0.95 and score_norm >= 0.9 and 0 < stop_fraction <= 0.015 and open_margin_pct < 0.35:
        high_conviction_boost = 21.0
    base = 3.0 + risk_norm * 12.0 + score_norm * 10.0 + stop_boost + high_conviction_boost - exposure_penalty
    if requested > 0:
        base = max(base, min(requested, tier_cap))
    leverage_floor = min(MIN_ADAPTIVE_LEVERAGE, tier_cap)
    leverage = clamp(round(base), leverage_floor, tier_cap)
    leverage = min(leverage, MAX_ADAPTIVE_LEVERAGE)
    return leverage, {
        "mode": "adaptive_paper_leverage",
        "tier_cap": tier_cap,
        "paper_only_leverage_cap": patch_cap or None,
        "risk_norm": round(risk_norm, 6),
        "score_norm": round(score_norm, 6),
        "stop_distance_fraction": round(stop_fraction, 8),
        "stop_boost": stop_boost,
        "open_margin_pct": round(open_margin_pct, 6),
        "exposure_penalty": exposure_penalty,
        "high_conviction_boost": high_conviction_boost,
        "candidate_requested_leverage": requested or None,
        "selected_leverage": leverage,
    }


def futures_margin_from_risk_budget(candidate: dict[str, Any], allocation: dict[str, Any], account: dict[str, Any]) -> float:
    """Convert risk budget at SL into Binance-futures-style isolated margin."""
    equity = max(0.0, safe_float(account.get("equity"), 100.0))
    cash = max(0.0, safe_float(account.get("cash"), equity))
    risk_budget = max(0.0, safe_float(allocation.get("max_loss_usdt")))
    leverage = max(1.0, safe_float(candidate.get("leverage"), 1.0))
    risk_distance = stop_distance_fraction(str(candidate.get("side") or ""), candidate.get("entry"), candidate.get("sl"))
    if risk_budget <= 0 or risk_distance <= 0:
        return 0.0
    hard_risk_cap = equity * safe_float(DEFAULT_MAX_RISK_FRACTION, 0.02)
    usable_risk_budget = min(risk_budget, hard_risk_cap)
    raw_margin = usable_risk_budget / (risk_distance * leverage)
    max_margin = min(cash, equity * MAX_PAPER_MARGIN_FRACTION)
    capped = max(0.0, min(raw_margin, max_margin))
    return floor(capped * 1_000_000) / 1_000_000

def liquidation_distance_fraction(entry: Any, side: str, leverage: Any) -> float:
    entry_f = safe_float(entry)
    lev = max(1.0, safe_float(leverage, 1.0))
    if entry_f <= 0:
        return 0.0
    move = max(0.0, (1.0 / lev) - MAINTENANCE_MARGIN_RATE)
    return move

def account_loss_breaker(account: dict[str, Any]) -> dict[str, Any]:
    equity = max(0.0, safe_float(account.get("equity"), 100.0))
    starting = max(equity, safe_float(account.get("starting_equity"), 100.0))
    daily_loss = safe_float(account.get("daily_loss_usdt"), 0.0)
    if daily_loss == 0.0:
        realized = safe_float(account.get("realized_pnl"), 0.0)
        daily_loss = min(0.0, realized)
    drawdown = max(0.0, (starting - equity) / starting) if starting > 0 else 0.0
    errors: list[str] = []
    if abs(min(0.0, daily_loss)) >= max(1.0, starting * MAX_DAILY_LOSS_FRACTION):
        errors.append("daily_loss_breaker_active")
    if drawdown >= MAX_DRAWDOWN_FRACTION:
        errors.append("drawdown_throttle_active")
    return {
        "daily_loss_usdt": round(daily_loss, 8),
        "daily_loss_limit_usdt": round(max(1.0, starting * MAX_DAILY_LOSS_FRACTION), 8),
        "drawdown_fraction": round(drawdown, 8),
        "drawdown_limit_fraction": MAX_DRAWDOWN_FRACTION,
        "errors": errors,
    }

def build_paper_sizing(
    candidate: dict[str, Any],
    allocation: dict[str, Any],
    account: dict[str, Any],
    requested_margin: float,
    requested_leverage: float,
    leverage_factors: dict[str, Any],
    risk: dict[str, Any] | None = None,
    preflight: dict[str, Any] | None = None,
) -> dict[str, Any]:
    risk = risk or {}
    equity = max(0.0, safe_float(account.get("equity"), 100.0))
    max_loss = max(0.0, min(safe_float(allocation.get("max_loss_usdt")), equity * safe_float(DEFAULT_MAX_RISK_FRACTION, 0.05)))
    margin = safe_float(risk.get("margin"), requested_margin)
    leverage = safe_float(risk.get("leverage"), requested_leverage)
    notional = safe_float(risk.get("notional"), margin * leverage)
    stop_fraction = stop_distance_fraction(str(candidate.get("side") or ""), candidate.get("entry"), candidate.get("sl"))
    risk_at_stop = safe_float(risk.get("estimated_loss"), notional * stop_fraction)
    fee_to_close_reserve = safe_float(risk.get("fee_to_close_reserve"), abs(notional) * TAKER_FEE_RATE)
    funding_pct = safe_float((candidate.get("market_features") or {}).get("funding_pct")) if isinstance(candidate.get("market_features"), dict) else 0.0
    funding_reserve = abs(notional) * max(0.0005, abs(funding_pct) / 100.0)
    gap_loss_estimate = risk_at_stop * 0.25
    stress_loss = risk_at_stop + fee_to_close_reserve + funding_reserve + gap_loss_estimate
    liq_distance = liquidation_distance_fraction(candidate.get("entry"), str(candidate.get("side") or ""), leverage)
    breaker = account_loss_breaker(account)
    policy = {
        "policy_id": "paper_risk_policy_v1",
        "max_risk_fraction": safe_float(DEFAULT_MAX_RISK_FRACTION, 0.05),
        "max_daily_loss_fraction": MAX_DAILY_LOSS_FRACTION,
        "max_drawdown_fraction": MAX_DRAWDOWN_FRACTION,
        "max_margin_fraction": MAX_PAPER_MARGIN_FRACTION,
        "max_leverage": MAX_ADAPTIVE_LEVERAGE,
        "liquidation_distance_floor": LIQUIDATION_DISTANCE_FLOOR,
        "stress_loss_multiplier": MAX_STRESS_LOSS_MULTIPLIER,
        "paper_only_high_leverage": True,
    }
    errors: list[str] = list(breaker["errors"])
    warnings: list[str] = []
    required_liq_distance = max(LIQUIDATION_DISTANCE_FLOOR, stop_fraction * 1.05)
    if leverage > 25 and liq_distance < required_liq_distance:
        errors.append("liquidation_distance_inside_stop_risk")
    if max_loss > 0 and stress_loss > max_loss * MAX_STRESS_LOSS_MULTIPLIER:
        errors.append("stress_loss_above_policy")
    instrument = (preflight or {}).get("instrument") if isinstance((preflight or {}).get("instrument"), dict) else {}
    if instrument and instrument.get("errors"):
        errors.append("sizing_requires_fresh_instrument")
    open_positions = [row for row in account.get("open_positions", []) if isinstance(row, dict)] if isinstance(account.get("open_positions"), list) else []
    same_symbol_notional = sum(safe_float(row.get("notional"), safe_float(row.get("margin")) * safe_float(row.get("leverage"), 1.0)) for row in open_positions if str(row.get("symbol") or "").upper() == str(candidate.get("symbol") or "").upper())
    if same_symbol_notional > 0:
        warnings.append("same_symbol_exposure_present")
    return {
        "method": "risk_budget_to_isolated_margin",
        "risk_policy_id": policy["policy_id"],
        "risk_budget_usdt": allocation.get("max_loss_usdt"),
        "usable_risk_budget_usdt": round(max_loss, 8),
        "requested_margin": requested_margin,
        "requested_leverage": requested_leverage,
        "initial_margin": round(margin, 8),
        "maintenance_margin": round(abs(notional) * MAINTENANCE_MARGIN_RATE, 8),
        "fee_to_close_reserve": round(fee_to_close_reserve, 8),
        "risk_at_stop": round(risk_at_stop, 8),
        "funding_reserve": round(funding_reserve, 8),
        "gap_loss_estimate": round(gap_loss_estimate, 8),
        "stress_estimated_loss": round(stress_loss, 8),
        "liquidation_distance_fraction": round(liq_distance, 8),
        "required_liquidation_distance_fraction": round(required_liq_distance, 8),
        "leverage_factors": leverage_factors,
        "stop_distance_fraction": round(stop_fraction, 8),
        "max_margin_fraction": MAX_PAPER_MARGIN_FRACTION,
        "account_breaker": breaker,
        "same_symbol_open_notional": round(same_symbol_notional, 8),
        "policy": policy,
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
    }


def candidate_route_score(candidate: dict[str, Any], setup_row: dict[str, Any] | None = None) -> float:
    score = safe_float(candidate.get("score") or candidate.get("setup_score"))
    row = setup_row or {}
    if not row:
        return score - 1.0
    hint = str(row.get("allocation_hint") or "")
    evidence_expectancy = safe_float(row.get("evidence_expectancy"), safe_float(row.get("expectancy")))
    rank_score = safe_float(row.get("rank_score"))
    if row.get("paper_only_retired"):
        return score - 100.0
    score += min(2.0, max(-2.0, rank_score / 2.0))
    if hint == "normal" and evidence_expectancy > 0:
        score += 2.0
    elif hint == "tiny" and evidence_expectancy >= 0:
        score += 0.75
    elif hint == "reduced" and evidence_expectancy > 0:
        score += 0.25
    if evidence_expectancy <= 0:
        score -= 3.0
    if "non_positive_evidence_expectancy" in (row.get("rank_reasons") or []):
        score -= 2.0
    if safe_float(row.get("paper_only_min_score_adjustment")) > 0:
        score -= 1.0
    return score

def choose_candidate(candidates: list[dict[str, Any]], rankings: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    ranked = rank_candidate_list(candidates, rankings)
    return ranked[0] if ranked else None


def rank_candidate_list(candidates: list[dict[str, Any]], rankings: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    eligible = [row for row in candidates if row.get("symbol") and row.get("side") and row.get("setup_id")]
    lookup = {str(row.get("setup_id")): row for row in (rankings or []) if isinstance(row, dict)}
    eligible.sort(key=lambda row: candidate_route_score(row, lookup.get(str(row.get("setup_id") or ""))), reverse=True)
    return eligible


def attach_setup_contract(candidate: dict[str, Any], rankings: list[dict[str, Any]]) -> dict[str, Any]:
    row = next((item for item in rankings if str(item.get("setup_id")) == str(candidate.get("setup_id"))), {})
    fields = {
        "setup_version": row.get("setup_version"),
        "setup_contract_id": row.get("setup_contract_id"),
        "setup_contract_hash": row.get("setup_contract_hash"),
        "setup_quality_tier": row.get("setup_quality_tier"),
        "matcher_version": row.get("matcher_version"),
        "ranker_version": row.get("ranker_version"),
        "risk_version": row.get("risk_version"),
        "paper_only_leverage_cap": row.get("paper_only_leverage_cap"),
        "paper_only_min_score_adjustment": row.get("paper_only_min_score_adjustment"),
    }
    return {**candidate, **{key: value for key, value in fields.items() if value not in (None, "")}}


def paper_only_patch_errors(candidate: dict[str, Any], rankings: list[dict[str, Any]]) -> list[str]:
    setup_id = str(candidate.get("setup_id") or "")
    row = next((item for item in rankings if str(item.get("setup_id")) == setup_id), {})
    errors: list[str] = []
    if row.get("paper_only_retired"):
        errors.append("setup_paper_only_retired")
    min_delta = safe_float(row.get("paper_only_min_score_adjustment"))
    if min_delta > 0:
        candidate_score = safe_float(candidate.get("score") or candidate.get("setup_score"))
        if candidate_score < 8.0 + min_delta:
            errors.append("skill_patch_min_score_block")
    return errors


def open_position_conflict(candidate: dict[str, Any], account: dict[str, Any]) -> bool:
    positions = account.get("open_positions") if isinstance(account.get("open_positions"), list) else []
    for position in positions:
        if not isinstance(position, dict):
            continue
        same_symbol = str(position.get("symbol") or "").upper() == str(candidate.get("symbol") or "").upper()
        same_side = str(position.get("side") or "").upper() == str(candidate.get("side") or "").upper()
        same_setup = str(position.get("setup_id") or "") == str(candidate.get("setup_id") or "")
        if same_symbol and same_side and same_setup:
            return True
    return False


def candidate_feature_errors(candidate: dict[str, Any]) -> list[str]:
    producer = str(candidate.get("producer_id") or candidate.get("source") or "")
    runtime_candidate = producer == "paper_candidate_feeder" or bool(candidate.get("market_snapshot_ts"))
    if not runtime_candidate:
        return []
    errors: list[str] = []
    feature_id = candidate.get("feature_id")
    if not feature_id:
        errors.append("missing_feature_row_id")
    if candidate.get("feature_status") == "quarantined" or candidate.get("provenance_status") == "quarantined":
        errors.append("feature_row_quarantined")
    mask = candidate.get("decision_data_capability_mask") if isinstance(candidate.get("decision_data_capability_mask"), dict) else {}
    action = str(mask.get("action") or "")
    if action in {"skip", "shadow_only"}:
        errors.append(f"capability_mask_{action}")
    cutoff = candidate.get("feature_cutoff_proof") if isinstance(candidate.get("feature_cutoff_proof"), dict) else {}
    if cutoff and not cutoff.get("ok", False):
        errors.append("feature_cutoff_proof_failed")
    return sorted(set(errors))


def apply_capability_size_cap(allocation: dict[str, Any], candidate: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
    mask = candidate.get("decision_data_capability_mask") if isinstance(candidate.get("decision_data_capability_mask"), dict) else {}
    if mask.get("action") != "size_cap":
        return allocation
    equity = max(0.0, safe_float(account.get("equity"), 100.0))
    capped = dict(allocation)
    capped["capability_action"] = "size_cap"
    capped["capability_mask"] = mask
    capped["risk_fraction"] = min(safe_float(capped.get("risk_fraction")), 0.02)
    capped["max_loss_usdt"] = min(safe_float(capped.get("max_loss_usdt")), round(equity * capped["risk_fraction"], 8))
    capped.setdefault("warnings", [])
    capped["warnings"] = list(capped.get("warnings") or []) + ["capability_mask_size_cap"]
    return capped

def skipped_risk_state(reason: str, errors: list[str] | None = None, attempts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "evaluated_at": utc_now(),
        "mode": "paper",
        "can_open_paper": False,
        "can_place_live_orders": False,
        "reason": reason,
        "errors": sorted(set(errors or [])),
        "paper_sizing": {"method": "not_evaluated", "errors": sorted(set(errors or []))},
        "candidate_attempts": (attempts or [])[-10:],
    }


def empty_active_recall(reason: str = "not_evaluated") -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "recalled_at": utc_now(),
        "decision_cutoff": utc_now(),
        "query": "",
        "filters": {},
        "hit_count": 0,
        "active_recall_hit_rate": 0.0,
        "memory_ids_used": [],
        "dont_do_hits": [],
        "hits": [],
        "decision_delta": {"action": "none", "reason": reason, "memory_ids": [], "can_loosen": False},
        "can_place_live_orders": False,
        "can_loosen_risk": False,
    }


def decide_paper_action(candidates: list[dict[str, Any]], setup_stats: list[dict[str, Any]], account: dict[str, Any], exploration_allowed: bool = False) -> dict[str, Any]:
    runtime_pause = paper_opens_paused_by_runtime()
    if runtime_pause.get("paused"):
        decision = {
            "schema_version": SCHEMA_VERSION,
            "decided_at": utc_now(),
            "action": "skip",
            "reason": runtime_pause.get("reason") or "host_runtime_pause",
            "errors": ["host_runtime_pause_paper_opens"],
            "runtime_pause": runtime_pause,
            "active_recall": empty_active_recall("host_runtime_pause"),
            "memory_ids_used": [],
            "can_place_live_orders": False,
        }
        risk_state = skipped_risk_state("host_runtime_pause", ["host_runtime_pause_paper_opens"], [])
        decision["risk_decision"] = risk_state
        write_json_atomic(PAPER_RISK_STATE, risk_state)
        write_json_atomic(BRAIN_LATEST, decision)
        append_jsonl(BRAIN_HISTORY, decision)
        return decision
    rankings = rank_setups(setup_stats)["rankings"]
    ranked_candidates = rank_candidate_list(candidates, rankings)
    if not ranked_candidates:
        decision = {"schema_version": SCHEMA_VERSION, "decided_at": utc_now(), "action": "skip", "reason": "no_candidate", "active_recall": empty_active_recall("no_candidate"), "memory_ids_used": [], "can_place_live_orders": False}
        write_json_atomic(BRAIN_LATEST, decision)
        append_jsonl(BRAIN_HISTORY, decision)
        return decision
    attempts = []
    fallback_decision = None
    for candidate in ranked_candidates:
        candidate = attach_setup_contract(candidate, rankings)
        decision_cutoff = str(candidate.get("decision_cutoff") or candidate.get("market_snapshot_ts") or utc_now())
        active_recall = active_recall_for_decision(candidate, decision_cutoff=decision_cutoff)
        recall_delta = active_recall.get("decision_delta") if isinstance(active_recall.get("decision_delta"), dict) else {}
        if open_position_conflict(candidate, account):
            attempts.append({"candidate_id": candidate.get("candidate_id"), "symbol": candidate.get("symbol"), "setup_id": candidate.get("setup_id"), "action": "skip", "errors": ["matching_position_already_open"], "allocation_tier": None, "active_recall": active_recall, "memory_ids_used": active_recall.get("memory_ids_used") or []})
            continue
        feature_errors = candidate_feature_errors(candidate)
        if feature_errors:
            attempts.append({"candidate_id": candidate.get("candidate_id"), "symbol": candidate.get("symbol"), "setup_id": candidate.get("setup_id"), "feature_id": candidate.get("feature_id"), "action": "skip", "errors": feature_errors, "allocation_tier": None, "active_recall": active_recall, "memory_ids_used": active_recall.get("memory_ids_used") or []})
            continue
        preflight = run_preflight({"action": "paper_decision", "requires_fresh_market": True, "requires_lifecycle_clean": True, "candidate": candidate}, symbol=candidate.get("symbol"))
        preflight_action = preflight.get("action") if isinstance(preflight.get("action"), dict) else {}
        preflight_candidate = preflight_action.get("candidate") if isinstance(preflight_action.get("candidate"), dict) else None
        if preflight_candidate is not None:
            candidate = preflight_candidate
        dont_do = evaluate_candidate(candidate)
        allocation = allocate_capital(str(candidate.get("setup_id")), rankings, account, exploration_allowed=exploration_allowed)
        allocation = apply_capability_size_cap(allocation, candidate, account)
        requested_leverage, leverage_factors = adaptive_paper_leverage(candidate, allocation, account)
        sizing_candidate = {**candidate, "leverage": requested_leverage}
        requested_margin = futures_margin_from_risk_budget(sizing_candidate, allocation, account)
        if allocation.get("allowed"):
            instrument = None
            if isinstance(preflight.get("instrument"), dict):
                instrument = preflight["instrument"].get("instrument")
            risk = evaluate_paper_order(candidate.get("symbol"), candidate.get("side"), candidate.get("entry"), candidate.get("sl"), candidate.get("tp"), requested_margin=requested_margin, requested_leverage=requested_leverage, setup_id=str(candidate.get("setup_id")), account=account, instrument=instrument)
        else:
            risk = {"schema_version": SCHEMA_VERSION, "evaluated_at": utc_now(), "mode": "paper", "can_open_paper": False, "can_place_live_orders": False, "reason": "allocation_blocked", "errors": [], "setup_id": str(candidate.get("setup_id")), "symbol": candidate.get("symbol"), "side": candidate.get("side")}
        risk["paper_sizing"] = build_paper_sizing(candidate, allocation, account, requested_margin, requested_leverage, leverage_factors, risk=risk, preflight=preflight)
        sizing_errors = risk["paper_sizing"].get("errors") or []
        if sizing_errors:
            risk["can_open_paper"] = False
            risk["errors"] = sorted(set(list(risk.get("errors") or []) + list(sizing_errors)))
            risk["reason"] = ";".join(risk["errors"])
        errors = []
        if not paper_action_allowed(preflight):
            errors.extend(preflight.get("errors") or [])
        if dont_do.get("blocked"):
            errors.append("dont_do_match")
        if recall_delta.get("action") == "block":
            errors.append("active_recall_block")
        errors.extend(paper_only_patch_errors(candidate, rankings))
        if not allocation.get("allowed"):
            errors.extend(allocation.get("errors") or [])
        if not risk.get("can_open_paper"):
            errors.extend(risk.get("errors") or [])
        errors.extend(sizing_errors)
        action = "paper_open_candidate" if not errors else "shadow_only" if dont_do.get("action") == "shadow_only" else "skip"
        attempt = {"candidate_id": candidate.get("candidate_id"), "symbol": candidate.get("symbol"), "setup_id": candidate.get("setup_id"), "feature_id": candidate.get("feature_id"), "action": action, "errors": sorted(set(errors)), "allocation_tier": allocation.get("tier"), "capability_action": (candidate.get("decision_data_capability_mask") or {}).get("action") if isinstance(candidate.get("decision_data_capability_mask"), dict) else None, "active_recall": active_recall, "memory_ids_used": active_recall.get("memory_ids_used") or []}
        attempts.append(attempt)
        decision = {"schema_version": SCHEMA_VERSION, "decided_at": utc_now(), "action": action, "feature_id": candidate.get("feature_id"), "feature_manifest_id": candidate.get("feature_manifest_id"), "decision_data_capability_mask": candidate.get("decision_data_capability_mask"), "decision_regime_state": candidate.get("decision_regime_state"), "candidate": candidate, "errors": sorted(set(errors)), "preflight": preflight, "dont_do": dont_do, "active_recall": active_recall, "memory_ids_used": active_recall.get("memory_ids_used") or [], "allocation": allocation, "risk_decision": risk, "candidate_attempts": attempts, "can_place_live_orders": False}
        fallback_decision = decision
        if action == "paper_open_candidate":
            write_json_atomic(PAPER_RISK_STATE, risk)
            write_json_atomic(BRAIN_LATEST, decision)
            append_jsonl(BRAIN_HISTORY, decision)
            return decision
    decision = fallback_decision or {"schema_version": SCHEMA_VERSION, "decided_at": utc_now(), "action": "skip", "reason": "no_tradeable_candidate", "active_recall": attempts[-1].get("active_recall") if attempts else empty_active_recall("no_tradeable_candidate"), "memory_ids_used": attempts[-1].get("memory_ids_used") if attempts else [], "candidate_attempts": attempts, "can_place_live_orders": False}
    risk_state = decision.get("risk_decision") if isinstance(decision.get("risk_decision"), dict) and decision.get("risk_decision") else skipped_risk_state(str(decision.get("reason") or "no_tradeable_candidate"), [error for attempt in attempts for error in (attempt.get("errors") or [])], attempts)
    decision.setdefault("risk_decision", risk_state)
    write_json_atomic(PAPER_RISK_STATE, risk_state)
    write_json_atomic(BRAIN_LATEST, decision)
    append_jsonl(BRAIN_HISTORY, decision)
    return decision
