"""Canonical paper/shadow edge scoring board.

This module is deliberately deterministic and paper-only. It scores closed,
mature outcomes after execution costs and uncertainty, then emits immutable
snapshots for readiness gates.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import timedelta
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, read_json, read_jsonl, write_json_atomic
from timebase import parse_utc, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
PAPER_TRADES = MEMORY_DIR / "paper_trades.jsonl"
SHADOW_CLOSES = MEMORY_DIR / "shadow_closes.jsonl"
CANDIDATE_HISTORY = MEMORY_DIR / "paper_candidate_feeder_history.jsonl"
OPERATING_COSTS_LATEST = MEMORY_DIR / "operating_costs_latest.json"
LATEST_JSON = MEMORY_DIR / "real_scoring_board_latest.json"
HISTORY_JSONL = MEMORY_DIR / "real_scoring_board_history.jsonl"

METRIC_MANIFEST_VERSION = "real_scoring_board.v1"
DEFAULT_BOOTSTRAP_BLOCK_SIZE = 5
DEFAULT_MIN_EFFECTIVE_N = 20
DEFAULT_SOURCE_TRUST_FLOOR = 0.75

def canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str, separators=(",", ":"))

def canonical_hash(payload: Any, prefix: str) -> str:
    return f"{prefix}_" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:20]

def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default

def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except Exception:
        return default

def optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except Exception:
        return None

def dt_key(value: Any) -> str:
    parsed = parse_utc(value)
    return parsed.date().isoformat() if parsed else "unknown"

def is_closed_trade_row(row: dict[str, Any], source: str) -> bool:
    event = str(row.get("event") or "").lower()
    status = str(row.get("status") or "").lower()
    if event:
        if source == "paper":
            return event in {"paper_close", "paper_trade_close", "trade_close", "close"}
        return event in {"shadow_close", "trade_close", "close"} or status == "closed"
    if status and status != "closed":
        return False
    return bool(row.get("close_ts") or row.get("closed_at") or row.get("net") is not None)

def nested(row: dict[str, Any], key: str) -> dict[str, Any]:
    value = row.get(key)
    return value if isinstance(value, dict) else {}

def first_present(*values: Any, default: Any = None) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return default

def normalize_trade(row: dict[str, Any], source: str = "paper") -> dict[str, Any]:
    position = nested(row, "position")
    costs = nested(row, "costs")
    signal = nested(row, "signal")
    order_plan = nested(row, "order_plan")
    net = safe_float(row.get("net"), safe_float(costs.get("net")))
    gross = safe_float(row.get("gross"), net + safe_float(row.get("fee")) + safe_float(row.get("funding_payment")) + safe_float(row.get("slippage")))
    ts = row.get("close_ts") or row.get("closed_at") or row.get("ts")
    capability = row.get("decision_data_capability_mask") if isinstance(row.get("decision_data_capability_mask"), dict) else {}
    return {
        "event_seq": optional_int(row.get("event_seq") if "event_seq" in row else row.get("seq")),
        "trade_id": str(first_present(row.get("trade_id"), row.get("paper_trade_id"), row.get("shadow_id"), row.get("signal_id"), default="")),
        "paper_trade_id": str(first_present(row.get("paper_trade_id"), position.get("paper_trade_id"), default="")),
        "shadow_id": str(first_present(row.get("shadow_id"), default="")),
        "signal_id": str(first_present(row.get("signal_id"), signal.get("signal_id"), order_plan.get("signal_id"), default="")),
        "candidate_id": str(first_present(row.get("candidate_id"), position.get("candidate_id"), signal.get("candidate_id"), order_plan.get("candidate_id"), default="")),
        "risk_decision_id": str(first_present(row.get("risk_decision_id"), position.get("risk_decision_id"), signal.get("risk_decision_id"), order_plan.get("risk_decision_id"), default="")),
        "close_id": str(first_present(row.get("close_id"), default="")),
        "setup_id": str(first_present(row.get("setup_id"), position.get("setup_id"), signal.get("setup_id"), order_plan.get("setup_id"), default="unknown")),
        "setup_contract_hash": str(row.get("setup_contract_hash") or position.get("setup_contract_hash") or "unknown"),
        "side": str(first_present(row.get("side"), position.get("side"), signal.get("side"), order_plan.get("side"), default="unknown")).upper(),
        "symbol": str(first_present(row.get("symbol"), position.get("symbol"), signal.get("symbol"), order_plan.get("symbol"), default="unknown")).upper(),
        "regime": str(first_present(row.get("market_regime"), row.get("regime"), position.get("market_regime"), signal.get("market_regime"), signal.get("regime"), order_plan.get("market_regime"), order_plan.get("regime"), default="unknown")),
        "source": str(row.get("source") or source),
        "source_trust": max(0.0, min(1.0, safe_float(row.get("source_trust"), 1.0))),
        "net": net,
        "gross": gross,
        "entry": safe_float(row.get("entry") or row.get("fill_price")),
        "fill_price": safe_float(row.get("fill_price") or row.get("entry")),
        "fee": safe_float(row.get("fee"), safe_float(row.get("fees"))),
        "funding_payment": safe_float(row.get("funding_payment")),
        "slippage": safe_float(row.get("slippage")),
        "mae": row.get("mae"),
        "mfe": row.get("mfe"),
        "close_ts": ts,
        "outcome_known_at": row.get("outcome_known_at") or row.get("label_end_at") or ts,
        "open_ts": row.get("open_ts") or row.get("opened_at") or ts,
        "capital_event_id": row.get("capital_event_id") or row.get("account_epoch") or "default",
        "invalid_open": bool(row.get("invalid_open")),
        "liquidation": bool(row.get("liquidation") or str(row.get("reason") or "").lower() == "liquidation"),
        "capability_mask": capability,
        "blind_trade": bool(row.get("blind_trade") or capability.get("required_missing")),
        "size_capped": bool(row.get("size_capped") or capability.get("action") == "size_cap"),
        "costs_complete": all(key in row or key in costs for key in ("fee", "funding_payment", "slippage")),
        "is_correction_event": bool(row.get("is_correction_event") or row.get("supersedes_replay_id") or row.get("supersedes_trade_id")),
    }

def mature_rows(rows: list[dict[str, Any]], report_cutoff: str) -> tuple[list[dict[str, Any]], list[str]]:
    cutoff = parse_utc(report_cutoff)
    mature: list[dict[str, Any]] = []
    censored: list[str] = []
    for row in rows:
        known = parse_utc(row.get("outcome_known_at"))
        if cutoff and known and known <= cutoff:
            mature.append(row)
        else:
            censored.append(row.get("trade_id") or row.get("close_ts") or "unknown")
    return mature, censored

def confidence_interval(values: list[float], block_size: int = DEFAULT_BOOTSTRAP_BLOCK_SIZE) -> dict[str, Any]:
    n = len(values)
    mean = sum(values) / n if n else 0.0
    if n < 2:
        stderr = 0.0
    else:
        variance = sum((value - mean) ** 2 for value in values) / (n - 1)
        stderr = math.sqrt(variance) / math.sqrt(n)
    return {
        "method": "normal_lcb95_with_block_bootstrap_params",
        "block_bootstrap": {"block_size": block_size, "resamples": 0, "deterministic_normal_fallback": True},
        "mean": round(mean, 8),
        "stderr": round(stderr, 8),
        "lower_95": round(mean - 1.96 * stderr, 8),
        "upper_95": round(mean + 1.96 * stderr, 8),
    }

def drawdown_path(values: list[float]) -> tuple[list[dict[str, Any]], float]:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    path: list[dict[str, Any]] = []
    for idx, value in enumerate(values):
        equity += value
        peak = max(peak, equity)
        dd = equity - peak
        max_dd = min(max_dd, dd)
        path.append({"i": idx, "equity": round(equity, 8), "drawdown": round(abs(dd), 8)})
    return path, round(abs(max_dd), 8)

def effective_sample_size(rows: list[dict[str, Any]]) -> dict[str, Any]:
    raw_n = len(rows)
    unique_days = len({dt_key(row.get("close_ts")) for row in rows})
    unique_symbols = len({row.get("symbol") for row in rows})
    unique_regimes = len({row.get("regime") for row in rows})
    unique_sources = len({row.get("source") for row in rows})
    capped = min(raw_n, unique_days * 5 if unique_days else raw_n, unique_symbols * 10 if unique_symbols else raw_n)
    source_weighted = sum(safe_float(row.get("source_trust"), 1.0) for row in rows)
    eff_n = min(float(capped), source_weighted)
    return {
        "raw_n": raw_n,
        "effective_n": round(eff_n, 4),
        "unique_days": unique_days,
        "unique_symbols": unique_symbols,
        "unique_regimes": unique_regimes,
        "unique_sources": unique_sources,
        "method": "per_day_symbol_cap_source_trust",
    }

def score_bucket(rows: list[dict[str, Any]], *, min_effective_n: int = DEFAULT_MIN_EFFECTIVE_N) -> dict[str, Any]:
    nets = [safe_float(row.get("net")) for row in rows]
    gross = [safe_float(row.get("gross")) for row in rows]
    wins = [value for value in nets if value > 0]
    losses = [value for value in nets if value < 0]
    fees = sum(abs(safe_float(row.get("fee"))) for row in rows)
    funding = sum(abs(safe_float(row.get("funding_payment"))) for row in rows)
    slippage = sum(abs(safe_float(row.get("slippage"))) for row in rows)
    all_costs = fees + funding + slippage
    gross_abs = sum(abs(value) for value in gross)
    ci = confidence_interval(nets)
    path, max_dd = drawdown_path(nets)
    eff = effective_sample_size(rows)
    mae_mfe_covered = sum(1 for row in rows if row.get("mae") is not None and row.get("mfe") is not None)
    invalid_opens = sum(1 for row in rows if row.get("invalid_open"))
    cost_complete = all(row.get("costs_complete") for row in rows) if rows else False
    blind_trades = sum(1 for row in rows if row.get("blind_trade"))
    size_capped = sum(1 for row in rows if row.get("size_capped"))
    metric = {
        "trades": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(rows), 4) if rows else 0.0,
        "win_rate_diagnostic_only": True,
        "net": round(sum(nets), 8),
        "gross": round(sum(gross), 8),
        "expectancy_after_costs": round(sum(nets) / len(rows), 8) if rows else 0.0,
        "expectancy_lower_bound_95": ci["lower_95"],
        "profit_factor_after_costs": round(sum(wins) / abs(sum(losses)), 4) if losses else (999.0 if wins else 0.0),
        "avg_win": round(sum(wins) / len(wins), 8) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 8) if losses else 0.0,
        "payoff_ratio": round((sum(wins) / len(wins)) / abs(sum(losses) / len(losses)), 4) if wins and losses else 0.0,
        "confidence_interval": ci,
        "effective_sample": eff,
        "max_drawdown": max_dd,
        "drawdown_path": path,
        "fee_drag_pct": round(all_costs / max(1e-9, gross_abs + all_costs), 6),
        "fees": round(fees, 8),
        "funding_abs": round(funding, 8),
        "slippage_abs": round(slippage, 8),
        "mae_mfe_coverage_pct": round(mae_mfe_covered / len(rows), 4) if rows else 0.0,
        "invalid_opens": invalid_opens,
        "liquidations": sum(1 for row in rows if row.get("liquidation")),
        "cost_completeness": cost_complete,
        "blind_trade_count": blind_trades,
        "size_capped_count": size_capped,
        "source_trust_weighted_coverage": round(sum(safe_float(row.get("source_trust"), 1.0) for row in rows) / len(rows), 4) if rows else 0.0,
    }
    errors: list[str] = []
    if metric["expectancy_after_costs"] <= 0:
        errors.append("expectancy_not_positive_after_costs")
    if metric["expectancy_lower_bound_95"] <= 0:
        errors.append("expectancy_lcb_not_positive")
    if safe_float(metric["profit_factor_after_costs"]) < 1.15:
        errors.append("profit_factor_below_gate")
    if safe_float(eff["effective_n"]) < min_effective_n:
        errors.append("effective_n_below_gate")
    if not cost_complete:
        errors.append("execution_cost_completeness_missing")
    if metric["mae_mfe_coverage_pct"] < 0.8 and rows:
        errors.append("mae_mfe_coverage_low")
    if invalid_opens:
        errors.append("invalid_opens_present")
    if blind_trades:
        errors.append("blind_capability_trades_present")
    metric["passed"] = not errors
    metric["errors"] = errors
    return metric

def group_by(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(str(row.get(key) or "unknown"), []).append(row)
    return buckets

def candidate_census_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counters = {"seen": 0, "ranked": 0, "skipped": 0, "expired": 0, "selected": 0, "missed": 0, "closed": 0}
    for payload in rows:
        candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
        counters["seen"] += len(candidates)
        for candidate in candidates:
            state = str(candidate.get("state") or candidate.get("status") or "ranked")
            if state in counters:
                counters[state] += 1
            else:
                counters["ranked"] += 1
            if candidate.get("missed") or state == "missed":
                counters["missed"] += 1
            if candidate.get("selected") or state == "selected":
                counters["selected"] += 1
    seen = max(1, counters["seen"])
    return {**counters, "missed_candidate_rate": round(counters["missed"] / seen, 4), "no_trade_opportunity_cost": 0.0}

def concordance_keys(row: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for field in ("risk_decision_id", "candidate_id", "paper_trade_id", "signal_id", "trade_id", "shadow_id"):
        value = str(row.get(field) or "")
        if value:
            keys.append(f"{field}:{value}")
    return keys

def paper_shadow_concordance(paper_rows: list[dict[str, Any]], shadow_rows: list[dict[str, Any]], tolerance: dict[str, Any] | None = None) -> dict[str, Any]:
    tolerance = {"fill_bps": 25.0, "timing_seconds": 120.0, "unmatched_rate": 0.1, **(tolerance or {})}
    if not shadow_rows:
        return {
            "matched": 0,
            "comparable_pairs": 0,
            "paper_rows": len(paper_rows),
            "shadow_rows": 0,
            "unmatched_rate": 0.0,
            "max_fill_bps_error": 0.0,
            "max_timing_lag_seconds": 0.0,
            "mismatches": [],
            "passed": True,
            "errors": [],
            "tolerance": tolerance,
            "mode": "no_shadow_corpus",
        }
    shadow_by_key: dict[str, dict[str, Any]] = {}
    for shadow in shadow_rows:
        for key in concordance_keys(shadow):
            shadow_by_key.setdefault(key, shadow)
    matched = 0
    comparable = len(paper_rows)
    mismatches: list[str] = []
    fill_errors: list[float] = []
    timing_errors: list[float] = []
    for row in paper_rows:
        keys = concordance_keys(row)
        other = next((shadow_by_key[key] for key in keys if key in shadow_by_key), None)
        if not other:
            mismatches.append(f"unmatched:{keys[0] if keys else row.get('trade_id') or 'unknown'}")
            continue
        matched += 1
        key = keys[0] if keys else str(row.get("trade_id") or "unknown")
        entry = safe_float(row.get("entry") or row.get("fill_price") or 0)
        shadow_entry = safe_float(other.get("entry") or other.get("fill_price") or entry)
        if entry:
            fill_errors.append(abs(shadow_entry - entry) / entry * 10000)
        a, b = parse_utc(row.get("close_ts") or row.get("ts")), parse_utc(other.get("close_ts") or other.get("ts"))
        if a and b:
            timing_errors.append(abs((a - b).total_seconds()))
        for field in ("side", "setup_id", "regime"):
            if row.get(field) and other.get(field) and str(row.get(field)) != str(other.get(field)):
                mismatches.append(f"{field}_parity:{key}")
    total = max(1, comparable)
    unmatched = comparable - matched
    unmatched_rate = round(unmatched / total, 4)
    max_fill = max(fill_errors, default=0.0)
    max_timing = max(timing_errors, default=0.0)
    errors = []
    if unmatched_rate > safe_float(tolerance["unmatched_rate"]):
        errors.append("shadow_unmatched_rate_above_tolerance")
    if max_fill > safe_float(tolerance["fill_bps"]):
        errors.append("shadow_fill_error_above_tolerance")
    if max_timing > safe_float(tolerance["timing_seconds"]):
        errors.append("shadow_timing_error_above_tolerance")
    parity = [item for item in mismatches if "_parity:" in item]
    if parity:
        errors.append("paper_shadow_parity_mismatch")
    return {
        "matched": matched,
        "comparable_pairs": comparable,
        "paper_rows": len(paper_rows),
        "shadow_rows": len(shadow_rows),
        "unmatched_rate": unmatched_rate,
        "max_fill_bps_error": round(max_fill, 4),
        "max_timing_lag_seconds": round(max_timing, 4),
        "mismatches": mismatches[:50],
        "passed": not errors,
        "errors": errors,
        "tolerance": tolerance,
    }

def load_operating_costs(path: Path = OPERATING_COSTS_LATEST) -> dict[str, Any]:
    payload = read_json(path, default={})
    if not payload:
        return {}
    costs = payload.get("costs") if isinstance(payload.get("costs"), dict) else payload
    return {str(key): safe_float(value) for key, value in costs.items() if key not in {"updated_at", "schema_version", "source"}}

def stress_score_pack(metrics: dict[str, Any], exposures: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = {"max_stressed_loss": 0.2, "btc_eth_crash": -0.12, "alt_beta_shock": -0.25, "spread_multiplier": 5, "funding_shock": 0.01, **(params or {})}
    exposures = exposures or {}
    beta = safe_float(exposures.get("portfolio_beta"), 1.0)
    concentration = safe_float(exposures.get("cluster_concentration"), 0.0)
    base_loss = abs(safe_float(params["btc_eth_crash"])) * beta + max(0.0, concentration - 0.5) * 0.2
    spread_loss = safe_float(metrics.get("slippage_abs")) * (safe_float(params["spread_multiplier"]) - 1)
    funding_loss = safe_float(params["funding_shock"]) * max(1, safe_int(metrics.get("trades")))
    stressed_loss = round(base_loss + spread_loss + funding_loss, 8)
    errors = []
    if stressed_loss > safe_float(params["max_stressed_loss"]):
        errors.append("stress_loss_breaches_gate")
    return {"params": params, "stressed_loss": stressed_loss, "passed": not errors, "errors": errors}

def metric_manifest() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "manifest_version": METRIC_MANIFEST_VERSION,
        "formulas": {
            "expectancy_after_costs": "sum(net_after_fee_funding_slippage)/trades",
            "pf_after_costs": "sum(wins)/abs(sum(losses))",
            "lcb95": "mean - 1.96*stderr, deterministic normal fallback with bootstrap params persisted",
            "effective_n": "min(raw_n, day cap, symbol cap, source trust weight)",
        },
        "denominators": ["mature closed rows at report cutoff", "candidate census rows", "shadow concordance rows"],
        "ci_params": {"method": "normal_lcb95_with_block_bootstrap_params", "block_size": DEFAULT_BOOTSTRAP_BLOCK_SIZE},
        "stress_params": {"btc_eth_crash": -0.12, "alt_beta_shock": -0.25, "spread_multiplier": 5},
        "readiness_gate": {"min_effective_n": DEFAULT_MIN_EFFECTIVE_N, "min_pf": 1.15, "lcb_must_be_positive": True},
        "digest": canonical_hash({"version": METRIC_MANIFEST_VERSION, "min_effective_n": DEFAULT_MIN_EFFECTIVE_N}, "metric"),
    }

def score_all(
    paper_rows: list[dict[str, Any]],
    shadow_rows: list[dict[str, Any]] | None = None,
    candidate_rows: list[dict[str, Any]] | None = None,
    *,
    as_of: str | None = None,
    report_cutoff: str | None = None,
    previous_snapshot: dict[str, Any] | None = None,
    exposures: dict[str, Any] | None = None,
    operating_costs: dict[str, Any] | None = None,
    require_operating_costs: bool = False,
) -> dict[str, Any]:
    as_of = as_of or utc_now()
    report_cutoff = report_cutoff or as_of
    source_rows = [row for row in paper_rows if isinstance(row, dict) and is_closed_trade_row(row, "paper")]
    normalized = [normalize_trade(row, source="paper") for row in source_rows]
    mature, censored = mature_rows(normalized, report_cutoff)
    overall = score_bucket(mature)
    by_setup = {key: score_bucket(rows) for key, rows in group_by(mature, "setup_id").items()}
    by_regime = {key: score_bucket(rows) for key, rows in group_by(mature, "regime").items()}
    by_source = {key: score_bucket(rows) for key, rows in group_by(mature, "source").items()}
    by_symbol = {key: score_bucket(rows) for key, rows in group_by(mature, "symbol").items()}
    by_side = {key: score_bucket(rows) for key, rows in group_by(mature, "side").items()}
    by_contract = {key: score_bucket(rows) for key, rows in group_by(mature, "setup_contract_hash").items()}
    by_capital_event = {key: score_bucket(rows) for key, rows in group_by(mature, "capital_event_id").items()}
    shadow_norm = [normalize_trade(row, source="shadow") for row in (shadow_rows or []) if isinstance(row, dict) and is_closed_trade_row(row, "shadow")]
    concordance = paper_shadow_concordance(mature, shadow_norm) if shadow_rows is not None else {"passed": True, "errors": [], "matched": 0}
    candidates = candidate_census_metrics(candidate_rows or [])
    stress = stress_score_pack(overall, exposures)
    manifest = metric_manifest()
    spend = sum(safe_float(value) for value in (operating_costs or {}).values())
    spend_adjusted_expectancy = round((overall["net"] - spend) / max(1, overall["trades"]), 8)
    hard_errors = list(overall["errors"])
    if not concordance.get("passed"):
        hard_errors.extend(concordance.get("errors") or [])
    if not stress.get("passed"):
        hard_errors.extend(stress.get("errors") or [])
    if spend_adjusted_expectancy < 0:
        hard_errors.append("spend_adjusted_expectancy_negative")
    if require_operating_costs and not operating_costs:
        hard_errors.append("operating_costs_missing")
    for setup_id, metric in by_setup.items():
        if metric["trades"] >= 5 and not metric["passed"]:
            hard_errors.append(f"setup_bucket_failed:{setup_id}")
    mature_event_seqs = [row.get("event_seq") for row in mature if row.get("event_seq") is not None]
    included_event_seq_max = max(mature_event_seqs, default=None)
    outcome_known_at_max = max([str(row.get("outcome_known_at") or "") for row in mature], default=None)
    snapshot_core = {
        "schema_version": SCHEMA_VERSION,
        "as_of": as_of,
        "report_cutoff": report_cutoff,
        "included_event_seq_max": included_event_seq_max,
        "scored_closed_rows": len(source_rows),
        "outcome_known_at_max": outcome_known_at_max,
        "metric_manifest_digest": manifest["digest"],
        "overall": overall,
        "by_setup": by_setup,
        "by_regime": by_regime,
        "by_source": by_source,
        "by_symbol": by_symbol,
        "by_side": by_side,
        "by_setup_contract_hash": by_contract,
        "by_capital_event": by_capital_event,
        "candidate_census": candidates,
        "paper_shadow_concordance": concordance,
        "stress_score": stress,
        "operating_costs": operating_costs or {},
        "spend_adjusted_expectancy": spend_adjusted_expectancy,
        "censored_labels": censored,
        "metric_manifest": manifest,
        "hard_errors": sorted(set(hard_errors)),
        "passed": not hard_errors,
        "can_place_live_orders": False,
    }
    previous_seq_max = optional_int(previous_snapshot.get("included_event_seq_max")) if previous_snapshot else None
    has_prior_seq_correction = previous_seq_max is not None and any(row.get("event_seq") is not None and row.get("event_seq") <= previous_seq_max for row in mature)
    has_explicit_correction = any(row.get("is_correction_event") for row in mature)
    if previous_snapshot and (has_prior_seq_correction or has_explicit_correction):
        snapshot_core["correction_of_snapshot"] = previous_snapshot.get("snapshot_id")
        snapshot_core["correction_reason"] = "late_or_corrected_outcome_in_prior_seq_range"
    snapshot_core["snapshot_hash"] = canonical_hash(snapshot_core, "score")
    snapshot_core["snapshot_id"] = snapshot_core["snapshot_hash"]
    return snapshot_core

def run_once(
    paper_path: Path = PAPER_TRADES,
    shadow_path: Path = SHADOW_CLOSES,
    candidate_path: Path = CANDIDATE_HISTORY,
    latest_path: Path = LATEST_JSON,
    history_path: Path = HISTORY_JSONL,
    operating_costs_path: Path = OPERATING_COSTS_LATEST,
) -> dict[str, Any]:
    previous = read_json(latest_path, default={})
    result = score_all(
        read_jsonl(paper_path),
        read_jsonl(shadow_path),
        read_jsonl(candidate_path),
        previous_snapshot=previous,
        operating_costs=load_operating_costs(operating_costs_path),
        require_operating_costs=True,
    )
    write_json_atomic(latest_path, result)
    append_jsonl(history_path, result)
    return result
