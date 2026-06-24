"""Versioned A+ pure setup rubric and calibration helpers."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
RUBRIC_PATH = ROOT / "state" / "agent_memory" / "aplus_rubric.json"
RUBRIC_VERSION = "aplus_pure_v1"

DIMENSION_WEIGHTS = {
    "trend_alignment": 0.14,
    "liquidity_volume_quality": 0.14,
    "volatility_context": 0.10,
    "derivatives_confirmation": 0.10,
    "invalidation_clarity": 0.16,
    "regime_compatibility": 0.12,
    "news_social_conflict": 0.08,
    "timing_quality": 0.08,
    "execution_realism": 0.08,
}


def default_rubric() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "rubric_version": RUBRIC_VERSION,
        "updated_at": utc_now(),
        "weights": DIMENSION_WEIGHTS,
        "hard_requirements": ["explicit_invalidation", "r_multiple", "non_chase_timing", "execution_realistic"],
        "grade_thresholds": {"A+": 0.85, "A": 0.75, "B": 0.62, "C": 0.5},
    }


def clamp01(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except Exception:
        return 0.0


def score_setup(candidate: dict[str, Any], rubric: dict[str, Any] | None = None) -> dict[str, Any]:
    rubric = rubric or default_rubric()
    dimensions = candidate.get("dimensions") if isinstance(candidate.get("dimensions"), dict) else {}
    hard = candidate.get("hard_requirements") if isinstance(candidate.get("hard_requirements"), dict) else {}
    weighted = 0.0
    normalized_dimensions = {}
    for name, weight in DIMENSION_WEIGHTS.items():
        value = clamp01(dimensions.get(name))
        normalized_dimensions[name] = value
        weighted += value * weight
    errors: list[str] = []
    if not hard.get("explicit_invalidation"):
        errors.append("missing_explicit_invalidation")
    if float(candidate.get("r_multiple") or 0.0) <= 0:
        errors.append("missing_positive_r_multiple")
    if hard.get("chase_timing") is True:
        errors.append("chase_timing")
    if hard.get("execution_realistic") is False:
        errors.append("execution_not_realistic")
    other_score = sum(v for k, v in normalized_dimensions.items() if k != "liquidity_volume_quality")
    if normalized_dimensions.get("liquidity_volume_quality", 0.0) >= 0.9 and other_score < 3.0:
        errors.append("high_volume_alone_not_aplus")
    score = round(weighted, 4)
    thresholds = rubric["grade_thresholds"]
    grade = "D"
    for label, threshold in thresholds.items():
        if score >= threshold:
            grade = label
            break
    if errors and grade == "A+":
        grade = "A_blocked"
    return {
        "schema_version": SCHEMA_VERSION,
        "rubric_version": RUBRIC_VERSION,
        "scored_at": utc_now(),
        "setup_id": candidate.get("setup_id", "unknown"),
        "score": score,
        "grade": grade,
        "can_assign_aplus": grade == "A+" and not errors,
        "errors": errors,
        "dimension_scores": normalized_dimensions,
    }


def write_default_rubric(path: Path = RUBRIC_PATH) -> dict[str, Any]:
    rubric = default_rubric()
    write_json_atomic(path, rubric)
    return rubric


def calibration_adjustment(review: dict[str, Any]) -> dict[str, Any]:
    classification = str(review.get("classification") or "")
    adjustment = 0.0
    reason = "neutral"
    if classification == "bad_win":
        adjustment = -0.08
        reason = "bad_win_process_penalty"
    elif classification == "good_loss":
        adjustment = 0.02
        reason = "valid_loss_small_credit"
    elif classification == "bad_loss":
        adjustment = -0.12
        reason = "bad_loss_penalty"
    return {"rubric_version": RUBRIC_VERSION, "adjustment": adjustment, "reason": reason}
