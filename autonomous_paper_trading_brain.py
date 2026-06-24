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
from paper_portfolio_manager import evaluate_paper_order
from preflight_guard import run_preflight
from setup_ranker import rank_setups
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
BRAIN_LATEST = MEMORY_DIR / "paper_trading_brain_latest.json"
BRAIN_HISTORY = MEMORY_DIR / "paper_trading_brain_history.jsonl"
PAPER_RISK_STATE = MEMORY_DIR / "paper_risk_state.json"
MAX_PAPER_MARGIN_FRACTION = 0.25


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


def futures_margin_from_risk_budget(candidate: dict[str, Any], allocation: dict[str, Any], account: dict[str, Any]) -> float:
    """Convert risk budget at SL into Binance-futures-style isolated margin."""
    equity = max(0.0, safe_float(account.get("equity"), 100.0))
    cash = max(0.0, safe_float(account.get("cash"), equity))
    risk_budget = max(0.0, safe_float(allocation.get("max_loss_usdt")))
    leverage = max(1.0, safe_float(candidate.get("leverage"), 1.0))
    risk_distance = stop_distance_fraction(str(candidate.get("side") or ""), candidate.get("entry"), candidate.get("sl"))
    if risk_budget <= 0 or risk_distance <= 0:
        return 0.0
    raw_margin = risk_budget / (risk_distance * leverage)
    max_margin = min(cash, equity * MAX_PAPER_MARGIN_FRACTION)
    capped = max(0.0, min(raw_margin, max_margin))
    return floor(capped * 1_000_000) / 1_000_000


def choose_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    eligible = [row for row in candidates if row.get("symbol") and row.get("side") and row.get("setup_id")]
    eligible.sort(key=lambda row: float(row.get("score") or row.get("setup_score") or 0.0), reverse=True)
    return eligible[0] if eligible else None


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


def decide_paper_action(candidates: list[dict[str, Any]], setup_stats: list[dict[str, Any]], account: dict[str, Any], exploration_allowed: bool = False) -> dict[str, Any]:
    candidate = choose_candidate(candidates)
    if not candidate:
        decision = {"schema_version": SCHEMA_VERSION, "decided_at": utc_now(), "action": "skip", "reason": "no_candidate", "can_place_live_orders": False}
        write_json_atomic(BRAIN_LATEST, decision)
        append_jsonl(BRAIN_HISTORY, decision)
        return decision
    preflight = run_preflight({"action": "paper_decision", "requires_fresh_market": False, "requires_lifecycle_clean": False}, symbol=candidate.get("symbol"))
    dont_do = evaluate_candidate(candidate)
    rankings = rank_setups(setup_stats)["rankings"]
    allocation = allocate_capital(str(candidate.get("setup_id")), rankings, account, exploration_allowed=exploration_allowed)
    requested_margin = futures_margin_from_risk_budget(candidate, allocation, account)
    risk = evaluate_paper_order(candidate.get("symbol"), candidate.get("side"), candidate.get("entry"), candidate.get("sl"), candidate.get("tp"), requested_margin=requested_margin, requested_leverage=candidate.get("leverage", 1), setup_id=str(candidate.get("setup_id")), account=account)
    risk["paper_sizing"] = {
        "method": "risk_budget_to_isolated_margin",
        "risk_budget_usdt": allocation.get("max_loss_usdt"),
        "requested_margin": requested_margin,
        "stop_distance_fraction": round(stop_distance_fraction(str(candidate.get("side") or ""), candidate.get("entry"), candidate.get("sl")), 8),
        "max_margin_fraction": MAX_PAPER_MARGIN_FRACTION,
    }
    write_json_atomic(PAPER_RISK_STATE, risk)
    errors = []
    if not preflight.get("allowed"):
        errors.extend(preflight.get("errors") or [])
    if dont_do.get("blocked"):
        errors.append("dont_do_match")
    errors.extend(paper_only_patch_errors(candidate, rankings))
    if not allocation.get("allowed"):
        errors.extend(allocation.get("errors") or [])
    if not risk.get("can_open_paper"):
        errors.extend(risk.get("errors") or [])
    action = "paper_open_candidate" if not errors else "shadow_only" if dont_do.get("action") == "shadow_only" else "skip"
    decision = {"schema_version": SCHEMA_VERSION, "decided_at": utc_now(), "action": action, "candidate": candidate, "errors": sorted(set(errors)), "preflight": preflight, "dont_do": dont_do, "allocation": allocation, "risk_decision": risk, "can_place_live_orders": False}
    write_json_atomic(BRAIN_LATEST, decision)
    append_jsonl(BRAIN_HISTORY, decision)
    return decision
