"""Paper-only execution lifecycle loop.

This daemon turns an approved ``paper_open_candidate`` decision into a clean
simulated trade lifecycle. It opens paper positions, monitors mark-price
snapshots, closes on SL/TP/timeout, and emits validated learning rows. It never
imports exchange clients and cannot place live orders.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, append_jsonl_once, canonical_json, read_json, write_json_atomic
from live_permission_firewall import evaluate_live_permission, paper_action_allowed
from market_data_lake import store_candles
from paper_execution_simulator import DEFAULT_SLIPPAGE_BPS, TAKER_FEE_RATE, exit_slippage as cost_exit_slippage, simulate_entry_order, simulate_exit
from paper_cost_model import fill_bps as cost_fill_bps, liquidity_tier as cost_liquidity_tier
from paper_portfolio_manager import close_paper_position, load_account, open_paper_position, save_account
from post_trade_learning_agent import review_closed_trade
from timebase import seconds_between, utc_now
from trade_lifecycle_validator import write_latest_report

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
PID_FILE = STATE_DIR / "paper_execution_lifecycle_loop.pid"
HEARTBEAT_PATH = STATE_DIR / "paper_execution_lifecycle_loop_heartbeat.json"
STOP_FILE = STATE_DIR / "STOP_PAPER_EXECUTION_LIFECYCLE_LOOP"
DECISION_LATEST = MEMORY_DIR / "autonomous_paper_trading_loop_latest.json"
MARKET_LATEST = STATE_DIR / "market_updates_latest.json"
LATEST_PATH = MEMORY_DIR / "paper_execution_lifecycle_latest.json"
HISTORY_PATH = MEMORY_DIR / "paper_execution_lifecycle_history.jsonl"
SEEN_PATH = MEMORY_DIR / "paper_execution_lifecycle_seen.json"
PAPER_TRADES_PATH = MEMORY_DIR / "paper_trades.jsonl"
MAX_HOLD_SECONDS = 30 * 60
MAX_OPEN_POSITIONS = 8
MAX_PORTFOLIO_MARGIN_FRACTION = Decimal("0.85")
MAX_PORTFOLIO_RISK_FRACTION = Decimal("0.15")
MAX_MARKET_SNAPSHOT_AGE_SECONDS = 15 * 60
MAX_CHART_EVIDENCE_AGE_SECONDS = 15 * 60
FUNDING_INTERVAL_HOURS = 8
MAX_REPLAY_CANDLES = 240
# Phase 1: resolve exits against REAL intrabar OHLC instead of single-point mark
# snapshots, so SL/TP fire on wick touches instead of ~54% blind timeouts.
EXIT_CANDLE_TIMEFRAME = "5m"
EXIT_CANDLE_LIMIT = 60


def dec(value: Any, default: str = "0") -> Decimal:
    try:
        if value in (None, ""):
            return Decimal(default)
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def dec_str(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.00000001")).normalize())

def json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return dec_str(value)
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_ready(item) for item in value]
    if isinstance(value, tuple):
        return [json_ready(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value

def stable_digest(prefix: str, payload: Any) -> str:
    return f"{prefix}_" + hashlib.sha256(canonical_json(json_ready(payload)).encode("utf-8")).hexdigest()[:24]

def payload_digest(payload: Any) -> str | None:
    if payload in (None, "", [], {}):
        return None
    return "sha256:" + hashlib.sha256(canonical_json(json_ready(payload)).encode("utf-8")).hexdigest()

def parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    except Exception:
        return None

def market_snapshot_reject_reason(candidate: dict[str, Any], reference_ts: Any | None = None, max_age_seconds: int = MAX_MARKET_SNAPSHOT_AGE_SECONDS) -> str | None:
    snapshot_ts = candidate.get("market_snapshot_ts")
    if not snapshot_ts:
        return "missing_market_snapshot_ts"
    snapshot = parse_ts(snapshot_ts)
    if snapshot is None:
        return "invalid_market_snapshot_ts"
    reference = parse_ts(reference_ts or utc_now())
    if reference is None:
        return "invalid_reference_ts"
    age = (reference - snapshot).total_seconds()
    if age < 0:
        return "market_snapshot_after_open"
    if age > max_age_seconds:
        return "stale_market_snapshot"
    return None

def chart_score_from(candidate: dict[str, Any], decision: dict[str, Any] | None = None) -> dict[str, Any] | None:
    decision = decision or {}
    for value in (candidate.get("chart_score"), decision.get("chart_score")):
        if isinstance(value, dict) and value:
            return value
    return None

def chart_risk_plan_from(candidate: dict[str, Any], decision: dict[str, Any] | None = None, risk: dict[str, Any] | None = None) -> dict[str, Any] | None:
    decision = decision or {}
    risk = risk or {}
    for value in (candidate.get("chart_risk_plan"), decision.get("chart_risk_plan"), risk.get("chart_risk_plan")):
        if isinstance(value, dict) and value:
            return value
    return None

def chart_id_fields(candidate: dict[str, Any], decision: dict[str, Any] | None = None, risk: dict[str, Any] | None = None) -> dict[str, Any]:
    score = chart_score_from(candidate, decision)
    risk_plan = chart_risk_plan_from(candidate, decision, risk)
    return {
        "chart_score_id": (score or {}).get("score_id") or candidate.get("chart_score_id") or (risk or {}).get("chart_score_id"),
        "chart_risk_plan_id": (risk_plan or {}).get("risk_plan_id") or candidate.get("chart_risk_plan_id") or (risk or {}).get("chart_risk_plan_id"),
        "chart_intelligence_id": candidate.get("chart_intelligence_id") or (score or {}).get("chart_intelligence_id") or (score or {}).get("score_id"),
    }

def chart_snapshot_id_from(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("snapshot_id", "chart_snapshot_id"):
            if value.get(key):
                return str(value[key])
    if isinstance(value, str) and value:
        return value
    return None

def chart_snapshot_ids_from(candidate: dict[str, Any], position: dict[str, Any] | None = None) -> dict[str, str]:
    ids: dict[str, str] = {}
    for source in (candidate.get("chart_snapshot_ids"), (position or {}).get("chart_snapshot_ids")):
        if isinstance(source, dict):
            for key, value in source.items():
                snapshot_id = chart_snapshot_id_from(value)
                if snapshot_id:
                    ids[str(key)] = snapshot_id
    score = chart_score_from(candidate)
    candidate_snapshot = (
        chart_snapshot_id_from(candidate.get("candidate_chart_snapshot"))
        or chart_snapshot_id_from(candidate.get("chart_snapshot"))
        or chart_snapshot_id_from(candidate.get("chart_snapshot_id"))
        or chart_snapshot_id_from((score or {}).get("chart_snapshot"))
        or chart_snapshot_id_from((score or {}).get("chart_snapshot_id"))
        or chart_snapshot_id_from((score or {}).get("snapshot_id"))
    )
    if candidate_snapshot:
        ids.setdefault("candidate", candidate_snapshot)
    return ids

def chart_preflight_for_candidate(
    candidate: dict[str, Any],
    decision: dict[str, Any] | None = None,
    *,
    reference_ts: Any | None = None,
    max_age_seconds: int = MAX_CHART_EVIDENCE_AGE_SECONDS,
) -> dict[str, Any]:
    decision = decision or {}
    score = chart_score_from(candidate, decision)
    mask = candidate.get("chart_data_capability_mask") if isinstance(candidate.get("chart_data_capability_mask"), dict) else {}
    if not mask and isinstance(score, dict) and isinstance(score.get("capability_mask"), dict):
        mask = score["capability_mask"]
    ids = chart_id_fields(candidate, decision)
    chart_used = bool(score or ids.get("chart_intelligence_id"))
    if not chart_used:
        return {
            "status": "not_applicable",
            "chart_used": False,
            "reject_open": False,
            "chart_learning_eligible": False,
            "warnings": ["ticker_proxy_chart_ineligible"] if mask.get("action") == "skip" else [],
            "errors": [],
            "chart_score_id": None,
            "chart_risk_plan_id": None,
            "chart_intelligence_id": None,
            "can_place_live_orders": False,
        }
    errors: list[str] = []
    warnings: list[str] = []
    if mask.get("action") == "skip":
        errors.append("chart_capability_skip")
    for status in (candidate.get("chart_data_status"), (score or {}).get("degradation_state")):
        if status in {"stale", "quarantined", "missing_required"}:
            errors.append(f"chart_status_{status}")
        elif status in {"partial", "diagnostic_only"}:
            warnings.append(f"chart_status_{status}")
    cutoff = (score or {}).get("decision_cutoff") or candidate.get("chart_decision_cutoff") or candidate.get("decision_cutoff")
    cutoff_dt = parse_ts(cutoff)
    reference = parse_ts(reference_ts or decision.get("decided_at") or utc_now())
    if cutoff_dt is None:
        errors.append("missing_chart_decision_cutoff")
    elif reference is None:
        errors.append("invalid_chart_reference_ts")
    elif cutoff_dt > reference:
        errors.append("chart_cutoff_after_decision")
    elif (reference - cutoff_dt).total_seconds() > max_age_seconds:
        errors.append("stale_chart_evidence")
    cutoff_proof = (score or {}).get("cutoff_proof") if isinstance((score or {}).get("cutoff_proof"), dict) else candidate.get("chart_cutoff_proof")
    if isinstance(score, dict) and (not isinstance(cutoff_proof, dict) or cutoff_proof.get("ok") is not True):
        errors.append("chart_cutoff_proof_not_ok")
    return {
        "status": "rejected" if errors else "degraded" if warnings else "ok",
        "chart_used": True,
        "reject_open": bool(errors),
        "reason": errors[0] if errors else None,
        "chart_learning_eligible": not errors and not warnings,
        "warnings": sorted(set(warnings)),
        "errors": sorted(set(errors)),
        **ids,
        "decision_cutoff": cutoff,
        "capability_action": mask.get("action"),
        "can_place_live_orders": False,
    }

def try_render_chart_snapshot(
    candidate: dict[str, Any],
    *,
    score: dict[str, Any] | None,
    risk_plan: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    candle_batch = candidate.get("chart_candle_batch") if isinstance(candidate.get("chart_candle_batch"), dict) else None
    if candle_batch is None and isinstance(candidate.get("candle_batch"), dict):
        candle_batch = candidate["candle_batch"]
    if candle_batch is None:
        return None, ["missing_chart_candle_batch_for_render"]
    try:
        from chart_snapshot_renderer import render_snapshot

        metadata = render_snapshot(
            candle_batch,
            indicator_bundle=candidate.get("chart_indicator_bundle") if isinstance(candidate.get("chart_indicator_bundle"), dict) else None,
            score=score,
            risk_plan=risk_plan,
            zone_bundle=candidate.get("chart_zone_bundle") if isinstance(candidate.get("chart_zone_bundle"), dict) else None,
            trendline_bundle=candidate.get("chart_trendline_bundle") if isinstance(candidate.get("chart_trendline_bundle"), dict) else None,
            structure_bundle=candidate.get("chart_structure_bundle") if isinstance(candidate.get("chart_structure_bundle"), dict) else None,
            liquidity_bundle=candidate.get("chart_liquidity_bundle") if isinstance(candidate.get("chart_liquidity_bundle"), dict) else None,
        )
        return metadata, []
    except Exception as exc:
        return None, [f"chart_snapshot_render_failed:{type(exc).__name__}"]

def chart_snapshot_summary(
    candidate: dict[str, Any],
    decision: dict[str, Any] | None = None,
    *,
    position: dict[str, Any] | None = None,
    stage: str = "open",
    candle: dict[str, Any] | None = None,
    chart_preflight: dict[str, Any] | None = None,
) -> dict[str, Any]:
    decision = decision or {}
    risk = decision.get("risk_decision") if isinstance(decision.get("risk_decision"), dict) else {}
    score = chart_score_from(candidate, decision)
    risk_plan = chart_risk_plan_from(candidate, decision, risk)
    ids = chart_snapshot_ids_from(candidate, position)
    id_fields = chart_id_fields(candidate, decision, risk)
    preflight = chart_preflight or chart_preflight_for_candidate(candidate, decision)
    for key in ("chart_score_id", "chart_risk_plan_id", "chart_intelligence_id"):
        if not id_fields.get(key) and preflight.get(key):
            id_fields[key] = preflight.get(key)
    chart_used = bool(preflight.get("chart_used") or score or id_fields.get("chart_intelligence_id"))
    warnings = list(preflight.get("warnings") or [])
    errors = list(preflight.get("errors") or [])
    rendered_metadata = None
    if chart_used and stage in {"candidate", "open"}:
        rendered_metadata, render_warnings = try_render_chart_snapshot(candidate, score=score, risk_plan=risk_plan)
        warnings.extend(render_warnings)
        if rendered_metadata and rendered_metadata.get("snapshot_id"):
            ids[stage] = str(rendered_metadata["snapshot_id"])
    if chart_used and "candidate" not in ids:
        ids["candidate"] = stable_digest(
            "chart_snapshot_missing_candidate",
            {"candidate_id": candidate.get("candidate_id"), "chart_score_id": id_fields.get("chart_score_id")},
        )
        warnings.append("missing_candidate_chart_snapshot")
    if chart_used and stage in {"open", "close"} and stage not in ids:
        ids[stage] = stable_digest(
            f"paper_{stage}_chart_snapshot",
            {
                "stage": stage,
                "trade_id": (position or {}).get("position_id"),
                "candidate_id": candidate.get("candidate_id") or (position or {}).get("candidate_id"),
                "chart_score_id": id_fields.get("chart_score_id"),
                "risk_plan_id": id_fields.get("chart_risk_plan_id"),
                "ts": (position or {}).get("opened_at") if stage == "open" else (candle or {}).get("ts"),
                "price": (position or {}).get("entry") if stage == "open" else (candle or {}).get("close"),
            },
        )
        warnings.append(f"{stage}_chart_snapshot_metadata_only")
    source_hashes = {
        "candidate": payload_digest(candidate),
        "decision": payload_digest(decision),
        "chart_score": payload_digest(score),
        "chart_risk_plan": payload_digest(risk_plan),
        "feature_artifact": candidate.get("feature_artifact_digest") or (position or {}).get("feature_artifact_digest"),
        "rendered_snapshot": payload_digest(rendered_metadata),
    }
    source_hashes = {key: value for key, value in source_hashes.items() if value}
    status = "not_applicable"
    if chart_used:
        status = "degraded" if errors or warnings else "ok"
    return {
        "schema_version": SCHEMA_VERSION,
        "contract": "PaperChartEvidence.v1",
        "stage": stage,
        "chart_used": chart_used,
        "status": status,
        "chart_learning_eligible": bool(chart_used and status == "ok"),
        "chart_snapshot_ids": ids,
        "chart_score_id": id_fields.get("chart_score_id"),
        "chart_risk_plan_id": id_fields.get("chart_risk_plan_id"),
        "chart_intelligence_id": id_fields.get("chart_intelligence_id"),
        "warnings": sorted(set(warnings)),
        "errors": sorted(set(errors)),
        "source_hashes": source_hashes,
        "rendered_snapshot": rendered_metadata,
        "can_place_live_orders": False,
    }

def compact_candidate_for_position_snapshot(candidate: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "candidate_id",
        "symbol",
        "side",
        "setup_id",
        "score",
        "entry",
        "sl",
        "tp",
        "market_snapshot_ts",
        "feature_id",
        "feature_manifest_id",
        "feature_artifact_digest",
        "chart_intelligence_id",
        "chart_score_value",
        "chart_score_tier",
        "chart_data_status",
        "chart_decision_eligible",
    ]
    return {key: candidate.get(key) for key in keys if candidate.get(key) not in (None, "", [], {})}

def compact_risk_for_position_snapshot(risk: dict[str, Any]) -> dict[str, Any]:
    keys = ["risk_decision_id", "can_open_paper", "symbol", "side", "setup_id", "entry", "sl", "tp", "qty", "margin", "leverage", "notional", "estimated_loss", "reason", "errors"]
    return {key: risk.get(key) for key in keys if risk.get(key) not in (None, "", [], {})}

def account_snapshot(account: dict[str, Any]) -> dict[str, Any]:
    positions = account.get("open_positions") if isinstance(account.get("open_positions"), list) else []
    return {
        "equity": account.get("equity"),
        "cash": account.get("cash"),
        "realized_pnl": account.get("realized_pnl"),
        "fees_paid": account.get("fees_paid"),
        "open_positions": len(positions),
        "closed_trades": account.get("closed_trades"),
    }

def build_paper_position_snapshot_v2(
    position: dict[str, Any],
    candidate: dict[str, Any],
    decision: dict[str, Any],
    chart_preflight: dict[str, Any],
    account: dict[str, Any],
    chart_evidence: dict[str, Any],
) -> dict[str, Any]:
    risk = decision.get("risk_decision") if isinstance(decision.get("risk_decision"), dict) else {}
    feature = {
        "feature_id": candidate.get("feature_id") or decision.get("feature_id"),
        "feature_manifest_id": candidate.get("feature_manifest_id") or decision.get("feature_manifest_id"),
        "decision_data_capability_mask": candidate.get("decision_data_capability_mask") or decision.get("decision_data_capability_mask"),
        "decision_regime_state": candidate.get("decision_regime_state") or decision.get("decision_regime_state"),
    }
    source_digests = {
        "candidate": payload_digest(candidate),
        "decision": payload_digest(decision),
        "risk_decision": payload_digest(risk),
        "chart_score": payload_digest(chart_score_from(candidate, decision)),
        "chart_risk_plan": payload_digest(chart_risk_plan_from(candidate, decision, risk)),
        "account": payload_digest(account_snapshot(account)),
        "feature_artifact": candidate.get("feature_artifact_digest"),
    }
    source_digests = {key: value for key, value in source_digests.items() if value}
    payload = {
        "schema_version": SCHEMA_VERSION,
        "contract": "paper_position_snapshot_v2",
        "snapshot_version": 2,
        "created_at": utc_now(),
        "trade_id": position.get("position_id"),
        "mode": "paper",
        "candidate": compact_candidate_for_position_snapshot(candidate),
        "feature": feature,
        "chart": chart_evidence,
        "risk": compact_risk_for_position_snapshot(risk),
        "preflight": {"chart": chart_preflight, "decision": decision.get("preflight") if isinstance(decision.get("preflight"), dict) else None},
        "account": account_snapshot(account),
        "source_digests": source_digests,
        "can_place_live_orders": False,
    }
    payload["snapshot_id"] = stable_digest("paper_position_snapshot", {"trade_id": payload["trade_id"], "source_digests": source_digests, "chart_snapshot_ids": chart_evidence.get("chart_snapshot_ids")})
    return json_ready(payload)

def calculate_entry_fee(risk: dict[str, Any]) -> Decimal:
    return abs(dec(risk.get("notional")) * TAKER_FEE_RATE)

def apply_entry_execution(
    risk: dict[str, Any],
    candidate: dict[str, Any],
    market: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    symbol = str(risk.get("symbol") or candidate.get("symbol") or "").upper()
    side = str(risk.get("side") or candidate.get("side") or "").upper()
    candle = mark_candle(symbol, market or {})
    if not candle:
        return risk, {
            "status": "no_entry_mark_snapshot",
            "slippage_bps": "0",
            "price_basis": (candidate.get("price_basis") or {}).get("fills") if isinstance(candidate.get("price_basis"), dict) else "LAST",
            "live_execution": False,
        }
    fill = simulate_entry_order(symbol, side, "market", risk.get("qty"), risk.get("entry"), candle, append_order=False)
    if fill.get("status") not in {"filled", "partial"} or not fill.get("fill_price"):
        return risk, {**fill, "live_execution": False}
    fill_price = dec(fill.get("fill_price"))
    qty = dec(fill.get("filled_qty"), str(risk.get("qty") or "0"))
    leverage = dec(risk.get("leverage"), "1")
    notional = fill_price * qty
    margin = notional / leverage if leverage > 0 else dec(risk.get("margin"))
    sl = dec(risk.get("sl"))
    if side == "LONG":
        risk_distance = max(Decimal("0"), (fill_price - sl) / fill_price) if fill_price > 0 else Decimal("0")
    else:
        risk_distance = max(Decimal("0"), (sl - fill_price) / fill_price) if fill_price > 0 else Decimal("0")
    estimated_loss = notional * risk_distance
    updated = {
        **risk,
        "entry": dec_str(fill_price),
        "qty": dec_str(qty),
        "notional": dec_str(notional),
        "margin": dec_str(margin),
        "estimated_loss": dec_str(estimated_loss),
        "risk_distance": dec_str(risk_distance),
        "fee_to_close_reserve": dec_str(abs(notional) * TAKER_FEE_RATE),
    }
    return updated, {
        **fill,
        "slippage_bps": str(DEFAULT_SLIPPAGE_BPS.normalize()),
        "price_basis": (candidate.get("price_basis") or {}).get("fills") if isinstance(candidate.get("price_basis"), dict) else "BOOK_MID/LAST+slippage",
        "live_execution": False,
    }

def position_risk_at_stop(position: dict[str, Any]) -> Decimal:
    side = str(position.get("side") or "").upper()
    entry = dec(position.get("entry"))
    sl = dec(position.get("sl"))
    qty = dec(position.get("qty"))
    if entry <= 0 or sl <= 0 or qty <= 0:
        return Decimal("0")
    if side == "LONG":
        return max(Decimal("0"), (entry - sl) * qty)
    if side == "SHORT":
        return max(Decimal("0"), (sl - entry) * qty)
    return Decimal("0")

def portfolio_open_reject_reason(account: dict[str, Any], risk: dict[str, Any]) -> dict[str, Any] | None:
    positions = [row for row in account.get("open_positions", []) if isinstance(row, dict)] if isinstance(account.get("open_positions"), list) else []
    if len(positions) >= MAX_OPEN_POSITIONS:
        return {"action": "open_skipped", "reason": "max_open_positions_reached", "open_positions": len(positions), "max_open_positions": MAX_OPEN_POSITIONS}
    equity = dec(account.get("equity"), "100")
    if equity <= 0:
        return {"action": "open_skipped", "reason": "invalid_account_equity"}
    open_margin = sum(dec(row.get("margin")) for row in positions)
    new_margin = dec(risk.get("margin"))
    max_margin = equity * MAX_PORTFOLIO_MARGIN_FRACTION
    if open_margin + new_margin > max_margin:
        return {
            "action": "open_skipped",
            "reason": "portfolio_margin_cap_reached",
            "open_margin": dec_str(open_margin),
            "new_margin": dec_str(new_margin),
            "max_portfolio_margin": dec_str(max_margin),
        }
    open_risk = sum(position_risk_at_stop(row) for row in positions)
    new_risk = dec(risk.get("estimated_loss"))
    max_risk = equity * MAX_PORTFOLIO_RISK_FRACTION
    if open_risk + new_risk > max_risk:
        return {
            "action": "open_skipped",
            "reason": "portfolio_risk_cap_reached",
            "open_risk": dec_str(open_risk),
            "new_risk": dec_str(new_risk),
            "max_portfolio_risk": dec_str(max_risk),
        }
    return None

def funding_rate_from_row(row: dict[str, Any] | None) -> Decimal:
    if not isinstance(row, dict):
        return Decimal("0")
    if row.get("funding_rate") not in (None, ""):
        return dec(row.get("funding_rate"))
    if row.get("funding_pct") not in (None, ""):
        return dec(row.get("funding_pct")) / Decimal("100")
    return Decimal("0")

def funding_periods_crossed(open_ts: Any, close_ts: Any, interval_hours: int = FUNDING_INTERVAL_HOURS) -> int:
    opened = parse_ts(open_ts)
    closed = parse_ts(close_ts)
    if not opened or not closed or closed <= opened:
        return 0
    interval = timedelta(hours=interval_hours)
    anchor = closed.replace(hour=(closed.hour // interval_hours) * interval_hours, minute=0, second=0, microsecond=0)
    while anchor > closed:
        anchor -= interval
    count = 0
    boundary = anchor
    while boundary > opened:
        count += 1
        boundary -= interval
    return count

def calculate_funding_payment(position: dict[str, Any], market_row: dict[str, Any] | None, close_ts: Any) -> Decimal:
    rate = funding_rate_from_row(market_row)
    periods = funding_periods_crossed(position.get("opened_at"), close_ts)
    if rate == 0 or periods <= 0:
        return Decimal("0")
    payment = dec(position.get("notional")) * rate * Decimal(periods)
    return -payment if str(position.get("side") or "").upper() == "LONG" else payment


def write_heartbeat(status: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    row = {"schema_version": SCHEMA_VERSION, "ts": utc_now(), "pid": os.getpid(), "status": status, **(payload or {})}
    write_json_atomic(HEARTBEAT_PATH, row)
    return row


def write_latest(row: dict[str, Any]) -> dict[str, Any]:
    payload = {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), **row, "can_place_live_orders": False}
    write_json_atomic(LATEST_PATH, payload)
    append_jsonl(HISTORY_PATH, payload)
    return payload


def load_seen(path: Path | None = None) -> dict[str, Any]:
    path = path or SEEN_PATH
    payload = read_json(path, default={})
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("risk_decision_ids", [])
    payload.setdefault("candidate_ids", [])
    return payload


def save_seen(seen: dict[str, Any], path: Path | None = None) -> None:
    path = path or SEEN_PATH
    trimmed = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "risk_decision_ids": list(dict.fromkeys(str(x) for x in seen.get("risk_decision_ids", []) if x))[-500:],
        "candidate_ids": list(dict.fromkeys(str(x) for x in seen.get("candidate_ids", []) if x))[-500:],
    }
    write_json_atomic(path, trimmed)


def market_rows(market: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key in ("hot", "top_gainers", "top_losers", "top_volume", "funding_extremes", "majors"):
        batch = market.get(key) if isinstance(market.get(key), list) else []
        rows.extend(row for row in batch if isinstance(row, dict))
    return rows


def find_market_row(symbol: str, market: dict[str, Any]) -> dict[str, Any] | None:
    target = str(symbol or "").upper()
    for row in market_rows(market):
        if str(row.get("symbol") or "").upper() == target:
            return row
    return None


def mark_candle(symbol: str, market: dict[str, Any]) -> dict[str, Any] | None:
    row = find_market_row(symbol, market)
    if not row:
        return None
    price = dec(row.get("price"))
    if price <= 0:
        return None
    ts = str(market.get("ts") or market.get("updated_at") or utc_now())
    return {
        "ts": ts,
        "open": float(price),
        "high": float(price),
        "low": float(price),
        "close": float(price),
        "volume": float(row.get("quote_volume") or 0),
        "quality": "mark_only_snapshot",
    }

def timeout_fallback_candle(position: dict[str, Any], max_hold_seconds: int = MAX_HOLD_SECONDS) -> dict[str, Any] | None:
    wall_age = seconds_between(position.get("opened_at"), utc_now())
    if wall_age is None or wall_age < max_hold_seconds:
        return None
    candles = [row for row in position.get("replay_candles", []) if isinstance(row, dict)]
    last = next((row for row in reversed(candles) if dec(row.get("close")) > 0), None)
    mark = dec((last or {}).get("close"), str(position.get("entry") or "0"))
    if mark <= 0:
        return None
    return {
        "ts": utc_now(),
        "open": float(mark),
        "high": float(mark),
        "low": float(mark),
        "close": float(mark),
        "volume": float((last or {}).get("volume") or 0),
        "quality": "stale_mark_timeout_fallback",
    }

def append_replay_candle(position: dict[str, Any], candle: dict[str, Any]) -> dict[str, Any]:
    rows = [row for row in position.get("replay_candles", []) if isinstance(row, dict)]
    normalized = {
        "ts": candle.get("ts") or utc_now(),
        "open": float(candle.get("open") or candle.get("close") or 0.0),
        "high": float(candle.get("high") or candle.get("close") or 0.0),
        "low": float(candle.get("low") or candle.get("close") or 0.0),
        "close": float(candle.get("close") or candle.get("open") or 0.0),
        "volume": float(candle.get("volume") or 0.0),
        "quality": str(candle.get("quality") or "mark_only_snapshot"),
    }
    if normalized["close"] <= 0:
        return position
    if rows and rows[-1].get("ts") == normalized["ts"]:
        rows[-1] = normalized
    else:
        rows.append(normalized)
    rows = rows[-MAX_REPLAY_CANDLES:]
    return {
        **position,
        "replay_candles": rows,
        "replay_candle_count": len(rows),
        "replay_data_quality": "mark_sequence" if len(rows) >= 3 else "mark_only_snapshot",
    }

def persist_open_position(position: dict[str, Any]) -> None:
    account = load_account()
    positions = []
    changed = False
    for row in account.get("open_positions", []) if isinstance(account.get("open_positions"), list) else []:
        if isinstance(row, dict) and row.get("position_id") == position.get("position_id"):
            positions.append(position)
            changed = True
        else:
            positions.append(row)
    if changed:
        save_account({**account, "open_positions": positions})

def build_replay_cache(position: dict[str, Any], fallback_candle: dict[str, Any]) -> dict[str, Any]:
    candles = [row for row in position.get("replay_candles", []) if isinstance(row, dict)]
    if not candles and fallback_candle:
        candles = [fallback_candle]
    if len(candles) < 3:
        return {"data_quality": "mark_only_snapshot", "replay_candle_count": len(candles)}
    cached = store_candles(
        str(position.get("symbol") or "UNKNOWN"),
        "mark_sequence",
        candles,
        source_id="paper_lifecycle_mark_sequence",
        assumptions={"quality": "observed_mark_snapshots", "not_full_ohlcv": True},
    )
    return {
        "data_quality": "mark_sequence",
        "replay_candle_count": len(candles),
        "replay_candle_cache_id": cached.get("cache_id"),
        "candle_cache_id": cached.get("cache_id"),
    }


def open_position_conflicts(account: dict[str, Any], candidate: dict[str, Any], risk: dict[str, Any]) -> bool:
    for position in account.get("open_positions", []) if isinstance(account.get("open_positions"), list) else []:
        if not isinstance(position, dict):
            continue
        if position.get("risk_decision_id") == risk.get("risk_decision_id"):
            return True
        same_symbol = str(position.get("symbol") or "").upper() == str(candidate.get("symbol") or "").upper()
        same_side = str(position.get("side") or "").upper() == str(candidate.get("side") or "").upper()
        same_setup = str(position.get("setup_id") or "") == str(candidate.get("setup_id") or "")
        if same_symbol and same_side and same_setup:
            return True
    return False


def build_open_event(position: dict[str, Any], candidate: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    ids = chart_id_fields(candidate, decision, decision.get("risk_decision") if isinstance(decision.get("risk_decision"), dict) else {})
    chart_evidence = position.get("chart_evidence") if isinstance(position.get("chart_evidence"), dict) else {}
    snapshot_ids = chart_evidence.get("chart_snapshot_ids") if isinstance(chart_evidence.get("chart_snapshot_ids"), dict) else position.get("chart_snapshot_ids") if isinstance(position.get("chart_snapshot_ids"), dict) else {}
    position_snapshot = position.get("paper_position_snapshot_v2") if isinstance(position.get("paper_position_snapshot_v2"), dict) else None
    return {
        "schema_version": SCHEMA_VERSION,
        "event": "paper_open",
        "trade_id": position.get("position_id"),
        "mode": "paper",
        "ts": position.get("opened_at") or utc_now(),
        "open_ts": position.get("opened_at") or utc_now(),
        "symbol": position.get("symbol"),
        "side": position.get("side"),
        "setup_id": position.get("setup_id"),
        "entry": position.get("entry"),
        "qty": position.get("qty"),
        "margin": position.get("margin"),
        "leverage": position.get("leverage"),
        "entry_fee": position.get("entry_fee") or "0",
        "fee": position.get("entry_fee") or "0",
        "fee_to_close_reserve": position.get("fee_to_close_reserve") or "0",
        "sl": position.get("sl"),
        "tp": position.get("tp"),
        "liquidation_price": position.get("liquidation_price"),
        "execution_assumptions": position.get("execution_assumptions"),
        "risk_decision_id": position.get("risk_decision_id"),
        "candidate_id": candidate.get("candidate_id"),
        "feature_id": candidate.get("feature_id") or decision.get("feature_id"),
        "feature_manifest_id": candidate.get("feature_manifest_id") or decision.get("feature_manifest_id"),
        "chart_score_id": position.get("chart_score_id") or ids.get("chart_score_id"),
        "chart_risk_plan_id": position.get("chart_risk_plan_id") or ids.get("chart_risk_plan_id"),
        "chart_intelligence_id": position.get("chart_intelligence_id") or ids.get("chart_intelligence_id"),
        "chart_snapshot_ids": snapshot_ids,
        "chart_evidence_status": chart_evidence.get("status"),
        "chart_learning_eligible": bool(chart_evidence.get("chart_learning_eligible")),
        "paper_position_snapshot_v2": position_snapshot,
        "decision_data_capability_mask": candidate.get("decision_data_capability_mask") or decision.get("decision_data_capability_mask"),
        "decision_regime_state": candidate.get("decision_regime_state") or decision.get("decision_regime_state"),
        "market_snapshot_ts": candidate.get("market_snapshot_ts"),
        "reasoning_id": decision.get("decided_at"),
        "status": "open",
        "position": position,
        "can_place_live_orders": False,
    }


def real_intrabar_candles(position: dict[str, Any], cutoff_ts: str, timeframe: str = EXIT_CANDLE_TIMEFRAME) -> list[dict[str, Any]]:
    """Real closed OHLC bars that finalized AFTER the position opened and no later
    than cutoff_ts. Used so exits resolve on true wick touches, not single marks.

    Returns [] on any failure (fail-open to the mark-snapshot path) — never raises.
    """
    symbol = str(position.get("symbol") or "").upper()
    opened_at = str(position.get("opened_at") or "")
    if not symbol or not opened_at:
        return []
    try:
        from chart_candle_service import load_closed_candles

        batch = load_closed_candles(symbol, timeframe, cutoff_ts, limit=EXIT_CANDLE_LIMIT)
    except Exception:
        return []
    opened_dt = parse_ts(opened_at)
    out: list[dict[str, Any]] = []
    for bar in batch.get("bars") or []:
        if bar.get("is_final") is not True:
            continue
        open_t = parse_ts(str(bar.get("open_time") or ""))
        close_t = parse_ts(str(bar.get("close_time") or bar.get("finalized_at") or ""))
        # Only bars whose ENTIRE range is after entry. Requiring open_time >=
        # opened_at (not just close_time > opened_at) prevents a bar that spans
        # the entry from firing SL/TP on a wick that occurred BEFORE the position
        # opened (Phase 1 audit m5).
        if opened_dt is not None and open_t is not None and open_t < opened_dt:
            continue
        if opened_dt is not None and close_t is not None and close_t <= opened_dt:
            continue
        try:
            o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if c <= 0:
            continue
        out.append({
            "ts": str(bar.get("close_time") or bar.get("finalized_at")),
            "open": o, "high": h, "low": l, "close": c,
            "volume": float(bar.get("volume") or 0.0),
            "quality": "real_ohlc_5m",
        })
    out.sort(key=lambda r: r.get("ts") or "")
    return out


def should_close(position: dict[str, Any], candle: dict[str, Any], max_hold_seconds: int = MAX_HOLD_SECONDS) -> dict[str, Any] | None:
    side = str(position.get("side") or "").upper()
    mark = dec(candle.get("close"))
    sl = dec(position.get("sl"))
    tp = dec(position.get("tp"))
    # Phase 1: prefer REAL intrabar OHLC (chronological) so SL/TP fire on true
    # wick touches. Fall back to the single mark candle if no real bars.
    exit_candles = real_intrabar_candles(position, str(candle.get("ts") or utc_now())) or [candle]
    # Phase 2: liquidity tier from the position's traded volume (candle volume is
    # quote_volume) drives tiered slippage/spread/MMR in the simulator.
    quote_volume = position.get("quote_volume") or candle.get("volume")
    simulated = simulate_exit(side, position.get("entry"), position.get("qty"), sl, tp, exit_candles, position.get("leverage", "1"), quote_volume=quote_volume)
    if simulated.get("status") == "closed":
        exit_price = dec(simulated.get("exit"))
        trigger = dec(simulated.get("liquidation_price")) if simulated.get("reason") == "liquidation" else tp if simulated.get("reason") == "tp" else sl
        return {
            "reason": simulated.get("reason"),
            "exit": exit_price,
            "slippage": dec_str(abs(exit_price - trigger)),
            "liquidation_price": simulated.get("liquidation_price"),
            "liquidity_tier": simulated.get("liquidity_tier"),
            "exit_price_source": "real_ohlc" if exit_candles is not [candle] and exit_candles and exit_candles[0].get("quality") == "real_ohlc_5m" else "mark_or_fallback",
            "promotion_blocked": bool(simulated.get("promotion_blocked")),
            "execution_simulator": simulated,
        }
    age = seconds_between(position.get("opened_at"), candle.get("ts"))
    wall_age = seconds_between(position.get("opened_at"), utc_now())
    if (age is not None and age >= max_hold_seconds) or (wall_age is not None and wall_age >= max_hold_seconds):
        # Phase 2: timeout is a market close -> apply tiered slippage+spread (was 0).
        tier = cost_liquidity_tier(quote_volume)
        timeout_exit = cost_exit_slippage(mark, side, cost_fill_bps(tier))
        return {"reason": "timeout", "exit": timeout_exit, "liquidity_tier": tier, "exit_price_source": "timeout_mark"}
    return None


def build_close_event(position: dict[str, Any], closed: dict[str, Any], candle: dict[str, Any]) -> dict[str, Any]:
    replay_cache = build_replay_cache({**position, **closed}, candle)
    open_chart_evidence = position.get("chart_evidence") if isinstance(position.get("chart_evidence"), dict) else {}
    close_chart_evidence = chart_snapshot_summary(
        {},
        {},
        position=position,
        stage="close",
        candle=candle,
        chart_preflight={
            "chart_used": bool(open_chart_evidence.get("chart_used") or position.get("chart_score_id") or position.get("chart_intelligence_id")),
            "chart_learning_eligible": bool(open_chart_evidence.get("chart_learning_eligible")),
            "warnings": list(open_chart_evidence.get("warnings") or []),
            "errors": list(open_chart_evidence.get("errors") or []),
            "chart_score_id": position.get("chart_score_id"),
            "chart_risk_plan_id": position.get("chart_risk_plan_id"),
            "chart_intelligence_id": position.get("chart_intelligence_id"),
        },
    )
    if isinstance(open_chart_evidence.get("source_hashes"), dict):
        close_chart_evidence["source_hashes"] = {**open_chart_evidence["source_hashes"], **close_chart_evidence.get("source_hashes", {})}
    snapshot_ids = close_chart_evidence.get("chart_snapshot_ids") if isinstance(close_chart_evidence.get("chart_snapshot_ids"), dict) else {}
    position_snapshot = position.get("paper_position_snapshot_v2") if isinstance(position.get("paper_position_snapshot_v2"), dict) else None
    return {
        "schema_version": SCHEMA_VERSION,
        "event": "paper_close",
        "trade_id": position.get("position_id"),
        "mode": "paper",
        "ts": closed.get("closed_at") or candle.get("ts") or utc_now(),
        "open_ts": position.get("opened_at"),
        "close_ts": closed.get("closed_at") or candle.get("ts") or utc_now(),
        "symbol": position.get("symbol"),
        "side": position.get("side"),
        "setup_id": position.get("setup_id"),
        "entry": position.get("entry"),
        "exit": closed.get("exit"),
        "qty": position.get("qty"),
        "margin": position.get("margin"),
        "leverage": position.get("leverage"),
        "sl": position.get("sl"),
        "tp": position.get("tp"),
        "entry_fee": closed.get("entry_fee") or position.get("entry_fee") or "0",
        "exit_fee": closed.get("exit_fee") or "0",
        "fee": closed.get("fee") or "0",
        "fees": closed.get("fees") or closed.get("fee") or "0",
        "funding_payment": closed.get("funding_payment") or "0",
        "net_before_funding": closed.get("net_before_funding"),
        "slippage": closed.get("slippage") or "0",
        "liquidation_price": closed.get("liquidation_price"),
        "promotion_blocked": bool(closed.get("promotion_blocked")),
        "gross": closed.get("gross"),
        "net": closed.get("net"),
        "reason": closed.get("reason"),
        "execution_assumptions": closed.get("execution_assumptions") or position.get("execution_assumptions"),
        "status": "closed",
        "risk_decision_id": position.get("risk_decision_id"),
        "candidate_id": position.get("candidate_id"),
        "feature_id": position.get("feature_id"),
        "feature_manifest_id": position.get("feature_manifest_id"),
        "chart_score_id": close_chart_evidence.get("chart_score_id") or position.get("chart_score_id"),
        "chart_risk_plan_id": close_chart_evidence.get("chart_risk_plan_id") or position.get("chart_risk_plan_id"),
        "chart_intelligence_id": close_chart_evidence.get("chart_intelligence_id") or position.get("chart_intelligence_id"),
        "chart_snapshot_ids": snapshot_ids,
        "chart_evidence_status": close_chart_evidence.get("status"),
        "chart_learning_eligible": bool(close_chart_evidence.get("chart_learning_eligible")),
        "chart_evidence": close_chart_evidence,
        "paper_position_snapshot_v2": position_snapshot,
        "market_snapshot_ts": candle.get("ts"),
        "data_quality": replay_cache.get("data_quality") or candle.get("quality"),
        "replay_candle_count": replay_cache.get("replay_candle_count"),
        "replay_candle_cache_id": replay_cache.get("replay_candle_cache_id"),
        "candle_cache_id": replay_cache.get("candle_cache_id"),
        "position": {**position, **closed, **replay_cache, "chart_evidence": close_chart_evidence, "chart_snapshot_ids": snapshot_ids},
        "can_place_live_orders": False,
    }


def try_open_latest_decision(account: dict[str, Any], market: dict[str, Any] | None = None) -> dict[str, Any] | None:
    latest = read_json(DECISION_LATEST, default={})
    decision = latest.get("decision") if isinstance(latest.get("decision"), dict) else {}
    if decision.get("action") != "paper_open_candidate":
        return None
    candidate = decision.get("candidate") if isinstance(decision.get("candidate"), dict) else {}
    risk = decision.get("risk_decision") if isinstance(decision.get("risk_decision"), dict) else {}
    if not risk.get("can_open_paper"):
        return {"action": "open_skipped", "reason": "risk_decision_rejected", "risk_decision_id": risk.get("risk_decision_id")}
    seen = load_seen()
    candidate_id = str(candidate.get("candidate_id") or "")
    risk_id = str(risk.get("risk_decision_id") or "")
    if risk_id in set(seen.get("risk_decision_ids") or []) or candidate_id in set(seen.get("candidate_ids") or []):
        return {"action": "open_skipped", "reason": "decision_already_consumed", "risk_decision_id": risk_id, "candidate_id": candidate_id}
    snapshot_reject_reason = market_snapshot_reject_reason(candidate)
    if snapshot_reject_reason:
        return {"action": "open_skipped", "reason": snapshot_reject_reason, "risk_decision_id": risk_id, "candidate_id": candidate_id}
    chart_preflight = chart_preflight_for_candidate(candidate, decision, reference_ts=decision.get("decided_at") or utc_now())
    if chart_preflight.get("reject_open"):
        return {
            "action": "open_skipped",
            "reason": chart_preflight.get("reason") or "chart_preflight_rejected",
            "risk_decision_id": risk_id,
            "candidate_id": candidate_id,
            "chart_preflight": chart_preflight,
        }
    executed_risk, entry_execution = apply_entry_execution(risk, candidate, market or {})
    portfolio_reject = portfolio_open_reject_reason(account, executed_risk)
    if portfolio_reject:
        return {**portfolio_reject, "risk_decision_id": risk_id, "candidate_id": candidate_id}
    if str(candidate.get("producer_id") or candidate.get("source") or "") == "paper_candidate_feeder" and not (candidate.get("feature_id") or decision.get("feature_id")):
        return {"action": "open_skipped", "reason": "missing_feature_row_id", "risk_decision_id": risk_id, "candidate_id": candidate_id}
    if open_position_conflicts(account, candidate, risk):
        return {"action": "open_skipped", "reason": "matching_position_already_open", "risk_decision_id": risk_id, "candidate_id": candidate_id}
    entry_fee = calculate_entry_fee(executed_risk)
    opened = open_paper_position(executed_risk, account=account, entry_fee=entry_fee)
    if not opened.get("ok"):
        return {"action": "open_failed", "reason": opened.get("reason"), "risk_decision_id": risk_id, "candidate_id": candidate_id}
    position = opened["position"]
    open_chart_evidence = chart_snapshot_summary(candidate, decision, position=position, stage="open", chart_preflight=chart_preflight)
    position = {
        **position,
        "candidate_id": candidate_id,
        "feature_id": candidate.get("feature_id") or decision.get("feature_id"),
        "feature_manifest_id": candidate.get("feature_manifest_id") or decision.get("feature_manifest_id"),
        "feature_artifact_digest": candidate.get("feature_artifact_digest"),
        "decision_data_capability_mask": candidate.get("decision_data_capability_mask") or decision.get("decision_data_capability_mask"),
        "decision_regime_state": candidate.get("decision_regime_state") or decision.get("decision_regime_state"),
        "chart_score_id": open_chart_evidence.get("chart_score_id"),
        "chart_risk_plan_id": open_chart_evidence.get("chart_risk_plan_id"),
        "chart_intelligence_id": open_chart_evidence.get("chart_intelligence_id"),
        "chart_snapshot_ids": open_chart_evidence.get("chart_snapshot_ids"),
        "chart_evidence": open_chart_evidence,
        "entry_execution": entry_execution,
        "execution_assumptions": {
            **(position.get("execution_assumptions") if isinstance(position.get("execution_assumptions"), dict) else {}),
            "entry": entry_execution,
            "slippage_model": "adverse_bps_v1",
            "liquidation_model": "maintenance_margin_v1",
            "price_basis": entry_execution.get("price_basis"),
        },
    }
    position["paper_position_snapshot_v2"] = build_paper_position_snapshot_v2(position, candidate, decision, chart_preflight, opened.get("account") if isinstance(opened.get("account"), dict) else account, open_chart_evidence)
    entry_candle = mark_candle(str(position.get("symbol") or ""), market or {})
    if entry_candle:
        position = append_replay_candle(position, entry_candle)
    persist_open_position(position)
    event = build_open_event(position, candidate, decision)
    append_jsonl_once(PAPER_TRADES_PATH, event, "trade_id")
    seen["risk_decision_ids"] = list(seen.get("risk_decision_ids") or []) + [risk_id]
    if candidate_id:
        seen["candidate_ids"] = list(seen.get("candidate_ids") or []) + [candidate_id]
    save_seen(seen)
    return {"action": "opened", "event": event, "account": load_account(), "can_place_live_orders": False}


def monitor_open_positions(account: dict[str, Any], market: dict[str, Any], max_hold_seconds: int = MAX_HOLD_SECONDS) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for position in list(account.get("open_positions", []) if isinstance(account.get("open_positions"), list) else []):
        if not isinstance(position, dict):
            continue
        candle = mark_candle(str(position.get("symbol") or ""), market)
        if not candle:
            candle = timeout_fallback_candle(position, max_hold_seconds=max_hold_seconds)
            if not candle:
                results.append({"action": "monitor_wait", "position_id": position.get("position_id"), "reason": "missing_mark_price"})
                continue
            position = append_replay_candle(position, candle)
            # Phase 2: this fallback timeout is still a market close -> apply
            # tiered slippage (was 0, a hidden optimism vector).
            _tier = cost_liquidity_tier(position.get("quote_volume") or candle.get("volume"))
            _to_exit = cost_exit_slippage(dec(candle.get("close")), str(position.get("side") or ""), cost_fill_bps(_tier))
            close_plan = {"reason": "missing_mark_price_timeout", "exit": _to_exit, "liquidity_tier": _tier, "exit_price_source": "timeout_fallback"}
        else:
            position = append_replay_candle(position, candle)
            close_plan = should_close(position, candle, max_hold_seconds=max_hold_seconds)
        if not close_plan:
            persist_open_position(position)
            results.append({"action": "monitor_hold", "position_id": position.get("position_id"), "symbol": position.get("symbol"), "mark": candle.get("close")})
            continue
        persist_open_position(position)
        close_ts = candle.get("ts") or utc_now()
        fee = abs(close_plan["exit"] * dec(position.get("qty")) * TAKER_FEE_RATE)
        funding_payment = calculate_funding_payment(position, find_market_row(str(position.get("symbol") or ""), market), close_ts)
        closed_result = close_paper_position(str(position.get("position_id")), close_plan["exit"], fee=fee, reason=str(close_plan["reason"]), funding_payment=funding_payment)
        if not closed_result.get("ok"):
            results.append({"action": "close_failed", "position_id": position.get("position_id"), "reason": closed_result.get("reason")})
            continue
        closed_position = {
            **closed_result["position"],
            "slippage": close_plan.get("slippage") or "0",
            "liquidation_price": close_plan.get("liquidation_price"),
            "promotion_blocked": bool(close_plan.get("promotion_blocked")),
            "execution_simulator": close_plan.get("execution_simulator"),
        }
        close_event = build_close_event(position, closed_position, candle)
        append_jsonl(PAPER_TRADES_PATH, close_event)
        review_candles = [row for row in close_event.get("position", {}).get("replay_candles", []) if isinstance(row, dict)] or [candle]
        review = review_closed_trade(close_event, review_candles, setup_score={"score": 0.6}, append=True)
        account = closed_result["account"]
        results.append({"action": "closed", "event": close_event, "review": review, "account": account, "can_place_live_orders": False})
    return results


def run_once(max_hold_seconds: int = MAX_HOLD_SECONDS) -> dict[str, Any]:
    live_gate = evaluate_live_permission({"action": "paper_execution_lifecycle", "mode": "paper"})
    if not paper_action_allowed(live_gate):
        row = write_latest({"status": "blocked", "action": "skip", "reason": "live_firewall_block", "live_gate": live_gate})
        write_heartbeat("blocked", {"reason": "live_firewall_block"})
        return row
    account = load_account()
    market = read_json(MARKET_LATEST, default={})
    monitor_results = monitor_open_positions(account, market, max_hold_seconds=max_hold_seconds)
    account_after_monitor = load_account()
    open_result = try_open_latest_decision(account_after_monitor, market=market)
    account_for_validation = load_account()
    lifecycle_report = write_latest_report(paths=[PAPER_TRADES_PATH], min_open_ts=account_for_validation.get("created_at"))
    actions = [row.get("action") for row in monitor_results]
    if open_result:
        actions.append(str(open_result.get("action")))
    status = "ok" if lifecycle_report.get("learning_allowed") else "degraded"
    row = write_latest(
        {
            "status": status,
            "action": "lifecycle_tick",
            "actions": actions,
            "monitor_results": monitor_results[-10:],
            "open_result": open_result,
            "open_positions": len(load_account().get("open_positions") or []),
            "lifecycle": lifecycle_report,
        }
    )
    write_heartbeat(status, {"actions": actions[-5:], "open_positions": row.get("open_positions")})
    return row


def interruptible_sleep(seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline and not STOP_FILE.exists():
        write_heartbeat("waiting", {"next_run_seconds": round(max(0.0, deadline - time.time()), 1)})
        time.sleep(min(30.0, max(0.0, deadline - time.time())))


def read_pid(path: Path = PID_FILE) -> int | None:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except Exception:
        return None


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run paper-only execution lifecycle loop")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=30.0)
    parser.add_argument("--max-hold-seconds", type=int, default=MAX_HOLD_SECONDS)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be positive")
    if args.max_hold_seconds <= 0:
        parser.error("--max-hold-seconds must be positive")
    return args


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.status:
        print({"pid": read_pid(), "latest": str(LATEST_PATH), "heartbeat": str(HEARTBEAT_PATH), "stop_file": str(STOP_FILE)})
        return 0
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    while not STOP_FILE.exists():
        row = run_once(max_hold_seconds=args.max_hold_seconds)
        print(f"paper_execution_lifecycle_loop status={row.get('status')} actions={','.join(row.get('actions') or [])}", flush=True)
        if args.once:
            break
        interruptible_sleep(args.interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
