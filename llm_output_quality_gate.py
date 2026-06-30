"""Sanitize and score LLM outputs before downstream learning uses them."""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, write_json_atomic
from live_permission_firewall import contains_live_intent, sanitize_and_detect
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
MEMORY_DIR = ROOT / "state" / "agent_memory"
QUALITY_LATEST = MEMORY_DIR / "llm_output_quality_latest.json"
QUALITY_HISTORY = MEMORY_DIR / "llm_output_quality_history.jsonl"

REQUIRED_BY_KIND = {
    "council_role": {"role", "summary", "data_ids"},
    "council_synthesis": {"summary", "data_ids", "recommendations"},
    "skill_patch": {"setup_id", "patch_type", "invalidation", "data_ids"},
    "llm_reasoning": {"summary", "risk_proposal"},
}

UNSAFE_PERMISSION_KEYS = {
    "can_place_live_orders",
    "canplaceliveorders",
    "live_permission",
    "livepermission",
    "can_trade_live",
    "cantradelive",
    "can_loosen_risk",
    "canloosenrisk",
    "can_loosen",
    "canloosen",
    "ready_for_live",
    "readyforlive",
    "live_readiness",
    "livereadiness",
    "permission",
}

def _key_forms(value: Any) -> set[str]:
    normalized = re.sub(r"[^a-z0-9]+", "_", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value)).lower()).strip("_")
    return {normalized, normalized.replace("_", "")}


def _force_safe_flags(value: Any) -> tuple[Any, bool]:
    unsafe = False
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            safe_child, child_unsafe = _force_safe_flags(child)
            unsafe = unsafe or child_unsafe
            if _key_forms(key) & UNSAFE_PERMISSION_KEYS:
                if bool(child):
                    unsafe = True
                sanitized[str(key)] = False
            else:
                sanitized[str(key)] = safe_child
        return sanitized, unsafe
    if isinstance(value, list):
        rows = []
        for item in value:
            safe_item, item_unsafe = _force_safe_flags(item)
            unsafe = unsafe or item_unsafe
            rows.append(safe_item)
        return rows, unsafe
    return value, unsafe


def sanitize_output(payload: dict[str, Any], kind: str = "council_role") -> dict[str, Any]:
    errors = []
    warnings = []
    required = REQUIRED_BY_KIND.get(kind, set())
    missing = [field for field in required if not payload.get(field)]
    if missing:
        errors.append("missing:" + ",".join(sorted(missing)))
    scanned = sanitize_and_detect(payload)
    if scanned["live_intent"] or contains_live_intent(payload):
        errors.append("unsafe_live_intent")
    safe_payload, unsafe_permission = _force_safe_flags(scanned["sanitized"])
    unsafe_permission = unsafe_permission or any(str(path).endswith((":live_permission_true", ":live_readiness_true")) for path in scanned["live_paths"])
    if unsafe_permission:
        errors.append("unsafe_risk_or_live_permission")
    data_ids = payload.get("data_ids") if isinstance(payload.get("data_ids"), list) else []
    if len(data_ids) == 0:
        warnings.append("no_data_ids")
    safe_payload["can_place_live_orders"] = False
    safe_payload["live_permission"] = False
    safe_payload["can_loosen_risk"] = False
    result = {
        "schema_version": SCHEMA_VERSION,
        "checked_at": utc_now(),
        "kind": kind,
        "ok": not errors,
        "quality_score": 0.0 if errors else max(0.0, 1.0 - 0.15 * len(warnings)),
        "errors": errors,
        "warnings": warnings,
        "sanitized": safe_payload,
        "secret_paths": scanned["secret_paths"],
        "live_intent_paths": scanned["live_paths"],
        "can_place_live_orders": False,
        "live_permission": False,
    }
    write_json_atomic(QUALITY_LATEST, result)
    append_jsonl(QUALITY_HISTORY, result)
    return result
