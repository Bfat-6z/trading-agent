"""Walk-forward validation for paper-only skill patches.

This module evaluates whether an applied skill patch survives future paper
reviews after its application time. It is deliberately conservative: a patch is
`running` until enough out-of-sample reviews exist, and it never enables live
orders.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, read_jsonl, write_json_atomic
from experiment_registry import EXPERIMENTS_JSONL, EXPERIMENTS_LATEST
from timebase import parse_utc, utc_now

ROOT = Path(__file__).resolve().parent
MEMORY_DIR = ROOT / "state" / "agent_memory"
POST_TRADE_REVIEWS = MEMORY_DIR / "post_trade_reviews.jsonl"
SKILL_PATCHES_APPLIED = MEMORY_DIR / "skill_patches_applied.jsonl"
SKILL_PATCHES_PENDING = MEMORY_DIR / "skill_patches_pending.jsonl"
WALK_FORWARD_LATEST = MEMORY_DIR / "walk_forward_latest.json"
WALK_FORWARD_HEARTBEAT = ROOT / "state" / "walk_forward_validator_heartbeat.json"
WALK_FORWARD_PID = ROOT / "state" / "walk_forward_validator.pid"
STOP_FILE = ROOT / "state" / "walk_forward_validator.stop"
HOLDOUT_REGISTRY = MEMORY_DIR / "walk_forward_holdout_registry.jsonl"

DEFAULT_ALPHA_BUDGET = 0.05
DEFAULT_EMBARGO_SECONDS = 0

def canonical_hash(value: Any, prefix: str) -> str:
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str, separators=(",", ":"))
    return f"{prefix}_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def setup_id_from_review(row: dict[str, Any]) -> str:
    source = row.get("source_trade") if isinstance(row.get("source_trade"), dict) else {}
    return str(source.get("setup_id") or row.get("setup_id") or "")


def review_ts(row: dict[str, Any]) -> Any:
    source = row.get("source_trade") if isinstance(row.get("source_trade"), dict) else {}
    return row.get("reviewed_at") or source.get("close_ts") or source.get("ts") or row.get("ts")

def decision_ts(row: dict[str, Any]) -> Any:
    return row.get("decision_ts") or row.get("label_start_at")


def review_net(row: dict[str, Any]) -> float:
    source = row.get("source_trade") if isinstance(row.get("source_trade"), dict) else {}
    costs = row.get("costs") if isinstance(row.get("costs"), dict) else {}
    return safe_float(source.get("net"), safe_float(costs.get("net")))

def review_symbol(row: dict[str, Any]) -> str:
    source = row.get("source_trade") if isinstance(row.get("source_trade"), dict) else {}
    return str(row.get("symbol") or source.get("symbol") or "")

def review_sector(row: dict[str, Any]) -> str:
    return str(row.get("sector") or row.get("coin_sector") or "unknown")

def review_beta_cluster(row: dict[str, Any]) -> str:
    return str(row.get("beta_cluster") or row.get("cluster") or "unknown")

def review_regime(row: dict[str, Any]) -> str:
    return str(row.get("market_regime") or "unknown")

def review_day(row: dict[str, Any]) -> str:
    ts = parse_utc(review_ts(row))
    return ts.date().isoformat() if ts else "unknown"

def effect_ci(values: list[float]) -> dict[str, Any]:
    n = len(values)
    mean = sum(values) / n if n else 0.0
    if n < 2:
        return {"mean": round(mean, 8), "n": n, "lower_95": round(mean, 8), "upper_95": round(mean, 8), "stderr": 0.0}
    variance = sum((value - mean) ** 2 for value in values) / (n - 1)
    stderr = math.sqrt(variance) / math.sqrt(n)
    return {
        "mean": round(mean, 8),
        "n": n,
        "lower_95": round(mean - 1.96 * stderr, 8),
        "upper_95": round(mean + 1.96 * stderr, 8),
        "stderr": round(stderr, 8),
    }

def window_spec_payload(spec: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in spec.items() if key != "window_id"}

def window_id_for(spec: dict[str, Any]) -> str:
    return canonical_hash(window_spec_payload(spec), "wfwin")

def build_walk_forward_window_spec(
    *,
    train_start: str,
    train_end: str,
    test_start: str,
    test_end: str,
    holdout_start: str | None = None,
    holdout_end: str | None = None,
    step: str = "1d",
    mode: str = "rolling",
    calendar_basis: str = "calendar",
    decision_time_basis: str = "decision_ts",
    label_end_basis: str = "label_end_at",
    embargo_seconds: int = DEFAULT_EMBARGO_SECONDS,
) -> dict[str, Any]:
    spec = {
        "schema_version": SCHEMA_VERSION,
        "train": {"start": train_start, "end": train_end},
        "test": {"start": test_start, "end": test_end},
        "audit_holdout": {"start": holdout_start, "end": holdout_end} if holdout_start and holdout_end else None,
        "step": step,
        "mode": mode,
        "calendar_basis": calendar_basis,
        "decision_time_basis": decision_time_basis,
        "label_end_basis": label_end_basis,
        "embargo_seconds": int(embargo_seconds or 0),
    }
    spec["window_id"] = window_id_for(spec)
    return spec

def intervals_overlap(a_start: Any, a_end: Any, b_start: Any, b_end: Any) -> bool:
    a0, a1, b0, b1 = parse_utc(a_start), parse_utc(a_end), parse_utc(b_start), parse_utc(b_end)
    if not all([a0, a1, b0, b1]):
        return True
    return a0 <= b1 and b0 <= a1

def validate_walk_forward_window_spec(spec: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not spec:
        return ["missing_walk_forward_window_spec"]
    if spec.get("mode") not in {"rolling", "expanding"}:
        errors.append("invalid_walk_forward_mode")
    if spec.get("window_id") != window_id_for(spec):
        errors.append("window_id_digest_mismatch")
    train = spec.get("train") if isinstance(spec.get("train"), dict) else {}
    test = spec.get("test") if isinstance(spec.get("test"), dict) else {}
    holdout = spec.get("audit_holdout") if isinstance(spec.get("audit_holdout"), dict) else None
    for name, window in (("train", train), ("test", test), ("audit_holdout", holdout or {})):
        if not window and name == "audit_holdout":
            continue
        start, end = parse_utc(window.get("start")), parse_utc(window.get("end"))
        if not start or not end:
            errors.append(f"invalid_{name}_window")
        elif end <= start:
            errors.append(f"{name}_window_not_forward")
    if train and test and intervals_overlap(train.get("start"), train.get("end"), test.get("start"), test.get("end")):
        errors.append("train_test_windows_overlap")
    if holdout and test and intervals_overlap(test.get("start"), test.get("end"), holdout.get("start"), holdout.get("end")):
        errors.append("test_holdout_windows_overlap")
    if holdout and train and intervals_overlap(train.get("start"), train.get("end"), holdout.get("start"), holdout.get("end")):
        errors.append("train_holdout_windows_overlap")
    return sorted(set(errors))

def sample_feature_label_interval(row: dict[str, Any], horizon_contract: dict[str, Any] | None = None) -> dict[str, Any]:
    horizon_contract = horizon_contract or {}
    label_start = row.get("label_start_at") or row.get("decision_ts") or review_ts(row)
    label_end = row.get("label_end_at") or review_ts(row)
    outcome_known = row.get("outcome_known_at") or label_end
    lookback_seconds = int(horizon_contract.get("max_feature_lookback_seconds") or row.get("max_feature_lookback_seconds") or 0)
    source_lag_seconds = int(horizon_contract.get("max_source_lag_seconds") or row.get("max_source_lag_seconds") or 0)
    label_start_dt = parse_utc(label_start)
    feature_start = (label_start_dt - timedelta(seconds=lookback_seconds + source_lag_seconds)).isoformat(timespec="seconds") if label_start_dt else label_start
    return {
        "label_start_at": label_start,
        "label_end_at": label_end,
        "outcome_known_at": outcome_known,
        "feature_start_at": row.get("feature_start_at") or feature_start,
        "feature_end_at": row.get("feature_end_at") or row.get("feature_ts") or label_start,
        "max_feature_lookback_seconds": lookback_seconds,
        "max_source_lag_seconds": source_lag_seconds,
    }

def mature_label(row: dict[str, Any], now: Any | None = None) -> bool:
    known = parse_utc(row.get("outcome_known_at") or row.get("label_end_at") or review_ts(row))
    reviewed = parse_utc(review_ts(row))
    current = parse_utc(now) if now else parse_utc(utc_now())
    return bool(known and current and known <= current and (not reviewed or reviewed <= current))

def expanded_window(window: dict[str, Any], embargo_seconds: int) -> dict[str, Any]:
    start, end = parse_utc(window.get("start")), parse_utc(window.get("end"))
    if not start or not end:
        return window
    return {
        "start": (start - timedelta(seconds=max(0, embargo_seconds))).isoformat(timespec="seconds"),
        "end": (end + timedelta(seconds=max(0, embargo_seconds))).isoformat(timespec="seconds"),
    }

def sample_overlaps_window(row: dict[str, Any], window: dict[str, Any], embargo_seconds: int = 0, horizon_contract: dict[str, Any] | None = None) -> bool:
    interval = sample_feature_label_interval(row, horizon_contract)
    check_window = expanded_window(window, embargo_seconds)
    return intervals_overlap(interval["feature_start_at"], interval["label_end_at"], check_window.get("start"), check_window.get("end"))

def purge_samples_for_window(rows: list[dict[str, Any]], window: dict[str, Any], embargo_seconds: int = 0, horizon_contract: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    kept: list[dict[str, Any]] = []
    purged: list[str] = []
    for row in rows:
        if sample_overlaps_window(row, window, embargo_seconds, horizon_contract):
            purged.append(str(row.get("review_id") or row.get("trade_id") or review_ts(row)))
        else:
            kept.append(row)
    return kept, purged


def filter_reviews(
    reviews: list[dict[str, Any]],
    setup_id: str,
    start: Any | None = None,
    end: Any | None = None,
    *,
    start_exclusive: bool = False,
) -> list[dict[str, Any]]:
    start_dt = parse_utc(start) if start else None
    end_dt = parse_utc(end) if end else None
    rows: list[dict[str, Any]] = []
    for row in reviews:
        if setup_id_from_review(row) != setup_id:
            continue
        ts = parse_utc(decision_ts(row) or review_ts(row))
        if not ts:
            continue
        if start_dt and (ts <= start_dt if start_exclusive else ts < start_dt):
            continue
        if end_dt and ts > end_dt:
            continue
        rows.append(row)
    return rows


def performance_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    nets = [review_net(row) for row in rows]
    wins = [value for value in nets if value > 0]
    losses = [value for value in nets if value < 0]
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    regimes = set()
    for row, net in zip(rows, nets):
        equity += net
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
        regime = row.get("market_regime")
        if regime:
            regimes.add(str(regime))
    trades = len(nets)
    net_total = sum(nets)
    return {
        "trades": trades,
        "net": round(net_total, 8),
        "expectancy_after_fees": round(net_total / trades, 8) if trades else 0.0,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 4) if losses else (999.0 if wins else 0.0),
        "win_rate": round(len(wins) / trades, 4) if trades else 0.0,
        "avg_win": round(sum(wins) / len(wins), 8) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 8) if losses else 0.0,
        "max_drawdown": round(abs(max_dd), 8),
        "regime_coverage": sorted(regimes),
        "confidence_interval": effect_ci(nets),
    }

def group_validation_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "unique_symbols": len({review_symbol(row) for row in rows if review_symbol(row)}),
        "unique_sectors": len({review_sector(row) for row in rows if review_sector(row)}),
        "unique_beta_clusters": len({review_beta_cluster(row) for row in rows if review_beta_cluster(row)}),
        "unique_regimes": len({review_regime(row) for row in rows if review_regime(row)}),
        "unique_days": len({review_day(row) for row in rows if review_day(row) != "unknown"}),
    }

def grouped_validation_errors(rows: list[dict[str, Any]], requirements: dict[str, Any] | None = None) -> list[str]:
    requirements = {
        "min_unique_symbols": 1,
        "min_unique_sectors": 1,
        "min_unique_beta_clusters": 1,
        "min_unique_regimes": 1,
        "min_unique_days": 1,
        **(requirements or {}),
    }
    metrics = group_validation_metrics(rows)
    errors: list[str] = []
    for key, metric_key in (
        ("min_unique_symbols", "unique_symbols"),
        ("min_unique_sectors", "unique_sectors"),
        ("min_unique_beta_clusters", "unique_beta_clusters"),
        ("min_unique_regimes", "unique_regimes"),
        ("min_unique_days", "unique_days"),
    ):
        if int(metrics.get(metric_key) or 0) < int(requirements.get(key) or 0):
            errors.append(f"{metric_key}_below_minimum")
    return errors

def regime_distribution_errors(rows: list[dict[str, Any]], manifest: dict[str, Any] | None = None) -> list[str]:
    if not manifest:
        return []
    required = set(str(item) for item in manifest.get("required_buckets", []) if item)
    present = {review_regime(row) for row in rows if review_regime(row)}
    missing = sorted(required - present)
    errors: list[str] = []
    if missing:
        errors.append("required_regime_bucket_absent")
    min_effective_n = int(manifest.get("min_effective_n_per_bucket") or 0)
    for bucket in required:
        if sum(1 for row in rows if review_regime(row) == bucket) < min_effective_n:
            errors.append("regime_bucket_effective_n_below_minimum")
            break
    return sorted(set(errors))

def leakage_errors_for_review(row: dict[str, Any]) -> list[str]:
    explicit_decision_ts = row.get("decision_ts") or row.get("label_start_at")
    timing_sensitive = any(row.get(field) for field in ("feature_ts", "feature_start_at", "feature_end_at", "source_available_at_max", "known_at", "available_at")) or bool(row.get("regime_label_cutoff_proof")) or bool(row.get("transform_fit_partition") or row.get("scaler_fit_partition"))
    errors: list[str] = []
    if timing_sensitive and not explicit_decision_ts:
        errors.append("missing_decision_time")
    decision_ts = explicit_decision_ts or review_ts(row)
    decision = parse_utc(decision_ts)
    if not decision:
        errors.append("invalid_decision_time")
        return sorted(set(errors))
    for field in ("feature_ts", "feature_start_at", "feature_end_at", "source_available_at_max", "known_at", "available_at"):
        parsed = parse_utc(row.get(field))
        if parsed and parsed > decision:
            errors.append(f"{field}_after_decision")
    proof = row.get("regime_label_cutoff_proof") if isinstance(row.get("regime_label_cutoff_proof"), dict) else {}
    for field in ("max_input_ts", "label_available_at", "computed_at"):
        parsed = parse_utc(proof.get(field))
        if parsed and parsed > decision:
            errors.append(f"regime_{field}_after_decision")
    if row.get("regime_label_uses_post_trade_outcome"):
        errors.append("regime_uses_post_trade_outcome")
    transform_partition = str(row.get("transform_fit_partition") or row.get("scaler_fit_partition") or "")
    if transform_partition in {"full_history", "test", "holdout", "audit_holdout", "validation"}:
        errors.append("transform_fit_outside_train")
    universe = row.get("universe_manifest") if isinstance(row.get("universe_manifest"), dict) else {}
    if universe.get("current_survivor_only") is True:
        errors.append("current_survivor_universe_diagnostic_only")
    return sorted(set(errors))

def leakage_errors_for_rows(rows: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for row in rows:
        errors.extend(leakage_errors_for_review(row))
    return sorted(set(errors))

def governance_errors_for_patch(patch: dict[str, Any], pending: dict[str, Any] | None = None) -> list[str]:
    pending = pending or {}
    evidence = pending.get("evidence") if isinstance(pending.get("evidence"), dict) else {}
    errors: list[str] = []
    if str(patch.get("hypothesis_source_partition") or evidence.get("hypothesis_source_partition") or "") in {"test", "holdout", "audit_holdout"}:
        errors.append("test_holdout_derived_hypothesis_contaminates_family")
    if patch.get("tuning_partition_id") and patch.get("readiness_partition_id") and patch.get("tuning_partition_id") == patch.get("readiness_partition_id"):
        errors.append("same_partition_used_for_tuning_and_readiness")
    if patch.get("shadow_partition_used_for_tuning") and patch.get("shadow_partition_used_for_readiness"):
        if patch.get("shadow_partition_used_for_tuning") == patch.get("shadow_partition_used_for_readiness"):
            errors.append("shadow_partition_reused_for_readiness")
    if patch.get("requires_frozen_manifests"):
        for field in ("frozen_partition_digest", "code_config_digest", "candidate_policy_digest", "metric_manifest_digest"):
            if not patch.get(field):
                errors.append(f"missing_{field}")
    if patch.get("migration_backed") and not patch.get("rollback_rehearsal_id"):
        errors.append("missing_rollback_rehearsal")
    return sorted(set(errors))

def manifest_digest_payload(patch: dict[str, Any], pending: dict[str, Any] | None = None) -> dict[str, Any]:
    pending = pending or {}
    evidence = pending.get("evidence") if isinstance(pending.get("evidence"), dict) else {}
    result = {}
    for field in ("metric_manifest_digest", "cited_metric_manifest_digest", "code_config_digest", "candidate_policy_digest", "frozen_partition_digest"):
        result[field] = patch.get(field) or evidence.get(field)
    if result.get("metric_manifest_digest") and not result.get("cited_metric_manifest_digest"):
        result["cited_metric_manifest_digest"] = result["metric_manifest_digest"]
    return result

def holdout_budget_errors(patch: dict[str, Any], registry_rows: list[dict[str, Any]] | None = None, max_peeks: int = 1) -> list[str]:
    uses_holdout = bool(patch.get("uses_audit_holdout") or patch.get("audit_holdout_id") or (isinstance(patch.get("walk_forward_window_spec"), dict) and patch["walk_forward_window_spec"].get("audit_holdout")))
    if not uses_holdout:
        return []
    spec_holdout = patch.get("walk_forward_window_spec", {}).get("audit_holdout") if isinstance(patch.get("walk_forward_window_spec"), dict) else {}
    inferred_holdout_id = canonical_hash(spec_holdout, "holdout") if spec_holdout else ""
    holdout_id = str(patch.get("audit_holdout_id") or inferred_holdout_id)
    if not holdout_id:
        return ["missing_audit_holdout_id"]
    rows = registry_rows or []
    uses = [row for row in rows if str(row.get("audit_holdout_id") or "") == holdout_id]
    if len(uses) >= max_peeks:
        return ["audit_holdout_budget_exhausted"]
    if any(row.get("sealed") is False for row in uses):
        return ["audit_holdout_peeked_before_seal"]
    return []

def family_variant_count_from_registry(patch: dict[str, Any], registry_rows: list[dict[str, Any]] | None = None) -> int:
    family_id = str(patch.get("experiment_family_id") or "")
    if not family_id:
        return 1
    variants = {
        str(row.get("variant_hash") or row.get("patch_id") or row.get("experiment_id"))
        for row in (registry_rows or [])
        if str(row.get("experiment_family_id") or "") == family_id and (row.get("variant_hash") or row.get("patch_id") or row.get("experiment_id"))
    }
    return max(1, len(variants))

def family_correction_errors(patch: dict[str, Any], pending: dict[str, Any] | None = None, alpha_budget: float = DEFAULT_ALPHA_BUDGET, registry_rows: list[dict[str, Any]] | None = None) -> tuple[list[str], dict[str, Any]]:
    pending = pending or {}
    evidence = pending.get("evidence") if isinstance(pending.get("evidence"), dict) else {}
    variant_count = max(family_variant_count_from_registry(patch, registry_rows), int(patch.get("family_variant_count") or evidence.get("family_variant_count") or 1))
    p_value_raw = patch.get("p_value", evidence.get("p_value"))
    corrected_alpha = float(alpha_budget) / variant_count
    errors: list[str] = []
    if variant_count > 1 and p_value_raw is None:
        errors.append("missing_p_value_for_family_correction")
    if p_value_raw is not None and safe_float(p_value_raw, 1.0) > corrected_alpha:
        errors.append("multiple_test_penalty_failed")
    if p_value_raw is not None and not (0.0 <= safe_float(p_value_raw, -1.0) <= 1.0):
        errors.append("invalid_p_value")
    return errors, {"family_variant_count": variant_count, "alpha_budget": alpha_budget, "corrected_alpha": corrected_alpha, "p_value": p_value_raw}

def partition_reviews_by_spec(
    reviews: list[dict[str, Any]],
    setup_id: str,
    spec: dict[str, Any],
    horizon_contract: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    train_window = spec.get("train") if isinstance(spec.get("train"), dict) else {}
    test_window = spec.get("test") if isinstance(spec.get("test"), dict) else {}
    holdout_window = spec.get("audit_holdout") if isinstance(spec.get("audit_holdout"), dict) else {}
    embargo = int(spec.get("embargo_seconds") or 0)
    setup_rows = [row for row in reviews if setup_id_from_review(row) == setup_id]
    train_rows = filter_reviews(setup_rows, setup_id, start=train_window.get("start"), end=train_window.get("end"))
    test_rows = filter_reviews(setup_rows, setup_id, start=test_window.get("start"), end=test_window.get("end"))
    holdout_rows = filter_reviews(setup_rows, setup_id, start=holdout_window.get("start"), end=holdout_window.get("end")) if holdout_window else []
    train_rows, purged_for_test = purge_samples_for_window(train_rows, test_window, embargo, horizon_contract)
    purged_for_holdout: list[str] = []
    purged_test_for_holdout: list[str] = []
    if holdout_window:
        train_rows, purged_for_holdout = purge_samples_for_window(train_rows, holdout_window, embargo, horizon_contract)
        test_rows, purged_test_for_holdout = purge_samples_for_window(test_rows, holdout_window, embargo, horizon_contract)
    meta = {"purged_for_test": purged_for_test, "purged_for_holdout": purged_for_holdout, "purged_test_for_holdout": purged_test_for_holdout, "embargo_seconds": embargo}
    return train_rows, test_rows, holdout_rows, meta


def pending_patch_lookup(pending_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("patch_id")): row for row in pending_rows if row.get("patch_id")}


def train_window_for_patch(patch: dict[str, Any], pending: dict[str, Any] | None, reviews: list[dict[str, Any]]) -> dict[str, Any]:
    setup_id = str(patch.get("setup_id") or "")
    applied_at = patch.get("applied_at")
    rows = filter_reviews(reviews, setup_id, end=applied_at)
    if rows:
        timestamps = [parse_utc(review_ts(row)) for row in rows if parse_utc(review_ts(row))]
        return {
            "start": min(timestamps).isoformat(timespec="seconds") if timestamps else None,
            "end": applied_at,
        }
    evidence = (pending or {}).get("evidence") if isinstance((pending or {}).get("evidence"), dict) else {}
    return {"start": evidence.get("start_ts"), "end": applied_at}


def evaluate_patch_walk_forward(
    patch: dict[str, Any],
    reviews: list[dict[str, Any]],
    pending: dict[str, Any] | None = None,
    min_test_trades: int = 20,
    window_spec: dict[str, Any] | None = None,
    horizon_contract: dict[str, Any] | None = None,
    holdout_registry_rows: list[dict[str, Any]] | None = None,
    family_registry_rows: list[dict[str, Any]] | None = None,
    grouped_requirements: dict[str, Any] | None = None,
    min_effect_size: float = 0.0,
    alpha_budget: float = DEFAULT_ALPHA_BUDGET,
) -> dict[str, Any]:
    setup_id = str(patch.get("setup_id") or "")
    applied_at = patch.get("applied_at")
    experiment_id = "wf_" + str(patch.get("patch_id") or setup_id)
    if not setup_id or not parse_utc(applied_at):
        return {
            "schema_version": SCHEMA_VERSION,
            "experiment_id": experiment_id,
            "patch_id": patch.get("patch_id"),
            "setup_id": setup_id,
            "hypothesis": f"patch {patch.get('patch_id')} improves future paper expectancy",
            "status": "failed",
            "errors": ["missing_setup_id"] if not setup_id else ["invalid_patch_application_time"],
            "created_at": applied_at,
            "evaluated_at": utc_now(),
            "train_window": {"start": None, "end": applied_at},
            "test_window": {"start": applied_at, "end": utc_now()},
            "holdout": {"source": "future_post_trade_reviews", "not_used_for_patch_proposal": True},
            "train_metrics": performance_metrics([]),
            "test_metrics": performance_metrics([]),
            "min_test_trades": min_test_trades,
            "can_place_live_orders": False,
        }
    effective_window_spec = window_spec or patch.get("walk_forward_window_spec")
    window_errors: list[str] = []
    partition_meta: dict[str, Any] = {}
    holdout_rows: list[dict[str, Any]] = []
    if isinstance(effective_window_spec, dict):
        window_errors = validate_walk_forward_window_spec(effective_window_spec)
        if not window_errors:
            past_rows, future_rows, holdout_rows, partition_meta = partition_reviews_by_spec(reviews, setup_id, effective_window_spec, horizon_contract)
        else:
            past_rows = filter_reviews(reviews, setup_id, end=applied_at)
            future_rows = filter_reviews(reviews, setup_id, start=applied_at, start_exclusive=True)
    else:
        past_rows = filter_reviews(reviews, setup_id, end=applied_at)
        future_rows = filter_reviews(reviews, setup_id, start=applied_at, start_exclusive=True)

    immature_reviews = [
        str(row.get("review_id") or review_ts(row))
        for row in future_rows
        if not mature_label(row)
    ]
    if immature_reviews:
        future_rows = [row for row in future_rows if str(row.get("review_id") or review_ts(row)) not in set(immature_reviews)]
    train_metrics = performance_metrics(past_rows)
    test_metrics = performance_metrics(future_rows)
    holdout_metrics = performance_metrics(holdout_rows)
    errors: list[str] = []
    errors.extend(window_errors)
    errors.extend(leakage_errors_for_rows(past_rows + future_rows + holdout_rows))
    missing_decision_rows = [
        str(row.get("review_id") or review_ts(row))
        for row in past_rows + future_rows + holdout_rows
        if not decision_ts(row)
    ]
    if missing_decision_rows:
        errors.append("missing_decision_time")
    errors.extend(governance_errors_for_patch(patch, pending))
    errors.extend(holdout_budget_errors(patch, holdout_registry_rows))
    family_errors, family_meta = family_correction_errors(patch, pending, alpha_budget=alpha_budget, registry_rows=family_registry_rows)
    errors.extend(family_errors)
    if patch.get("requires_grouped_validation") or grouped_requirements:
        errors.extend(grouped_validation_errors(future_rows, grouped_requirements))
    regime_manifest = patch.get("regime_distribution_manifest") if isinstance(patch.get("regime_distribution_manifest"), dict) else None
    errors.extend(regime_distribution_errors(future_rows, regime_manifest))
    if immature_reviews:
        errors.append("immature_or_unresolved_labels")
    if test_metrics["trades"] < min_test_trades:
        errors.append("insufficient_future_trades")
    if test_metrics["trades"] >= min_test_trades and test_metrics["expectancy_after_fees"] <= 0:
        errors.append("future_expectancy_not_positive")
    if test_metrics["trades"] >= min_test_trades and test_metrics["profit_factor"] < 1.05:
        errors.append("future_profit_factor_too_low")
    if test_metrics["trades"] >= min_test_trades and safe_float(test_metrics.get("expectancy_after_fees")) < float(min_effect_size or 0.0):
        errors.append("effect_size_too_small")
    hard_errors = [error for error in errors if error != "insufficient_future_trades"]
    inconclusive_errors = {"required_regime_bucket_absent", "regime_bucket_effective_n_below_minimum"}
    status = "inconclusive" if hard_errors and set(hard_errors).issubset(inconclusive_errors) else "failed" if hard_errors else "running" if test_metrics["trades"] < min_test_trades else "passed"
    return {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "patch_id": patch.get("patch_id"),
        "setup_id": setup_id,
        "hypothesis": f"patch {patch.get('patch_id')} improves future paper expectancy",
        "status": status,
        "errors": errors,
        "created_at": patch.get("applied_at"),
        "evaluated_at": utc_now(),
        "train_window": (effective_window_spec or {}).get("train") if isinstance(effective_window_spec, dict) else train_window_for_patch(patch, pending, reviews),
        "test_window": (effective_window_spec or {}).get("test") if isinstance(effective_window_spec, dict) else {"start": applied_at, "end": utc_now()},
        "holdout": {
            "source": "future_post_trade_reviews",
            "not_used_for_patch_proposal": True,
            "audit_holdout": (effective_window_spec or {}).get("audit_holdout") if isinstance(effective_window_spec, dict) else None,
            "metrics": holdout_metrics,
        },
        "walk_forward_window_spec": effective_window_spec,
        "partition_meta": partition_meta,
        "horizon_contract": horizon_contract or patch.get("horizon_contract") or {},
        "family_correction": family_meta,
        **manifest_digest_payload(patch, pending),
        "group_validation": group_validation_metrics(future_rows),
        "immature_review_ids": immature_reviews,
        "train_metrics": train_metrics,
        "test_metrics": test_metrics,
        "min_test_trades": min_test_trades,
        "can_place_live_orders": False,
    }


def latest_by_experiment(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered: list[str] = []
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("experiment_id") or "")
        if not key:
            continue
        if key not in latest:
            ordered.append(key)
        latest[key] = row
    return [latest[key] for key in ordered]


def write_outputs(results: list[dict[str, Any]], history_path: Path = EXPERIMENTS_JSONL, latest_path: Path = EXPERIMENTS_LATEST, walk_forward_path: Path = WALK_FORWARD_LATEST) -> dict[str, Any]:
    for row in results:
        append_jsonl(history_path, row)
    latest_rows = [
        row
        for row in latest_by_experiment(read_jsonl(history_path))
        if str(row.get("experiment_id") or "").startswith("wf_") and row.get("patch_id")
    ]
    by_status: dict[str, int] = {}
    for row in latest_rows:
        status = str(row.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
    review_watermark = None
    for row in latest_rows:
        end = ((row.get("test_window") or {}).get("end") if isinstance(row.get("test_window"), dict) else None) or row.get("evaluated_at")
        if end and (review_watermark is None or str(end) > str(review_watermark)):
            review_watermark = end
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "review_watermark": review_watermark,
        "stale_sla_seconds": 24 * 60 * 60,
        "experiment_count": len(latest_rows),
        "by_status": by_status,
        "rows": latest_rows,
        "can_place_live_orders": False,
    }
    write_json_atomic(latest_path, payload)
    write_json_atomic(walk_forward_path, payload)
    write_json_atomic(WALK_FORWARD_HEARTBEAT, {"schema_version": SCHEMA_VERSION, "agent": "walk_forward_validator", "pid": os.getpid(), "status": "ok", "updated_at": payload["updated_at"], "review_watermark": review_watermark})
    return payload


def run_once(
    *,
    applied_path: Path = SKILL_PATCHES_APPLIED,
    pending_path: Path = SKILL_PATCHES_PENDING,
    reviews_path: Path = POST_TRADE_REVIEWS,
    history_path: Path = EXPERIMENTS_JSONL,
    latest_path: Path = EXPERIMENTS_LATEST,
    walk_forward_path: Path = WALK_FORWARD_LATEST,
    min_test_trades: int = 20,
) -> dict[str, Any]:
    WALK_FORWARD_PID.parent.mkdir(parents=True, exist_ok=True)
    WALK_FORWARD_PID.write_text(str(os.getpid()), encoding="utf-8")
    applied = read_jsonl(applied_path)
    pending_lookup = pending_patch_lookup(read_jsonl(pending_path))
    reviews = read_jsonl(reviews_path)
    holdout_registry_rows = read_jsonl(HOLDOUT_REGISTRY)
    results: list[dict[str, Any]] = []
    for patch in applied:
        result = evaluate_patch_walk_forward(patch, reviews, pending_lookup.get(str(patch.get("patch_id"))), min_test_trades=min_test_trades, holdout_registry_rows=holdout_registry_rows, family_registry_rows=applied)
        results.append(result)
        spec_holdout = patch.get("walk_forward_window_spec", {}).get("audit_holdout") if isinstance(patch.get("walk_forward_window_spec"), dict) else {}
        holdout_id = str(patch.get("audit_holdout_id") or (canonical_hash(spec_holdout, "holdout") if spec_holdout else ""))
        if (patch.get("uses_audit_holdout") or spec_holdout) and holdout_id:
            usage = {
                "schema_version": SCHEMA_VERSION,
                "audit_holdout_id": holdout_id,
                "patch_id": patch.get("patch_id"),
                "experiment_id": result.get("experiment_id"),
                "used_at": result.get("evaluated_at") or utc_now(),
                "sealed": True,
                "can_place_live_orders": False,
            }
            append_jsonl(HOLDOUT_REGISTRY, usage)
            holdout_registry_rows.append(usage)
    return write_outputs(results, history_path=history_path, latest_path=latest_path, walk_forward_path=walk_forward_path)


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate paper-only skill patches with walk-forward windows")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--min-test-trades", type=int, default=20)
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_once(min_test_trades=args.min_test_trades)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
