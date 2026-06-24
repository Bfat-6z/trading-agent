"""Sanitize and score LLM outputs before downstream learning uses them."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, write_json_atomic
from live_permission_firewall import contains_live_intent, redact_secrets
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


def sanitize_output(payload: dict[str, Any], kind: str = "council_role") -> dict[str, Any]:
    errors = []
    warnings = []
    required = REQUIRED_BY_KIND.get(kind, set())
    missing = [field for field in required if not payload.get(field)]
    if missing:
        errors.append("missing:" + ",".join(sorted(missing)))
    if contains_live_intent(payload):
        errors.append("unsafe_live_intent")
    risk = payload.get("risk_proposal") if isinstance(payload.get("risk_proposal"), dict) else {}
    if risk.get("can_loosen_risk") or risk.get("can_place_live_orders"):
        errors.append("unsafe_risk_or_live_permission")
    data_ids = payload.get("data_ids") if isinstance(payload.get("data_ids"), list) else []
    if len(data_ids) == 0:
        warnings.append("no_data_ids")
    sanitized = {k: redact_secrets(v) if isinstance(v, str) else v for k, v in payload.items()}
    sanitized["can_place_live_orders"] = False
    sanitized["can_loosen_risk"] = False
    result = {"schema_version": SCHEMA_VERSION, "checked_at": utc_now(), "kind": kind, "ok": not errors, "quality_score": 0.0 if errors else max(0.0, 1.0 - 0.15 * len(warnings)), "errors": errors, "warnings": warnings, "sanitized": sanitized}
    write_json_atomic(QUALITY_LATEST, result)
    append_jsonl(QUALITY_HISTORY, result)
    return result
