"""Hermes-style setup skill patch forge with deterministic gates.

LLMs may propose patches, but this module decides whether evidence is sufficient
to stage/apply them. Applied patches are paper-only metadata; they never enable
live orders and never edit strategy code.
"""
from __future__ import annotations

import hashlib
import argparse
from pathlib import Path
from typing import Any, Iterable

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl_once, read_jsonl, write_json_atomic
from setup_skill_library import load_library, save_library
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
MEMORY_DIR = ROOT / "state" / "agent_memory"
PATCHES_PENDING = MEMORY_DIR / "skill_patches_pending.jsonl"
PATCHES_APPLIED = MEMORY_DIR / "skill_patches_applied.jsonl"
PATCHES_REVERTED = MEMORY_DIR / "skill_patches_reverted.jsonl"
PATCH_REVIEWS = MEMORY_DIR / "skill_patch_reviews.jsonl"
SKILL_FORGE_LATEST = MEMORY_DIR / "skill_forge_latest.json"
SKILL_PATCH_INTEGRATION_LATEST = MEMORY_DIR / "skill_patch_integration_latest.json"
POST_TRADE_REVIEWS = MEMORY_DIR / "post_trade_reviews.jsonl"

ALLOWED_PATCH_TYPES = {
    "regime_filter",
    "sl_tp_template",
    "entry_timing_rule",
    "symbol_blacklist",
    "symbol_graylist",
    "setup_retirement",
    "setup_split_by_regime",
    "leverage_cap_by_setup",
    "min_score_adjustment_by_setup",
}
TIGHTENING_PATCH_TYPES = {
    "min_score_adjustment_by_setup",
    "setup_retirement",
    "symbol_blacklist",
    "symbol_graylist",
    "leverage_cap_by_setup",
}


def patch_id(patch: dict[str, Any]) -> str:
    return "skill_patch_" + hashlib.sha256(repr(sorted(patch.items())).encode("utf-8")).hexdigest()[:20]


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


def evidence_ids(evidence: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for key in ("evidence_ids", "data_ids", "post_trade_review_ids", "counterfactual_ids", "shadow_ids"):
        value = evidence.get(key)
        if isinstance(value, list):
            ids.extend(str(item) for item in value if item)
        elif isinstance(value, str) and value:
            ids.append(value)
    return sorted(set(ids))


def review_setup_id(row: dict[str, Any]) -> str:
    source = row.get("source_trade") if isinstance(row.get("source_trade"), dict) else {}
    return str(source.get("setup_id") or row.get("setup_id") or "")


def review_net(row: dict[str, Any]) -> float:
    source = row.get("source_trade") if isinstance(row.get("source_trade"), dict) else {}
    costs = row.get("costs") if isinstance(row.get("costs"), dict) else {}
    return safe_float(source.get("net"), safe_float(costs.get("net")))


def build_review_patch_candidates(reviews: list[dict[str, Any]], min_sample: int = 30) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in reviews:
        setup_id = review_setup_id(row)
        if not setup_id:
            continue
        buckets.setdefault(setup_id, []).append(row)
    candidates: list[dict[str, Any]] = []
    for setup_id, rows in sorted(buckets.items()):
        sample_size = len(rows)
        if sample_size < min_sample:
            continue
        net = sum(review_net(row) for row in rows)
        expectancy = net / sample_size if sample_size else 0.0
        bad_loss_count = sum(1 for row in rows if row.get("classification") == "bad_loss")
        tp_too_far_count = sum(1 for row in rows if row.get("classification") == "tp_too_far")
        bad_loss_rate = bad_loss_count / sample_size if sample_size else 0.0
        evidence_review_ids = [str(row.get("review_id")) for row in rows[-30:] if row.get("review_id")]
        if expectancy < 0 and bad_loss_rate >= 0.35 and evidence_review_ids:
            candidates.append(
                {
                    "patch": {
                        "setup_id": setup_id,
                        "patch_type": "min_score_adjustment_by_setup",
                        "min_score_delta": 1.0,
                        "invalidation": "recent paper reviews show negative expectancy and repeated bad_loss outcomes",
                        "rollback_criteria": "20 future paper closes have positive expectancy and bad_loss_rate below 0.25",
                    },
                    "evidence": {
                        "source": "post_trade_reviews",
                        "sample_size": sample_size,
                        "expectancy": round(expectancy, 8),
                        "net": round(net, 8),
                        "bad_loss_rate": round(bad_loss_rate, 4),
                        "tp_too_far_count": tp_too_far_count,
                        "post_trade_review_ids": evidence_review_ids,
                    },
                    "reason": "negative_expectancy_bad_loss_cluster",
                }
            )
    return candidates


def validate_patch(patch: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if not patch.get("setup_id"):
        errors.append("missing_setup_id")
    patch_type = patch.get("patch_type")
    if not patch_type:
        errors.append("missing_patch_type")
    elif patch_type not in ALLOWED_PATCH_TYPES:
        errors.append("unsupported_patch_type")
    if not patch.get("invalidation"):
        errors.append("missing_invalidation")
    if not patch.get("rollback_criteria"):
        errors.append("missing_rollback_criteria")
    ids = evidence_ids(evidence)
    if not ids:
        errors.append("missing_evidence_ids")
    if safe_float(evidence.get("expectancy")) < 0 and patch_type not in TIGHTENING_PATCH_TYPES:
        errors.append("negative_expectancy")
    elif safe_float(evidence.get("expectancy")) < 0:
        warnings.append("negative_expectancy_tightening_only")
    if int(evidence.get("sample_size") or 0) < 20:
        warnings.append("under_sampled_paper_shadow_only")
    lifecycle = ["proposed"]
    schema_errors = {"missing_setup_id", "missing_patch_type", "unsupported_patch_type", "missing_invalidation", "missing_rollback_criteria"}
    if not any(error in schema_errors for error in errors):
        lifecycle.append("schema_valid")
    if not errors:
        lifecycle.append("evidence_checked")
    status = "rejected" if errors else "paper_shadow_only"
    return {
        "ok": not errors,
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "lifecycle": lifecycle,
        "evidence_ids": ids,
        "paper_only": True,
        "can_place_live_orders": False,
        "can_loosen_risk": False,
    }


def propose_skill_patch(
    patch: dict[str, Any],
    evidence: dict[str, Any],
    pending_path: Path = PATCHES_PENDING,
    review_path: Path = PATCH_REVIEWS,
    latest_path: Path = SKILL_FORGE_LATEST,
) -> dict[str, Any]:
    row = {
        **patch,
        "schema_version": SCHEMA_VERSION,
        "patch_id": patch.get("patch_id") or patch_id(patch),
        "proposed_at": utc_now(),
        "evidence": evidence,
        "live_enabled": False,
    }
    validation = validate_patch(row, evidence)
    review = {"schema_version": SCHEMA_VERSION, "patch_id": row["patch_id"], "reviewed_at": utc_now(), **validation}
    append_jsonl_once(review_path, review, "patch_id")
    if review["ok"]:
        append_jsonl_once(
            pending_path,
            {
                **row,
                "status": review["status"],
                "lifecycle": review["lifecycle"],
                "evidence_ids": review["evidence_ids"],
                "paper_only": True,
            },
            "patch_id",
        )
    write_latest(pending_path, review_path, output_path=latest_path)
    return review


def write_latest(
    pending_path: Path = PATCHES_PENDING,
    review_path: Path = PATCH_REVIEWS,
    output_path: Path = SKILL_FORGE_LATEST,
    applied_path: Path = PATCHES_APPLIED,
    reverted_path: Path = PATCHES_REVERTED,
) -> dict[str, Any]:
    pending = read_jsonl(pending_path)
    reviews = read_jsonl(review_path)
    applied = read_jsonl(applied_path)
    reverted = read_jsonl(reverted_path)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "pending_count": len(pending),
        "review_count": len(reviews),
        "rejected_count": sum(1 for row in reviews if not row.get("ok")),
        "applied_count": len(applied),
        "reverted_count": len(reverted),
        "pending": pending[-20:],
        "applied": applied[-20:],
        "can_place_live_orders": False,
        "can_loosen_risk": False,
    }
    write_json_atomic(output_path, payload)
    return payload


def patch_already_in_skill(skill: dict[str, Any], patch: dict[str, Any]) -> bool:
    metadata = skill.get("metadata") if isinstance(skill.get("metadata"), dict) else {}
    existing = metadata.get("paper_shadow_patches") if isinstance(metadata.get("paper_shadow_patches"), list) else []
    return any(row.get("patch_id") == patch.get("patch_id") for row in existing if isinstance(row, dict))


def apply_patch_metadata(skill: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    metadata = skill.get("metadata") if isinstance(skill.get("metadata"), dict) else {}
    existing = metadata.get("paper_shadow_patches") if isinstance(metadata.get("paper_shadow_patches"), list) else []
    existing.append(
        {
            "patch_id": patch.get("patch_id"),
            "patch_type": patch.get("patch_type"),
            "status": "paper_only_applied",
            "invalidation": patch.get("invalidation"),
            "rollback_criteria": patch.get("rollback_criteria"),
            "evidence_ids": patch.get("evidence_ids") or evidence_ids(patch.get("evidence") if isinstance(patch.get("evidence"), dict) else {}),
            "evidence": patch.get("evidence", {}),
            "applied_at": utc_now(),
            "scope": "paper_only",
            "live_enabled": False,
        }
    )
    metadata["paper_shadow_patches"] = existing[-20:]
    patch_type = patch.get("patch_type")
    if patch_type == "setup_retirement":
        metadata["paper_only_retired"] = True
    elif patch_type == "leverage_cap_by_setup":
        metadata["paper_only_leverage_cap"] = patch.get("max_leverage")
    elif patch_type == "min_score_adjustment_by_setup":
        metadata["paper_only_min_score_adjustment"] = patch.get("min_score_delta")
    elif patch_type == "symbol_blacklist":
        symbols = metadata.get("paper_only_symbol_blacklist") if isinstance(metadata.get("paper_only_symbol_blacklist"), list) else []
        symbols.extend(str(item).upper() for item in patch.get("symbols", []) if item)
        metadata["paper_only_symbol_blacklist"] = sorted(set(symbols))
    elif patch_type == "symbol_graylist":
        symbols = metadata.get("paper_only_symbol_graylist") if isinstance(metadata.get("paper_only_symbol_graylist"), list) else []
        symbols.extend(str(item).upper() for item in patch.get("symbols", []) if item)
        metadata["paper_only_symbol_graylist"] = sorted(set(symbols))
    else:
        adjustments = metadata.get("paper_only_adjustments") if isinstance(metadata.get("paper_only_adjustments"), list) else []
        adjustments.append({"patch_id": patch.get("patch_id"), "patch_type": patch_type})
        metadata["paper_only_adjustments"] = adjustments[-20:]
    skill["metadata"] = metadata
    skill["paper_shadow_patch_count"] = len(metadata["paper_shadow_patches"])
    return skill


def apply_paper_shadow_patches(
    pending_path: Path = PATCHES_PENDING,
    output_path: Path = SKILL_PATCH_INTEGRATION_LATEST,
    applied_path: Path = PATCHES_APPLIED,
    latest_path: Path | None = SKILL_FORGE_LATEST,
) -> dict[str, Any]:
    pending = [row for row in read_jsonl(pending_path) if row.get("status") == "paper_shadow_only"]
    already_applied = {str(row.get("patch_id")) for row in read_jsonl(applied_path) if row.get("patch_id")}
    library = load_library()
    skills = library.get("skills") if isinstance(library.get("skills"), dict) else {}
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for patch in pending:
        if str(patch.get("patch_id")) in already_applied:
            skipped.append({"patch_id": patch.get("patch_id"), "setup_id": patch.get("setup_id"), "reason": "already_applied"})
            continue
        validation = validate_patch(patch, patch.get("evidence") if isinstance(patch.get("evidence"), dict) else {})
        if not validation.get("ok"):
            skipped.append({"patch_id": patch.get("patch_id"), "setup_id": patch.get("setup_id"), "reason": "failed_apply_gate", "errors": validation.get("errors")})
            continue
        setup_id = str(patch.get("setup_id") or "")
        skill = skills.get(setup_id)
        if not isinstance(skill, dict):
            skipped.append({"patch_id": patch.get("patch_id"), "setup_id": setup_id, "reason": "unknown_setup"})
            continue
        if patch_already_in_skill(skill, patch):
            skipped.append({"patch_id": patch.get("patch_id"), "setup_id": setup_id, "reason": "already_applied"})
            continue
        skills[setup_id] = apply_patch_metadata(skill, patch)
        applied_row = {
            "schema_version": SCHEMA_VERSION,
            "patch_id": patch.get("patch_id"),
            "setup_id": setup_id,
            "status": "paper_only_applied",
            "applied_at": utc_now(),
            "lifecycle": [*(patch.get("lifecycle") or ["proposed", "schema_valid", "evidence_checked"]), "paper_only_applied"],
            "live_enabled": False,
        }
        append_jsonl_once(applied_path, applied_row, "patch_id")
        already_applied.add(str(patch.get("patch_id")))
        applied.append(applied_row)
    if applied:
        library["skills"] = skills
        save_library(library)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "applied_count": len(applied),
        "skipped_count": len(skipped),
        "applied": applied,
        "skipped": skipped,
        "can_place_live_orders": False,
        "can_loosen_risk": False,
    }
    write_json_atomic(output_path, payload)
    if latest_path is not None:
        write_latest(pending_path=pending_path, output_path=latest_path, applied_path=applied_path)
    return payload


def run_once(
    *,
    reviews_path: Path = POST_TRADE_REVIEWS,
    pending_path: Path = PATCHES_PENDING,
    review_path: Path = PATCH_REVIEWS,
    latest_path: Path = SKILL_FORGE_LATEST,
    integration_output_path: Path = SKILL_PATCH_INTEGRATION_LATEST,
    applied_path: Path = PATCHES_APPLIED,
    min_sample: int = 30,
    apply: bool = False,
) -> dict[str, Any]:
    reviews = read_jsonl(reviews_path)
    candidates = build_review_patch_candidates(reviews, min_sample=min_sample)
    reviews_out: list[dict[str, Any]] = []
    for candidate in candidates:
        reviews_out.append(
            propose_skill_patch(
                candidate["patch"],
                candidate["evidence"],
                pending_path=pending_path,
                review_path=review_path,
                latest_path=latest_path,
            )
        )
    integration = None
    if apply:
        integration = apply_paper_shadow_patches(
            pending_path=pending_path,
            output_path=integration_output_path,
            applied_path=applied_path,
            latest_path=latest_path,
        )
    latest = write_latest(pending_path=pending_path, review_path=review_path, output_path=latest_path, applied_path=applied_path)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "candidate_count": len(candidates),
        "reviewed_count": len(reviews_out),
        "accepted_count": sum(1 for row in reviews_out if row.get("ok")),
        "apply": bool(apply),
        "integration": integration,
        "latest": latest,
        "can_place_live_orders": False,
        "can_loosen_risk": False,
    }
    return payload


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run deterministic paper-only skill forge")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--min-sample", type=int, default=30)
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_once(min_sample=args.min_sample, apply=bool(args.apply))
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
