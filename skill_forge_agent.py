"""Hermes-style setup skill patch forge with deterministic gates.

LLMs may propose patches, but this module decides whether evidence is sufficient
to stage/apply them. Applied patches are paper-only metadata; they never enable
live orders and never edit strategy code.
"""
from __future__ import annotations

import hashlib
import hmac
import argparse
import base64
import binascii
import json
import os
import re
import time
from urllib.parse import unquote
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, append_jsonl_once, read_jsonl, write_json_atomic
from data_trust import evaluate_evidence_for_learning
from setup_skill_library import load_library, save_library
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = ROOT / "state" / "agent_memory"
PATCHES_PENDING = MEMORY_DIR / "skill_patches_pending.jsonl"
PATCHES_APPLIED = MEMORY_DIR / "skill_patches_applied.jsonl"
PATCHES_REVERTED = MEMORY_DIR / "skill_patches_reverted.jsonl"
PATCH_REVIEWS = MEMORY_DIR / "skill_patch_reviews.jsonl"
PATCH_LEDGER = MEMORY_DIR / "skill_patch_ledger.jsonl"
SKILL_REGISTRY = MEMORY_DIR / "skill_registry.json"
PATCH_LOCK = MEMORY_DIR / "skill_patch_apply.lock"
SKILL_FORGE_LATEST = MEMORY_DIR / "skill_forge_latest.json"
SKILL_FORGE_HISTORY = MEMORY_DIR / "skill_forge_history.jsonl"
SKILL_PATCH_INTEGRATION_LATEST = MEMORY_DIR / "skill_patch_integration_latest.json"
POST_TRADE_REVIEWS = MEMORY_DIR / "post_trade_reviews.jsonl"
PID_FILE = STATE_DIR / "skill_forge_agent.pid"
HEARTBEAT_PATH = STATE_DIR / "skill_forge_agent_heartbeat.json"
STOP_FILE = STATE_DIR / "STOP_SKILL_FORGE_AGENT"

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
APPROVAL_REQUIRED_PATCH_TYPES = {"leverage_cap_by_setup"}
DENIED_PATCH_KEYS = {
    "code",
    "code_diff",
    "diff",
    "file",
    "files",
    "path",
    "target_path",
    "env",
    "secret",
    "api_key",
    "dependency",
    "dependencies",
    "import",
    "postinstall",
    "mcp_capability",
    "tool_capability",
    "firewall",
    "supervisor",
    "live_execution",
    "config",
}
DENIED_PATH_FRAGMENTS = {
    ".env",
    "firewall",
    "supervisor",
    "promotion_board.py",
    "real_scoring_board.py",
    "walk_forward_validator.py",
    "memory_consolidation_agent.py",
    "tests/",
    "test_",
    "fixtures",
    "requirements",
    "package.json",
}
REQUIRED_APPROVAL_FIELDS = {"signer", "reason", "scope", "ttl", "nonce", "approved_at", "rollback_owner", "evidence_ids", "before_hash", "after_hash"}
REQUIRED_APPROVAL_FIELDS |= {"signer_role", "signature", "expires_at"}
APPROVAL_HMAC_SECRET_ENV = "SKILL_APPROVAL_HMAC_SECRET"
AUTHORIZED_REPLAY_VERIFIERS = {"counterfactual_replay_agent", "post_trade_learning_agent", "experiment_swarm"}


def patch_id(patch: dict[str, Any]) -> str:
    return "skill_patch_" + hashlib.sha256(repr(sorted(patch.items())).encode("utf-8")).hexdigest()[:20]


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str, separators=(",", ":"))


def digest_payload(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

def hmac_digest(prefix: str, payload: Any, secret: str) -> str:
    return prefix + ":" + hmac.new(secret.encode("utf-8"), canonical_json(payload).encode("utf-8"), hashlib.sha256).hexdigest()

def parse_iso_time(value: Any) -> datetime | None:
    try:
        text = str(value or "").replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def library_hash(library: dict[str, Any]) -> str:
    return digest_payload(library.get("skills") if isinstance(library.get("skills"), dict) else library)


def skill_hash(skill: dict[str, Any]) -> str:
    return digest_payload(skill)


@contextmanager
def patch_apply_lock(lock_path: Path = PATCH_LOCK, timeout_seconds: float = 10.0):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout_seconds
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
        except FileExistsError:
            if time.time() >= deadline:
                raise TimeoutError(f"timed out waiting for skill patch lock: {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


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


def normalized_scope_values(value: str) -> list[str]:
    variants = []
    current = value.replace("\\", "/").lower()
    for _ in range(3):
        if current in variants:
            break
        variants.append(current)
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
    for token in re.findall(r"[A-Za-z0-9+/=_-]{8,}", value):
        for candidate in (token, token.replace("-", "+").replace("_", "/")):
            padded = candidate + "=" * (-len(candidate) % 4)
            try:
                decoded_bytes = base64.b64decode(padded, validate=True)
                decoded = decoded_bytes.decode("utf-8", errors="strict").replace("\\", "/").lower()
            except (binascii.Error, UnicodeDecodeError, ValueError):
                continue
            if decoded and decoded not in variants:
                variants.extend(normalized_scope_values(decoded))
    return variants


def scan_patch_scope(payload: Any, path: str = "$") -> list[str]:
    errors: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            key_l = str(key).lower()
            key_norm = "".join(ch for ch in key_l if ch.isalnum())
            current = f"{path}.{key_l}"
            denied_norm = {"".join(ch for ch in item if ch.isalnum()) for item in DENIED_PATCH_KEYS}
            if key_l in DENIED_PATCH_KEYS or key_norm in denied_norm:
                errors.append(f"denied_patch_key:{current}")
            errors.extend(scan_patch_scope(value, current))
    elif isinstance(payload, list):
        for idx, item in enumerate(payload):
            errors.extend(scan_patch_scope(item, f"{path}[{idx}]"))
    elif isinstance(payload, str):
        for value_l in normalized_scope_values(payload):
            for fragment in DENIED_PATH_FRAGMENTS:
                if fragment in value_l:
                    errors.append(f"denied_patch_value:{path}:{fragment}")
            if re.search(r"\.py(?:$|[^a-z0-9_])", value_l):
                errors.append(f"denied_patch_value:{path}:.py")
    return sorted(set(errors))


def requires_approval(patch: dict[str, Any]) -> bool:
    patch_type = str(patch.get("patch_type") or "")
    if patch_type in APPROVAL_REQUIRED_PATCH_TYPES:
        return True
    return any(key in patch for key in ("risk_fraction", "allocation_threshold", "max_risk_fraction", "capability_mask", "tool_capability"))


def approval_signature_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    return {field: manifest.get(field) for field in sorted(REQUIRED_APPROVAL_FIELDS - {"signature"})}


def expected_approval_signature(manifest: dict[str, Any], secret: str | None = None) -> str:
    secret_value = secret if secret is not None else os.getenv(APPROVAL_HMAC_SECRET_ENV, "")
    if not secret_value:
        return ""
    return hmac_digest("sig:v1", approval_signature_payload(manifest), secret_value)


def replay_proof_payload(patch: dict[str, Any], before_hash: str, after_hash: str, proof: dict[str, Any]) -> dict[str, Any]:
    changed = proof.get("changed_decisions") if isinstance(proof.get("changed_decisions"), list) else []
    return {
        "patch_id": patch.get("patch_id"),
        "setup_id": patch.get("setup_id"),
        "source": proof.get("source"),
        "verifier": proof.get("verifier"),
        "replay_artifact_id": proof.get("replay_artifact_id"),
        "changed_decisions": [str(item) for item in changed],
        "decision_delta_count": safe_int(proof.get("decision_delta_count"), len(changed)),
        "before_hash": before_hash,
        "after_hash": after_hash,
    }


def expected_replay_proof_hash(patch: dict[str, Any], before_hash: str, after_hash: str, proof: dict[str, Any]) -> str:
    return digest_payload(replay_proof_payload(patch, before_hash, after_hash, proof))


def expected_replay_proof_signature(patch: dict[str, Any], before_hash: str, after_hash: str, proof: dict[str, Any], secret: str | None = None) -> str:
    secret_value = secret if secret is not None else os.getenv(APPROVAL_HMAC_SECRET_ENV, "")
    if not secret_value:
        return ""
    payload = replay_proof_payload(patch, before_hash, after_hash, proof)
    payload["proof_hash"] = expected_replay_proof_hash(patch, before_hash, after_hash, proof)
    return hmac_digest("replay_sig:v1", payload, secret_value)


def verified_replay_proof(patch: dict[str, Any], before_hash: str, after_hash: str) -> bool:
    proof = patch.get("decision_diff_proof") if isinstance(patch.get("decision_diff_proof"), dict) else {}
    if proof.get("source") != "deterministic_replay":
        return False
    if str(proof.get("verifier") or "") not in AUTHORIZED_REPLAY_VERIFIERS:
        return False
    changed = proof.get("changed_decisions") if isinstance(proof.get("changed_decisions"), list) else []
    if not changed and safe_int(proof.get("decision_delta_count")) <= 0:
        return False
    expected_hash = expected_replay_proof_hash(patch, before_hash, after_hash, proof)
    expected_sig = expected_replay_proof_signature(patch, before_hash, after_hash, proof)
    if not expected_sig:
        return False
    return proof.get("proof_hash") == expected_hash and hmac.compare_digest(str(proof.get("signature") or ""), expected_sig)


def manifest_errors(
    manifest: dict[str, Any] | None,
    *,
    expected_before_hash: str | None = None,
    expected_after_hash: str | None = None,
    expected_evidence_ids: list[str] | None = None,
    used_nonces: set[str] | None = None,
    required: bool = False,
) -> list[str]:
    if not required and not manifest:
        return []
    if not isinstance(manifest, dict):
        return ["missing_approval_manifest"]
    errors: list[str] = []
    missing = sorted(field for field in REQUIRED_APPROVAL_FIELDS if not manifest.get(field))
    errors.extend(f"missing_approval_field:{field}" for field in missing)
    if expected_before_hash and manifest.get("before_hash") and manifest.get("before_hash") != expected_before_hash:
        errors.append("approval_before_hash_mismatch")
    if expected_after_hash and manifest.get("after_hash") and manifest.get("after_hash") != expected_after_hash:
        errors.append("approval_after_hash_mismatch")
    expected_ids = sorted(set(expected_evidence_ids or []))
    manifest_ids = sorted(set(str(item) for item in manifest.get("evidence_ids", []) if item)) if isinstance(manifest.get("evidence_ids"), list) else []
    if expected_ids and manifest_ids != expected_ids:
        errors.append("approval_evidence_ids_mismatch")
    if str(manifest.get("scope") or "") not in {"metadata_only", "paper_only_metadata"}:
        errors.append("approval_scope_not_metadata_only")
    if str(manifest.get("signer_role") or "") not in {"approver", "admin", "risk_approver"}:
        errors.append("approval_signer_role_not_authorized")
    expected_sig = expected_approval_signature(manifest)
    if not expected_sig:
        errors.append("approval_signature_secret_missing")
    elif not hmac.compare_digest(str(manifest.get("signature") or ""), expected_sig):
        errors.append("approval_signature_invalid")
    approved_dt = parse_iso_time(manifest.get("approved_at"))
    expires_dt = parse_iso_time(manifest.get("expires_at"))
    now_dt = parse_iso_time(utc_now())
    if not approved_dt or not expires_dt:
        errors.append("approval_time_invalid")
    elif expires_dt <= approved_dt:
        errors.append("approval_expiry_invalid")
    elif now_dt and expires_dt <= now_dt:
        errors.append("approval_expired")
    if used_nonces is not None and str(manifest.get("nonce") or "") in used_nonces:
        errors.append("approval_nonce_reused")
    return sorted(set(errors))


def learning_claim_for_patch(patch: dict[str, Any], applied_row: dict[str, Any], before_skill: dict[str, Any], after_skill: dict[str, Any]) -> dict[str, Any]:
    changed = before_skill != after_skill
    has_decision_diff = verified_replay_proof(patch, skill_hash(before_skill), skill_hash(after_skill))
    return {
        "schema_version": SCHEMA_VERSION,
        "claim_type": "learned" if changed and has_decision_diff else "hypothesis_only",
        "patch_id": patch.get("patch_id"),
        "changed_skill_ids": [str(patch.get("setup_id"))] if changed else [],
        "deterministic_before_after_decision_diff": has_decision_diff,
        "applied_manifest_hash": applied_row.get("apply_manifest_hash"),
        "can_place_live_orders": False,
        "can_loosen_risk": False,
    }


def _existing_ids(path: Path, id_fields: tuple[str, ...]) -> set[str]:
    ids: set[str] = set()
    for row in read_jsonl(path):
        for field in id_fields:
            if row.get(field):
                ids.add(str(row.get(field)))
    return ids


def resolve_objective_evidence(evidence: dict[str, Any], reviews_path: Path | None = None) -> dict[str, Any]:
    errors: list[str] = []
    resolved: list[str] = []
    declared_resolved = {str(item) for item in evidence.get("resolved_objective_ids", []) if item} if isinstance(evidence.get("resolved_objective_ids"), list) else set()
    checks = [
        ("post_trade_review_ids", reviews_path or POST_TRADE_REVIEWS, ("review_id",)),
    ]
    for key, path, id_fields in checks:
        requested = evidence.get(key)
        if not requested:
            continue
        requested_ids = [str(item) for item in requested if item] if isinstance(requested, list) else [str(requested)]
        existing = _existing_ids(path, id_fields)
        for item in requested_ids:
            if item in existing or item in declared_resolved:
                resolved.append(item)
            else:
                errors.append(f"unresolved_objective_evidence:{key}:{item}")
    return {"ok": not errors, "errors": errors, "resolved_ids": sorted(set(resolved))}


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


def validate_patch(patch: dict[str, Any], evidence: dict[str, Any], reviews_path: Path | None = None) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    errors.extend(scan_patch_scope(patch))
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
    trust = evaluate_evidence_for_learning(evidence, requested_effect="skill_patch")
    errors.extend(trust["errors"])
    warnings.extend(trust["warnings"])
    resolution = resolve_objective_evidence(evidence, reviews_path=reviews_path)
    errors.extend(resolution["errors"])
    if requires_approval(patch):
        warnings.append("approval_required_before_apply")
        errors.extend(manifest_errors(patch.get("approval_manifest") if isinstance(patch.get("approval_manifest"), dict) else None, expected_evidence_ids=ids, required=False))
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
        "evidence_trust": trust,
        "evidence_resolution": resolution,
        "lifecycle": lifecycle,
        "evidence_ids": ids,
        "approval_required": requires_approval(patch),
        "scope_errors": [error for error in errors if error.startswith("denied_patch_")],
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
    objective_reviews_path: Path | None = None,
) -> dict[str, Any]:
    resolution = resolve_objective_evidence(evidence, reviews_path=objective_reviews_path)
    if resolution.get("resolved_ids"):
        evidence = {**evidence, "resolved_objective_ids": resolution["resolved_ids"]}
    row = {
        **patch,
        "schema_version": SCHEMA_VERSION,
        "patch_id": patch.get("patch_id") or patch_id(patch),
        "proposed_at": utc_now(),
        "evidence": evidence,
        "live_enabled": False,
    }
    validation = validate_patch(row, evidence, reviews_path=objective_reviews_path)
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

def write_heartbeat(status: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    row = {"schema_version": SCHEMA_VERSION, "ts": utc_now(), "pid": os.getpid(), "status": status, **(payload or {})}
    write_json_atomic(HEARTBEAT_PATH, row)
    return row

def interruptible_sleep(seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline and not STOP_FILE.exists():
        write_heartbeat("waiting", {"next_run_seconds": round(max(0.0, deadline - time.time()), 1)})
        time.sleep(min(30.0, max(0.0, deadline - time.time())))


def patch_already_in_skill(skill: dict[str, Any], patch: dict[str, Any]) -> bool:
    metadata = skill.get("metadata") if isinstance(skill.get("metadata"), dict) else {}
    existing = metadata.get("paper_shadow_patches") if isinstance(metadata.get("paper_shadow_patches"), list) else []
    return any(row.get("patch_id") == patch.get("patch_id") for row in existing if isinstance(row, dict))


def patch_applied_marker(patch: dict[str, Any]) -> str:
    return str(patch.get("proposed_at") or patch.get("patch_id") or "paper_shadow_patch")


def apply_patch_metadata(skill: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    metadata = skill.get("metadata") if isinstance(skill.get("metadata"), dict) else {}
    existing = metadata.get("paper_shadow_patches") if isinstance(metadata.get("paper_shadow_patches"), list) else []
    patch_row = {
        "patch_id": patch.get("patch_id"),
        "patch_type": patch.get("patch_type"),
        "status": "paper_only_applied",
        "invalidation": patch.get("invalidation"),
        "rollback_criteria": patch.get("rollback_criteria"),
        "evidence_ids": patch.get("evidence_ids") or evidence_ids(patch.get("evidence") if isinstance(patch.get("evidence"), dict) else {}),
        "evidence": patch.get("evidence", {}),
        "symbols": [str(item).upper() for item in patch.get("symbols", []) if item] if isinstance(patch.get("symbols"), list) else [],
        "applied_at": patch_applied_marker(patch),
        "scope": "paper_only",
        "live_enabled": False,
    }
    if patch.get("patch_type") == "leverage_cap_by_setup":
        patch_row["max_leverage"] = patch.get("max_leverage")
    elif patch.get("patch_type") == "min_score_adjustment_by_setup":
        patch_row["min_score_delta"] = patch.get("min_score_delta")
    existing.append(patch_row)
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


def preview_patch_application(skill: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    before = deepcopy(skill)
    after = apply_patch_metadata(deepcopy(skill), patch)
    return {
        "before_skill_hash": skill_hash(before),
        "after_skill_hash": skill_hash(after),
        "before_skill": before,
        "after_skill": after,
    }


def validate_apply_gate(patch: dict[str, Any], skill: dict[str, Any], used_nonces: set[str] | None = None) -> dict[str, Any]:
    evidence = patch.get("evidence") if isinstance(patch.get("evidence"), dict) else {}
    validation = validate_patch(patch, evidence)
    errors = list(validation.get("errors") or [])
    preview = preview_patch_application(skill, patch)
    if requires_approval(patch):
        errors.extend(
            manifest_errors(
                patch.get("approval_manifest") if isinstance(patch.get("approval_manifest"), dict) else None,
                expected_before_hash=preview["before_skill_hash"],
                expected_after_hash=preview["after_skill_hash"],
                expected_evidence_ids=patch.get("evidence_ids") or evidence_ids(evidence),
                used_nonces=used_nonces,
                required=True,
            )
        )
    if patch.get("base_skill_hash") and patch.get("base_skill_hash") != preview["before_skill_hash"]:
        errors.append("skill_base_hash_mismatch")
    return {**validation, **preview, "ok": not errors, "errors": sorted(set(errors))}


def append_patch_ledger(event: str, row: dict[str, Any], ledger_path: Path = PATCH_LEDGER) -> None:
    append_jsonl(ledger_path, {"schema_version": SCHEMA_VERSION, "event": event, "ts": utc_now(), **row})


def write_skill_registry(library: dict[str, Any], registry_path: Path = SKILL_REGISTRY) -> dict[str, Any]:
    rows = []
    skills = library.get("skills") if isinstance(library.get("skills"), dict) else {}
    for setup_id, skill in sorted(skills.items()):
        if not isinstance(skill, dict):
            continue
        metadata = skill.get("metadata") if isinstance(skill.get("metadata"), dict) else {}
        rows.append(
            {
                "setup_id": setup_id,
                "setup_contract_id": skill.get("setup_contract_id") or f"{setup_id}.contract",
                "version": int(skill.get("version") or 1),
                "status": "retired" if metadata.get("paper_only_retired") else "active",
                "contract_hash": skill_hash(skill),
                "paper_shadow_patch_count": int(skill.get("paper_shadow_patch_count") or 0),
                "compatibility_class": "paper_metadata",
                "can_place_live_orders": False,
            }
        )
    payload = {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "skill_count": len(rows), "skills": rows, "registry_hash": digest_payload(rows), "can_place_live_orders": False}
    write_json_atomic(registry_path, payload)
    return payload


def apply_paper_shadow_patches(
    pending_path: Path = PATCHES_PENDING,
    output_path: Path = SKILL_PATCH_INTEGRATION_LATEST,
    applied_path: Path = PATCHES_APPLIED,
    latest_path: Path | None = SKILL_FORGE_LATEST,
    registry_path: Path = SKILL_REGISTRY,
    ledger_path: Path = PATCH_LEDGER,
    lock_path: Path = PATCH_LOCK,
) -> dict[str, Any]:
    with patch_apply_lock(lock_path):
        pending = [row for row in read_jsonl(pending_path) if row.get("status") == "paper_shadow_only"]
        applied_rows_existing = read_jsonl(applied_path)
        already_applied = {str(row.get("patch_id")) for row in applied_rows_existing if row.get("patch_id")}
        used_nonces = {
            str(((row.get("apply_manifest") or {}).get("approval_manifest") or {}).get("nonce"))
            for row in applied_rows_existing
            if isinstance(row.get("apply_manifest"), dict)
            and isinstance((row.get("apply_manifest") or {}).get("approval_manifest"), dict)
            and ((row.get("apply_manifest") or {}).get("approval_manifest") or {}).get("nonce")
        }
        library = load_library()
        skills = library.get("skills") if isinstance(library.get("skills"), dict) else {}
        applied: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for patch in pending:
            if str(patch.get("patch_id")) in already_applied:
                skipped.append({"patch_id": patch.get("patch_id"), "setup_id": patch.get("setup_id"), "reason": "already_applied"})
                continue
            setup_id = str(patch.get("setup_id") or "")
            skill = skills.get(setup_id)
            if not isinstance(skill, dict):
                skipped.append({"patch_id": patch.get("patch_id"), "setup_id": setup_id, "reason": "unknown_setup"})
                continue
            gate = validate_apply_gate(patch, skill, used_nonces=used_nonces)
            if not gate.get("ok"):
                skipped.append({"patch_id": patch.get("patch_id"), "setup_id": setup_id, "reason": "failed_apply_gate", "errors": gate.get("errors")})
                append_patch_ledger("patch_apply_rejected", {"patch_id": patch.get("patch_id"), "setup_id": setup_id, "errors": gate.get("errors")}, ledger_path)
                continue
            if patch_already_in_skill(skill, patch):
                skipped.append({"patch_id": patch.get("patch_id"), "setup_id": setup_id, "reason": "already_applied"})
                continue
            before_skill = gate["before_skill"]
            after_skill = gate["after_skill"]
            library_hash_before = library_hash(library)
            skills[setup_id] = after_skill
            apply_manifest = {
                "patch_id": patch.get("patch_id"),
                "setup_id": setup_id,
                "base_skill_hash": gate["before_skill_hash"],
                "target_skill_hash": gate["after_skill_hash"],
                "library_hash_before": library_hash_before,
                "approval_manifest": patch.get("approval_manifest") if isinstance(patch.get("approval_manifest"), dict) else None,
                "rollback_plan": patch.get("rollback_criteria"),
                "canary_window": patch.get("canary_window") or "paper_shadow_only",
                "rollback_thresholds": patch.get("rollback_thresholds") or {"future_expectancy": "<=0"},
                "live_enabled": False,
            }
            applied_row = {
                "schema_version": SCHEMA_VERSION,
                "patch_id": patch.get("patch_id"),
                "setup_id": setup_id,
                "status": "paper_only_applied",
                "applied_at": utc_now(),
                "lifecycle": [*(patch.get("lifecycle") or ["proposed", "schema_valid", "evidence_checked"]), "paper_only_applied"],
                "base_skill_hash": gate["before_skill_hash"],
                "target_skill_hash": gate["after_skill_hash"],
                "apply_manifest": apply_manifest,
                "apply_manifest_hash": digest_payload(apply_manifest),
                "learning_claim": None,
                "live_enabled": False,
                "can_place_live_orders": False,
                "can_loosen_risk": False,
            }
            applied_row["learning_claim"] = learning_claim_for_patch(patch, applied_row, before_skill, after_skill)
            append_jsonl_once(applied_path, applied_row, "patch_id")
            append_patch_ledger("patch_applied", applied_row, ledger_path)
            already_applied.add(str(patch.get("patch_id")))
            manifest = patch.get("approval_manifest") if isinstance(patch.get("approval_manifest"), dict) else {}
            if manifest.get("nonce"):
                used_nonces.add(str(manifest["nonce"]))
            applied.append(applied_row)
        if applied:
            library["skills"] = skills
            save_library(library)
        registry = write_skill_registry(library, registry_path=registry_path)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "applied_count": len(applied),
        "skipped_count": len(skipped),
        "applied": applied,
        "skipped": skipped,
        "registry_hash": registry.get("registry_hash"),
        "can_place_live_orders": False,
        "can_loosen_risk": False,
    }
    write_json_atomic(output_path, payload)
    if latest_path is not None:
        write_latest(pending_path=pending_path, output_path=latest_path, applied_path=applied_path)
    return payload


def remove_patch_metadata(skill: dict[str, Any], patch_id_value: str) -> tuple[dict[str, Any], dict[str, Any] | None, bool]:
    metadata = skill.get("metadata") if isinstance(skill.get("metadata"), dict) else {}
    patches = [row for row in metadata.get("paper_shadow_patches", []) if isinstance(row, dict)] if isinstance(metadata.get("paper_shadow_patches"), list) else []
    target = next((row for row in patches if row.get("patch_id") == patch_id_value), None)
    if not target:
        return skill, None, False
    remaining = [row for row in patches if row.get("patch_id") != patch_id_value]
    metadata["paper_shadow_patches"] = remaining
    patch_type = target.get("patch_type")
    remaining_same_type = any(row.get("patch_type") == patch_type for row in remaining)
    if patch_type == "setup_retirement" and not remaining_same_type:
        metadata.pop("paper_only_retired", None)
    elif patch_type == "leverage_cap_by_setup":
        caps = [safe_float(row.get("max_leverage")) for row in remaining if row.get("patch_type") == patch_type and safe_float(row.get("max_leverage")) > 0]
        if caps:
            metadata["paper_only_leverage_cap"] = caps[-1]
        else:
            metadata.pop("paper_only_leverage_cap", None)
    elif patch_type == "min_score_adjustment_by_setup":
        deltas = [safe_float(row.get("min_score_delta")) for row in remaining if row.get("patch_type") == patch_type and row.get("min_score_delta") is not None]
        if deltas:
            metadata["paper_only_min_score_adjustment"] = deltas[-1]
        else:
            metadata.pop("paper_only_min_score_adjustment", None)
    elif patch_type in {"symbol_blacklist", "symbol_graylist"}:
        key = "paper_only_symbol_blacklist" if patch_type == "symbol_blacklist" else "paper_only_symbol_graylist"
        rebuilt: set[str] = set()
        for row in remaining:
            if row.get("patch_type") == patch_type:
                source = row.get("symbols") if isinstance(row.get("symbols"), list) else (row.get("patch", {}) or {}).get("symbols") if isinstance(row.get("patch"), dict) else []
                rebuilt.update(str(item).upper() for item in source if item)
        if rebuilt:
            metadata[key] = sorted(rebuilt)
        else:
            metadata.pop(key, None)
    skill["metadata"] = metadata
    skill["paper_shadow_patch_count"] = len(remaining)
    return skill, target, True


def rollback_paper_shadow_patch(
    patch_id_value: str,
    *,
    applied_path: Path = PATCHES_APPLIED,
    reverted_path: Path = PATCHES_REVERTED,
    output_path: Path = SKILL_PATCH_INTEGRATION_LATEST,
    registry_path: Path = SKILL_REGISTRY,
    ledger_path: Path = PATCH_LEDGER,
    lock_path: Path = PATCH_LOCK,
    inverse_patch: bool = False,
) -> dict[str, Any]:
    with patch_apply_lock(lock_path):
        applied_rows = [row for row in read_jsonl(applied_path) if row.get("patch_id")]
        target = next((row for row in applied_rows if row.get("patch_id") == patch_id_value), None)
        if not target:
            result = {"schema_version": SCHEMA_VERSION, "rolled_back": False, "patch_id": patch_id_value, "errors": ["patch_not_applied"], "can_place_live_orders": False}
            write_json_atomic(output_path, result)
            return result
        setup_id = str(target.get("setup_id") or "")
        target_index = next((idx for idx, row in enumerate(applied_rows) if row.get("patch_id") == patch_id_value), -1)
        newer = [row for idx, row in enumerate(applied_rows) if idx > target_index and row.get("setup_id") == setup_id and row.get("patch_id") != patch_id_value]
        if newer and not inverse_patch:
            result = {"schema_version": SCHEMA_VERSION, "rolled_back": False, "patch_id": patch_id_value, "errors": ["rollback_head_mismatch"], "newer_patch_ids": [row.get("patch_id") for row in newer], "can_place_live_orders": False}
            write_json_atomic(output_path, result)
            return result
        library = load_library()
        skills = library.get("skills") if isinstance(library.get("skills"), dict) else {}
        skill = skills.get(setup_id)
        if not isinstance(skill, dict):
            result = {"schema_version": SCHEMA_VERSION, "rolled_back": False, "patch_id": patch_id_value, "errors": ["unknown_setup"], "can_place_live_orders": False}
            write_json_atomic(output_path, result)
            return result
        before_hash = skill_hash(skill)
        updated_skill, removed, ok = remove_patch_metadata(deepcopy(skill), patch_id_value)
        if not ok:
            result = {"schema_version": SCHEMA_VERSION, "rolled_back": False, "patch_id": patch_id_value, "errors": ["patch_metadata_not_found"], "can_place_live_orders": False}
            write_json_atomic(output_path, result)
            return result
        skills[setup_id] = updated_skill
        library["skills"] = skills
        save_library(library)
        registry = write_skill_registry(library, registry_path=registry_path)
        row = {
            "schema_version": SCHEMA_VERSION,
            "patch_id": patch_id_value,
            "setup_id": setup_id,
            "status": "paper_only_reverted",
            "reverted_at": utc_now(),
            "before_skill_hash": before_hash,
            "after_skill_hash": skill_hash(updated_skill),
            "removed_patch": removed,
            "inverse_patch": bool(inverse_patch),
            "registry_hash": registry.get("registry_hash"),
            "can_place_live_orders": False,
            "can_loosen_risk": False,
        }
        append_jsonl_once(reverted_path, row, "patch_id")
        append_patch_ledger("patch_reverted", row, ledger_path)
        result = {"schema_version": SCHEMA_VERSION, "rolled_back": True, "patch_id": patch_id_value, "row": row, "can_place_live_orders": False, "can_loosen_risk": False}
        write_json_atomic(output_path, result)
        return result


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
                objective_reviews_path=reviews_path,
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
    parser.add_argument("--interval-seconds", type=float, default=1800.0)
    parser.add_argument("--status", action="store_true")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.status:
        print({"pid_file": str(PID_FILE), "heartbeat": str(HEARTBEAT_PATH), "latest": str(SKILL_FORGE_LATEST), "stop_file": str(STOP_FILE)})
        return 0
    if args.interval_seconds <= 0:
        raise SystemExit("--interval-seconds must be positive")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    while not STOP_FILE.exists():
        result = run_once(
            reviews_path=POST_TRADE_REVIEWS,
            pending_path=PATCHES_PENDING,
            review_path=PATCH_REVIEWS,
            latest_path=SKILL_FORGE_LATEST,
            integration_output_path=SKILL_PATCH_INTEGRATION_LATEST,
            applied_path=PATCHES_APPLIED,
            min_sample=args.min_sample,
            apply=bool(args.apply),
        )
        append_jsonl(SKILL_FORGE_HISTORY, result)
        write_heartbeat("ok", {"candidate_count": result.get("candidate_count"), "accepted_count": result.get("accepted_count"), "apply": bool(args.apply)})
        print(f"skill_forge_agent candidates={result.get('candidate_count')} accepted={result.get('accepted_count')} apply={bool(args.apply)}", flush=True)
        if args.once:
            break
        interruptible_sleep(args.interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
