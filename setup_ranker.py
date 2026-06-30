"""Evidence-based setup ranking for paper capital allocation."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import read_json, read_jsonl, write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
MEMORY_DIR = ROOT / "state" / "agent_memory"
RANKINGS_LATEST = MEMORY_DIR / "setup_rankings_latest.json"
POST_TRADE_REVIEWS = MEMORY_DIR / "post_trade_reviews.jsonl"
COUNTERFACTUAL_REPLAYS = MEMORY_DIR / "counterfactual_replays.jsonl"
SHADOW_CLOSES = MEMORY_DIR / "shadow_closes.jsonl"
REAL_SCORING_LATEST = MEMORY_DIR / "real_scoring_board_latest.json"


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def setup_id_from_review(row: dict[str, Any]) -> str:
    source = row.get("source_trade") if isinstance(row.get("source_trade"), dict) else {}
    return str(source.get("setup_id") or row.get("setup_id") or "")


def trade_id_from_review(row: dict[str, Any]) -> str:
    source = row.get("source_trade") if isinstance(row.get("source_trade"), dict) else {}
    return str(row.get("trade_id") or source.get("trade_id") or source.get("position_id") or "")


def net_from_review(row: dict[str, Any]) -> float:
    source = row.get("source_trade") if isinstance(row.get("source_trade"), dict) else {}
    costs = row.get("costs") if isinstance(row.get("costs"), dict) else {}
    return safe_float(source.get("net"), safe_float(costs.get("net")))


def summarize_reviews_by_setup(reviews: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    trade_to_setup: dict[str, str] = {}
    for row in reviews:
        setup_id = setup_id_from_review(row)
        if not setup_id:
            continue
        buckets.setdefault(setup_id, []).append(row)
        trade_id = trade_id_from_review(row)
        if trade_id:
            trade_to_setup[trade_id] = setup_id
    summary: dict[str, dict[str, Any]] = {}
    for setup_id, rows in buckets.items():
        sample = len(rows)
        net = sum(net_from_review(row) for row in rows)
        bad_loss = sum(1 for row in rows if row.get("classification") == "bad_loss")
        good_loss = sum(1 for row in rows if row.get("classification") == "good_loss")
        tp_too_far = sum(1 for row in rows if row.get("classification") == "tp_too_far")
        process_scores = [safe_float(row.get("process_quality_score")) for row in rows if row.get("process_quality_score") is not None]
        setup_scores = [safe_float(row.get("setup_validity_score")) for row in rows if row.get("setup_validity_score") is not None]
        summary[setup_id] = {
            "review_sample": sample,
            "review_net": round(net, 8),
            "review_expectancy": round(net / sample, 8) if sample else 0.0,
            "bad_loss_rate": round(bad_loss / sample, 4) if sample else 0.0,
            "good_loss_rate": round(good_loss / sample, 4) if sample else 0.0,
            "tp_too_far_rate": round(tp_too_far / sample, 4) if sample else 0.0,
            "avg_process_quality_score": round(sum(process_scores) / len(process_scores), 4) if process_scores else None,
            "avg_setup_validity_score": round(sum(setup_scores) / len(setup_scores), 4) if setup_scores else None,
        }
    return summary, trade_to_setup


def summarize_counterfactual_by_setup(replays: list[dict[str, Any]], trade_to_setup: dict[str, str]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in replays:
        setup_id = str(row.get("setup_id") or trade_to_setup.get(str(row.get("signal_id") or "")) or "")
        if not setup_id:
            continue
        buckets.setdefault(setup_id, []).append(row)
    summary: dict[str, dict[str, Any]] = {}
    for setup_id, rows in buckets.items():
        complete = [row for row in rows if row.get("status") == "complete"]
        improvement = [row for row in complete if row.get("conclusion") == "parameter_improvement_candidate"]
        summary[setup_id] = {
            "counterfactual_sample": len(rows),
            "counterfactual_complete": len(complete),
            "parameter_improvement_count": len(improvement),
            "parameter_instability_rate": round(len(improvement) / len(complete), 4) if complete else 0.0,
        }
    return summary


def setup_id_from_shadow(row: dict[str, Any]) -> str:
    signal = row.get("signal") if isinstance(row.get("signal"), dict) else {}
    order_plan = row.get("order_plan") if isinstance(row.get("order_plan"), dict) else {}
    return str(row.get("setup_id") or signal.get("setup_id") or order_plan.get("setup_id") or "")


def summarize_shadow_by_setup(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        setup_id = setup_id_from_shadow(row)
        if not setup_id:
            continue
        buckets.setdefault(setup_id, []).append(row)
    summary: dict[str, dict[str, Any]] = {}
    for setup_id, items in buckets.items():
        closed = [row for row in items if row.get("status") == "closed"]
        net = sum(safe_float(row.get("net")) for row in closed)
        wins = sum(1 for row in closed if safe_float(row.get("net")) > 0)
        summary[setup_id] = {
            "shadow_setup_sample": len(closed),
            "shadow_setup_net": round(net, 8),
            "shadow_setup_expectancy": round(net / len(closed), 8) if closed else 0.0,
            "shadow_setup_win_rate": round(wins / len(closed), 4) if closed else 0.0,
        }
    return summary


def summarize_real_scoring_by_setup(scoring: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    by_setup = scoring.get("by_setup") if isinstance(scoring.get("by_setup"), dict) else {}
    base = {
        "real_scoring_snapshot_id": scoring.get("snapshot_id"),
        "real_scoring_passed": scoring.get("passed"),
        "real_scoring_hard_errors": scoring.get("hard_errors") if isinstance(scoring.get("hard_errors"), list) else [],
        "real_scoring_metric_manifest_digest": scoring.get("metric_manifest_digest"),
    }
    summary: dict[str, dict[str, Any]] = {}
    for setup_id, metric in by_setup.items():
        if not isinstance(metric, dict):
            continue
        summary[str(setup_id)] = {
            **metric,
            **base,
        }
    return summary, base


def build_setup_evidence_rows(
    library: dict[str, Any],
    reviews: list[dict[str, Any]] | None = None,
    replays: list[dict[str, Any]] | None = None,
    shadow_rows: list[dict[str, Any]] | None = None,
    real_scoring: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    review_summary, trade_to_setup = summarize_reviews_by_setup(reviews if reviews is not None else read_jsonl(POST_TRADE_REVIEWS))
    counterfactual_summary = summarize_counterfactual_by_setup(replays if replays is not None else read_jsonl(COUNTERFACTUAL_REPLAYS), trade_to_setup)
    shadow_summary = summarize_shadow_by_setup(shadow_rows if shadow_rows is not None else read_jsonl(SHADOW_CLOSES))
    scoring_summary, scoring_base = summarize_real_scoring_by_setup(real_scoring if real_scoring is not None else read_json(REAL_SCORING_LATEST, default={}))
    rows: list[dict[str, Any]] = []
    skills = library.get("skills") if isinstance(library.get("skills"), dict) else {}
    for setup_id, skill in skills.items():
        if not isinstance(skill, dict):
            continue
        stats = skill.get("stats") if isinstance(skill.get("stats"), dict) else {}
        rows.append(
            {
                "setup_id": setup_id,
                "setup_version": skill.get("setup_version") or skill.get("version"),
                "setup_contract_id": skill.get("setup_contract_id"),
                "setup_contract_hash": skill.get("setup_contract_hash"),
                "setup_quality_tier": skill.get("setup_quality_tier"),
                "matcher_version": skill.get("matcher_version"),
                "ranker_version": skill.get("ranker_version"),
                "risk_version": skill.get("risk_version"),
                **stats,
                "metadata": skill.get("metadata") if isinstance(skill.get("metadata"), dict) else {},
                **review_summary.get(setup_id, {}),
                **counterfactual_summary.get(setup_id, {}),
                **shadow_summary.get(setup_id, {}),
                **(scoring_base if scoring_base.get("real_scoring_snapshot_id") else {}),
                **scoring_summary.get(setup_id, {}),
            }
        )
    return rows


def allocation_hint_for(row: dict[str, Any], score: float, expectancy: float, trades: int) -> tuple[str, float, list[str]]:
    reasons: list[str] = []
    if row.get("paper_only_retired"):
        return "skip", 0.0, ["paper_only_retired"]
    if row.get("real_scoring_passed") is False:
        hard_errors = [str(item) for item in (row.get("real_scoring_hard_errors") or [])]
        setup_id = str(row.get("setup_id") or "")
        setup_failed = any(item == f"setup_bucket_failed:{setup_id}" for item in hard_errors)
        global_failed = any(not item.startswith("setup_bucket_failed:") for item in hard_errors)
        if setup_failed or global_failed or not hard_errors:
            reasons.append("real_scoring_failed")
    if expectancy <= 0:
        reasons.append("non_positive_evidence_expectancy")
    if safe_float(row.get("bad_loss_rate")) >= 0.35 and safe_int(row.get("review_sample")) >= 30:
        reasons.append("bad_loss_cluster")
    if safe_float(row.get("parameter_instability_rate")) >= 0.35 and safe_int(row.get("counterfactual_complete")) >= 10:
        reasons.append("counterfactual_parameter_instability")
    if safe_int(row.get("shadow_setup_sample")) >= 20 and safe_float(row.get("shadow_setup_expectancy")) < 0:
        reasons.append("shadow_setup_negative")
    has_real_scoring = bool(row.get("real_scoring_snapshot_id"))
    effective = row.get("effective_sample") if isinstance(row.get("effective_sample"), dict) else {}
    if has_real_scoring and safe_float(effective.get("effective_n"), trades) < 20 and not reasons:
        return "tiny", 0.5, ["real_scoring_effective_n_below_gate"]
    if has_real_scoring and row.get("expectancy_lower_bound_95") is not None and safe_float(row.get("expectancy_lower_bound_95")) <= 0:
        reasons.append("real_scoring_lcb_non_positive")
    if has_real_scoring and safe_float(row.get("profit_factor_after_costs")) < 1.15 and trades >= 5:
        reasons.append("real_scoring_profit_factor_below_gate")
    if has_real_scoring and row.get("cost_completeness") is False:
        reasons.append("real_scoring_cost_incomplete")
    if reasons:
        return "reduced", 0.35, reasons
    if trades < 20:
        return "tiny", 0.5, ["under_sampled"]
    if score >= 1.0:
        return "normal", 1.0, ["positive_rank"]
    return "tiny", 0.65, ["low_rank_score"]


def rank_setup(row: dict[str, Any]) -> dict[str, Any]:
    trades = safe_int(row.get("trades") or row.get("closed") or row.get("review_sample") or 0)
    base_expectancy = safe_float(row.get("expectancy_after_costs"), safe_float(row.get("expectancy")))
    review_expectancy = row.get("review_expectancy")
    has_real_scoring = bool(row.get("real_scoring_snapshot_id"))
    expectancy = base_expectancy if has_real_scoring else safe_float(review_expectancy, base_expectancy) if review_expectancy is not None else base_expectancy
    profit_factor = safe_float(row.get("profit_factor_after_costs"), safe_float(row.get("profit_factor")))
    win_rate = safe_float(row.get("win_rate"))
    max_drawdown = safe_float(row.get("max_drawdown"))
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    confidence = min(1.0, trades / 50)
    effective = row.get("effective_sample") if isinstance(row.get("effective_sample"), dict) else {}
    has_effective_n = "effective_n" in effective
    effective_n = safe_float(effective.get("effective_n"), float(trades))
    lcb = row.get("expectancy_lower_bound_95")
    score = expectancy * 10 + min(profit_factor, 3.0) * 0.2 + win_rate * 0.2 + confidence * 0.25 - max_drawdown * 0.5
    if trades < 20:
        score -= 0.5
    if has_effective_n and effective_n < 20:
        score -= 0.75
    if expectancy <= 0:
        score -= 1.0
    if lcb is not None and safe_float(lcb) <= 0:
        score -= 1.0
    bad_loss_rate = safe_float(row.get("bad_loss_rate"))
    if bad_loss_rate >= 0.35 and safe_int(row.get("review_sample")) >= 30:
        score -= min(1.5, bad_loss_rate * 2.0)
    instability = safe_float(row.get("parameter_instability_rate"))
    if instability >= 0.35 and safe_int(row.get("counterfactual_complete")) >= 10:
        score -= min(1.0, instability)
    if safe_int(row.get("shadow_setup_sample")) >= 20 and safe_float(row.get("shadow_setup_expectancy")) < 0:
        score -= 0.75
    paper_only_retired = bool(metadata.get("paper_only_retired"))
    if paper_only_retired:
        score -= 100.0
    hint, risk_multiplier, reasons = allocation_hint_for({**row, "paper_only_retired": paper_only_retired}, score, expectancy, trades)
    return {
        **row,
        "evidence_expectancy": round(expectancy, 8),
        "rank_score": round(score, 6),
        "sample_confidence": round(confidence, 4),
        "effective_n": round(effective_n, 4),
        "expectancy_lower_bound_95": safe_float(lcb) if lcb is not None else None,
        "under_sampled": trades < 20,
        "paper_only_retired": paper_only_retired,
        "paper_only_leverage_cap": metadata.get("paper_only_leverage_cap"),
        "paper_only_min_score_adjustment": metadata.get("paper_only_min_score_adjustment"),
        "allocation_hint": hint,
        "risk_multiplier": risk_multiplier,
        "rank_reasons": reasons,
    }


def rank_setups(rows: list[dict[str, Any]], output_path: Path = RANKINGS_LATEST) -> dict[str, Any]:
    ranked = [rank_setup(row) for row in rows]
    ranked.sort(key=lambda row: row["rank_score"], reverse=True)
    payload = {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "rankings": ranked, "top_setup_id": ranked[0].get("setup_id") if ranked else None}
    write_json_atomic(output_path, payload)
    return payload
