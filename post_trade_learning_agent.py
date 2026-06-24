"""Objective post-trade review for paper/shadow closes."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl_once, read_jsonl, write_json_atomic
from episodic_task_ledger import record_episode
from timebase import seconds_between, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
REVIEWS_JSONL = MEMORY_DIR / "post_trade_reviews.jsonl"
LATEST_JSON = MEMORY_DIR / "post_trade_learning_latest.json"
COUNTERFACTUAL_REPLAYS_JSONL = MEMORY_DIR / "counterfactual_replays.jsonl"


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def review_id(trade_id: str, close_ts: str | None) -> str:
    return "review_" + hashlib.sha256(f"{trade_id}:{close_ts}".encode("utf-8")).hexdigest()[:20]


def mae_mfe(side: str, entry: float, candles: list[dict[str, Any]]) -> tuple[float, float]:
    mae = 0.0
    mfe = 0.0
    if entry <= 0:
        return mae, mfe
    for row in candles:
        high = safe_float(row.get("high"))
        low = safe_float(row.get("low"))
        if side.upper() == "LONG":
            mfe = max(mfe, (high - entry) / entry)
            mae = min(mae, (low - entry) / entry)
        else:
            mfe = max(mfe, (entry - low) / entry)
            mae = min(mae, (entry - high) / entry)
    return round(mae, 8), round(mfe, 8)


def r_multiple(side: str, entry: float, exit_price: float, sl: float) -> float:
    risk = abs(entry - sl)
    if risk <= 0:
        return 0.0
    pnl = exit_price - entry if side.upper() == "LONG" else entry - exit_price
    return round(pnl / risk, 6)

def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))

def cost_breakdown(trade: dict[str, Any]) -> dict[str, Any]:
    entry_fee = safe_float(trade.get("entry_fee"))
    exit_fee = safe_float(trade.get("exit_fee"))
    explicit_fee = safe_float(trade.get("fee") or trade.get("fees"))
    fees = explicit_fee if explicit_fee else entry_fee + exit_fee
    funding = safe_float(trade.get("funding_payment"))
    slippage = safe_float(trade.get("slippage"))
    gross = safe_float(trade.get("gross"))
    net_before_funding = safe_float(trade.get("net_before_funding"), gross - fees)
    net = safe_float(trade.get("net"), net_before_funding + funding)
    margin = abs(safe_float(trade.get("margin")))
    return {
        "entry_fee": round(entry_fee, 8),
        "exit_fee": round(exit_fee, 8),
        "fees": round(fees, 8),
        "funding_payment": round(funding, 8),
        "slippage": round(slippage, 8),
        "gross": round(gross, 8),
        "net_before_funding": round(net_before_funding, 8),
        "net": round(net, 8),
        "fee_to_gross_pct": round(fees / abs(gross), 6) if gross else 0.0,
        "fee_to_margin_pct": round(fees / margin, 6) if margin else 0.0,
        "total_cost": round(fees - funding + abs(slippage), 8),
    }

def latest_counterfactual_for(trade_id: str, path: Path | None = None) -> dict[str, Any]:
    path = path or COUNTERFACTUAL_REPLAYS_JSONL
    matches = [row for row in read_jsonl(path, limit=500) if str(row.get("signal_id") or "") == str(trade_id)]
    return matches[-1] if matches else {}

def setup_validity_score(trade: dict[str, Any], setup_score: dict[str, Any] | None, process_quality: float) -> float:
    score = safe_float((setup_score or {}).get("score"), process_quality)
    if not trade.get("setup_id"):
        score -= 0.2
    if safe_float(trade.get("entry")) <= 0 or safe_float(trade.get("sl")) <= 0 or safe_float(trade.get("tp")) <= 0:
        score -= 0.3
    return round(clamp(score), 4)

def primary_failure_reason(
    trade: dict[str, Any],
    classification: str,
    r_value: float,
    costs: dict[str, Any],
    counterfactual: dict[str, Any],
) -> str:
    reason = str(trade.get("reason") or trade.get("close_reason") or "")
    if classification == "news_conflict":
        return "news_conflict"
    if classification == "stop_too_tight":
        return "stop_too_tight"
    if reason == "liquidation":
        return "liquidation"
    if counterfactual.get("conclusion") == "parameter_improvement_candidate":
        return "counterfactual_parameter_improvement"
    if costs.get("fees", 0) > abs(safe_float(trade.get("gross"))) and safe_float(trade.get("net")) <= 0:
        return "fee_drag_dominated"
    if safe_float(costs.get("funding_payment")) < 0 and safe_float(trade.get("net")) <= 0:
        return "funding_drag"
    if classification == "bad_win":
        return "bad_process_win"
    if classification == "good_loss":
        return "good_process_loss"
    if classification == "tp_too_far":
        return "timeout_tp_not_reached"
    if safe_float(trade.get("net")) > 0:
        return "no_failure_profit"
    if r_value <= -1:
        return "adverse_move_to_stop"
    return "setup_or_timing_failed"


def detect_stop_too_tight(trade: dict[str, Any], candles_after_close: list[dict[str, Any]]) -> bool:
    reason = str(trade.get("reason") or trade.get("close_reason") or "")
    if reason not in {"sl", "ambiguous_sl_first"}:
        return False
    tp = safe_float(trade.get("tp") or trade.get("take_profit"))
    side = str(trade.get("side") or "").upper()
    for row in candles_after_close[:5]:
        if side == "LONG" and safe_float(row.get("high")) >= tp:
            return True
        if side == "SHORT" and safe_float(row.get("low")) <= tp:
            return True
    return False


def classify_trade(trade: dict[str, Any], process_quality: float, mfe: float, stop_too_tight: bool, news_conflict: bool) -> str:
    net = safe_float(trade.get("net"), safe_float(trade.get("gross")))
    reason = str(trade.get("reason") or trade.get("close_reason") or "")
    if news_conflict:
        return "news_conflict"
    if stop_too_tight:
        return "stop_too_tight"
    if net > 0 and process_quality < 0.55:
        return "bad_win"
    if net > 0:
        return "good_win"
    if net <= 0 and mfe >= 0.005 and process_quality >= 0.6:
        return "good_loss"
    if reason == "timeout" and mfe > 0:
        return "tp_too_far"
    return "bad_loss"


def review_closed_trade(
    trade: dict[str, Any],
    candles: list[dict[str, Any]],
    setup_score: dict[str, Any] | None = None,
    news_snapshot: dict[str, Any] | None = None,
    append: bool = True,
) -> dict[str, Any]:
    trade_id = str(trade.get("trade_id") or trade.get("close_id") or trade.get("shadow_id") or "unknown")
    side = str(trade.get("side") or (trade.get("signal") or {}).get("side") or "").upper()
    entry = safe_float(trade.get("entry"))
    exit_price = safe_float(trade.get("exit") or trade.get("close"))
    sl = safe_float(trade.get("sl") or trade.get("stop"))
    mae, mfe = mae_mfe(side, entry, candles)
    process_quality = safe_float((setup_score or {}).get("score"), 0.5)
    high_risk_news = bool((news_snapshot or {}).get("high_risk_before_entry"))
    stop_tight = detect_stop_too_tight(trade, candles)
    classification = classify_trade(trade, process_quality, mfe, stop_tight, high_risk_news)
    r_value = r_multiple(side, entry, exit_price, sl)
    costs = cost_breakdown(trade)
    counterfactual = latest_counterfactual_for(trade_id)
    setup_score_value = setup_validity_score(trade, setup_score, process_quality)
    duration = seconds_between(trade.get("open_ts") or trade.get("entry_ts"), trade.get("close_ts"))
    review = {
        "schema_version": SCHEMA_VERSION,
        "review_id": review_id(trade_id, trade.get("close_ts")),
        "trade_id": trade_id,
        "reviewed_at": utc_now(),
        "classification": classification,
        "primary_failure_reason": primary_failure_reason(trade, classification, r_value, costs, counterfactual),
        "mae": mae,
        "mfe": mfe,
        "r_multiple": r_value,
        "duration_seconds": duration,
        "process_quality_score": round(process_quality, 4),
        "setup_validity_score": setup_score_value,
        "outcome_quality_score": round(clamp(0.5 + (r_value / 2.0) - min(0.25, safe_float(costs.get("fee_to_margin_pct")))), 4),
        "costs": costs,
        "counterfactual": {
            "replay_id": counterfactual.get("replay_id"),
            "status": counterfactual.get("status"),
            "conclusion": counterfactual.get("conclusion") or counterfactual.get("reason"),
            "best_variant": counterfactual.get("best_variant"),
        } if counterfactual else {},
        "market_regime": trade.get("market_regime") or trade.get("regime"),
        "data_quality": {
            "candle_count": len(candles),
            "has_counterfactual": bool(counterfactual),
            "trade_data_quality": trade.get("data_quality"),
        },
        "flags": {
            "stop_too_tight": stop_tight,
            "news_conflict": high_risk_news,
            "liquidation": str(trade.get("reason")) == "liquidation",
            "fee_drag_high": safe_float(costs.get("fee_to_margin_pct")) >= 0.01,
            "funding_drag": safe_float(costs.get("funding_payment")) < 0,
        },
        "source_trade": trade,
    }
    if append:
        append_jsonl_once(REVIEWS_JSONL, review, "review_id")
        write_latest_summary()
        record_episode(
            "paper_close",
            "post trade review",
            context_refs=[trade_id, review["review_id"]],
            outcome={"classification": classification},
            lesson=f"classified {trade_id} as {classification}",
            quality=process_quality,
        )
    return review


def summarize_reviews(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_class: dict[str, int] = {}
    by_failure: dict[str, int] = {}
    process_scores = []
    outcome_scores = []
    setup_scores = []
    for row in rows:
        klass = str(row.get("classification") or "unknown")
        by_class[klass] = by_class.get(klass, 0) + 1
        failure = str(row.get("primary_failure_reason") or "unknown")
        by_failure[failure] = by_failure.get(failure, 0) + 1
        if "process_quality_score" in row:
            process_scores.append(safe_float(row.get("process_quality_score")))
        if "outcome_quality_score" in row:
            outcome_scores.append(safe_float(row.get("outcome_quality_score")))
        if "setup_validity_score" in row:
            setup_scores.append(safe_float(row.get("setup_validity_score")))
    def avg(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 4) if values else None
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "review_count": len(rows),
        "by_classification": by_class,
        "by_primary_failure_reason": by_failure,
        "avg_process_quality_score": avg(process_scores),
        "avg_outcome_quality_score": avg(outcome_scores),
        "avg_setup_validity_score": avg(setup_scores),
        "quality_sample_counts": {
            "process": len(process_scores),
            "outcome": len(outcome_scores),
            "setup_validity": len(setup_scores),
        },
    }


def write_latest_summary() -> dict[str, Any]:
    summary = summarize_reviews(read_jsonl(REVIEWS_JSONL))
    write_json_atomic(LATEST_JSON, summary)
    return summary
