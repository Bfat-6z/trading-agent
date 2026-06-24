"""Hard live-execution firewall for Phase A paper learning."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import write_json_atomic
from runtime_config import evaluate_mode, load_runtime_config
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
LATEST_PATH = MEMORY_DIR / "live_permission_firewall_latest.json"

LIVE_ACTION_WORDS = {
    "create_order",
    "place_order",
    "live_order",
    "futures_order",
    "market_order",
    "limit_order",
    "cancel_order",
    "set_leverage",
    "transfer",
    "withdraw",
}
SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[^\s'\"]+"),
    re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"),
]


def redact_secrets(value: Any) -> str:
    text = str(value)
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda match: match.group(0).split("=")[0] + "=[REDACTED]" if "=" in match.group(0) else "[REDACTED]", text)
    return text


def contains_live_intent(request: Any) -> bool:
    if isinstance(request, dict):
        fields = " ".join(str(v) for v in request.values() if not isinstance(v, (dict, list)))
        action = str(request.get("action") or request.get("tool") or request.get("intent") or "")
        mode = str(request.get("mode") or "")
        return any(word in action.lower() for word in LIVE_ACTION_WORDS) or mode.lower().startswith("live") or contains_live_intent(fields)
    text = str(request).lower()
    return any(word in text for word in LIVE_ACTION_WORDS) or "real order" in text or "lenh that" in text


def evaluate_live_permission(request: Any, config: dict[str, Any] | None = None, output_path: Path = LATEST_PATH) -> dict[str, Any]:
    effective = evaluate_mode(config or load_runtime_config())
    live_intent = contains_live_intent(request)
    errors: list[str] = []
    if live_intent:
        errors.append("live_intent_blocked_phase_a")
    if effective.get("feature_flags", {}).get("live_orders") or effective.get("live_execution_enabled"):
        errors.append("live_config_blocked_phase_a")
    allowed = not errors
    payload = {
        "schema_version": SCHEMA_VERSION,
        "checked_at": utc_now(),
        "allowed": allowed,
        "mode": effective.get("mode"),
        "reason": "ok" if allowed else ";".join(errors),
        "errors": errors,
        "request_redacted": redact_secrets(request),
    }
    write_json_atomic(output_path, payload)
    return payload


def require_paper_only(request: Any, config: dict[str, Any] | None = None) -> None:
    decision = evaluate_live_permission(request, config)
    if not decision["allowed"]:
        raise PermissionError(decision["reason"])
