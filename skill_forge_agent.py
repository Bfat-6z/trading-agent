"""Hermes-style setup skill patch forge with deterministic gates."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl_once, read_jsonl, write_json_atomic
from setup_skill_library import load_library, save_library
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
MEMORY_DIR = ROOT / "state" / "agent_memory"
PATCHES_PENDING = MEMORY_DIR / "skill_patches_pending.jsonl"
PATCH_REVIEWS = MEMORY_DIR / "skill_patch_reviews.jsonl"
SKILL_FORGE_LATEST = MEMORY_DIR / "skill_forge_latest.json"
SKILL_PATCH_INTEGRATION_LATEST = MEMORY_DIR / "skill_patch_integration_latest.json"


def patch_id(patch: dict[str, Any]) -> str:
    return "skill_patch_" + hashlib.sha256(repr(sorted(patch.items())).encode("utf-8")).hexdigest()[:20]


def validate_patch(patch: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    errors = []
    warnings = []
    if not patch.get("setup_id"):
        errors.append("missing_setup_id")
    if not patch.get("patch_type"):
        errors.append("missing_patch_type")
    if not patch.get("invalidation"):
        errors.append("missing_invalidation")
    if float(evidence.get("expectancy") or 0.0) < 0:
        errors.append("negative_expectancy")
    if int(evidence.get("sample_size") or 0) < 20:
        warnings.append("under_sampled_paper_shadow_only")
    status = "rejected" if errors else "paper_shadow_only"
    return {"ok": not errors, "status": status, "errors": errors, "warnings": warnings}


def propose_skill_patch(patch: dict[str, Any], evidence: dict[str, Any], pending_path: Path = PATCHES_PENDING, review_path: Path = PATCH_REVIEWS) -> dict[str, Any]:
    row = {**patch, "schema_version": SCHEMA_VERSION, "patch_id": patch.get("patch_id") or patch_id(patch), "proposed_at": utc_now(), "evidence": evidence}
    review = {"schema_version": SCHEMA_VERSION, "patch_id": row["patch_id"], "reviewed_at": utc_now(), **validate_patch(row, evidence)}
    append_jsonl_once(review_path, review, "patch_id")
    if review["ok"]:
        append_jsonl_once(pending_path, {**row, "status": review["status"], "live_enabled": False}, "patch_id")
    write_latest(pending_path, review_path)
    return review


def write_latest(pending_path: Path = PATCHES_PENDING, review_path: Path = PATCH_REVIEWS, output_path: Path = SKILL_FORGE_LATEST) -> dict[str, Any]:
    pending = read_jsonl(pending_path)
    reviews = read_jsonl(review_path)
    payload = {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "pending_count": len(pending), "review_count": len(reviews), "rejected_count": sum(1 for row in reviews if not row.get("ok")), "pending": pending[-20:]}
    write_json_atomic(output_path, payload)
    return payload

def apply_paper_shadow_patches(pending_path: Path = PATCHES_PENDING, output_path: Path = SKILL_PATCH_INTEGRATION_LATEST) -> dict[str, Any]:
    pending = [row for row in read_jsonl(pending_path) if row.get("status") == "paper_shadow_only"]
    library = load_library()
    skills = library.get("skills") if isinstance(library.get("skills"), dict) else {}
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for patch in pending:
        setup_id = str(patch.get("setup_id") or "")
        skill = skills.get(setup_id)
        if not isinstance(skill, dict):
            skipped.append({"patch_id": patch.get("patch_id"), "setup_id": setup_id, "reason": "unknown_setup"})
            continue
        metadata = skill.get("metadata") if isinstance(skill.get("metadata"), dict) else {}
        existing = metadata.get("paper_shadow_patches") if isinstance(metadata.get("paper_shadow_patches"), list) else []
        if any(row.get("patch_id") == patch.get("patch_id") for row in existing if isinstance(row, dict)):
            skipped.append({"patch_id": patch.get("patch_id"), "setup_id": setup_id, "reason": "already_applied"})
            continue
        existing.append(
            {
                "patch_id": patch.get("patch_id"),
                "patch_type": patch.get("patch_type"),
                "invalidation": patch.get("invalidation"),
                "evidence": patch.get("evidence", {}),
                "applied_at": utc_now(),
                "scope": "paper_shadow_only",
                "live_enabled": False,
            }
        )
        metadata["paper_shadow_patches"] = existing[-20:]
        skill["metadata"] = metadata
        skill["paper_shadow_patch_count"] = len(metadata["paper_shadow_patches"])
        applied.append({"patch_id": patch.get("patch_id"), "setup_id": setup_id})
    if applied:
        library["skills"] = skills
        save_library(library)
    payload = {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "applied_count": len(applied), "skipped_count": len(skipped), "applied": applied, "skipped": skipped, "can_place_live_orders": False}
    write_json_atomic(output_path, payload)
    return payload
