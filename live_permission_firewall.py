"""Hard live-execution firewall for Phase A paper learning."""
from __future__ import annotations

import hashlib
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
    "_request_futures_api",
    "account_balance",
    "authenticated_exchange",
    "cancel_order",
    "create_order",
    "delete_order",
    "exchange_private",
    "futures_change_leverage",
    "futures_create_order",
    "futures_order",
    "limit_order",
    "live_order",
    "market_order",
    "place_order",
    "set_leverage",
    "signed",
    "transfer",
    "withdraw",
}
LIVE_TEXT_WORDS = LIVE_ACTION_WORDS | {
    "lenh that",
    "lệnh thật",
    "live execution",
    "live trade",
    "private account",
    "real order",
    "real trade",
}
LIVE_SCALAR_VALUES = {"live", "real", "prod", "production", "mainnet"}
LIVE_READINESS_VALUES = {"ready_for_live", "readyforlive"}
READINESS_PERMISSION_KEYS = {"ready_for_live", "readyforlive", "live_readiness", "livereadiness"}
SECRET_KEY_WORDS = {
    "api_key",
    "apikey",
    "api_secret",
    "authorization",
    "bearer",
    "cookie",
    "password",
    "private_key",
    "secret",
    "signature",
    "token",
    "wallet",
}
PERMISSION_KEYS = {"can_place_live_orders", "canplaceliveorders", "live_permission", "livepermission", "can_trade_live", "cantradelive", "live_execution_enabled", "liveexecutionenabled", "live_eligible", "liveeligible", "can_submit_live_orders", "cansubmitliveorders"}
GENERIC_PERMISSION_KEYS = {"permission"}
MODE_KEYS = {"mode", "execution_mode", "executionmode", "trade_mode", "trademode"}
AUDIT_TEXT_KEYS = {"errors", "warnings", "reason", "safety_violations_corrected", "live_intent_paths", "secret_paths"}
SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|token|password|authorization|cookie|private[_-]?key)\s*[:=]\s*['\"]?[^\s'\"]+"),
    re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"),
]
MAX_SCAN_ITEMS = 5000


def _stable_fingerprint(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8", errors="ignore")).hexdigest()[:12]


def _redact_text(value: Any) -> str:
    text = str(value)
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(
            lambda match: match.group(0).split("=")[0] + "=[REDACTED]" if "=" in match.group(0) else "[REDACTED]",
            text,
        )
    return text


def _normalized(value: Any) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value))
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")

def _key_forms(value: Any) -> set[str]:
    normalized = _normalized(value)
    return {normalized, normalized.replace("_", "")}


def _looks_secret_key(key: Any) -> bool:
    normalized = _normalized(key)
    return any(word in normalized for word in SECRET_KEY_WORDS)


def _looks_secret_value(value: Any) -> bool:
    if value is None:
        return False
    text = str(value)
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def _contains_live_text(value: Any) -> bool:
    text = str(value).lower()
    normalized = _normalized(value)
    return (
        normalized in LIVE_SCALAR_VALUES
        or normalized in LIVE_READINESS_VALUES
        or any(word in text for word in LIVE_TEXT_WORDS)
        or bool(re.search(r"\blive[_\s-]*(order|trade|execution)\b", text))
    )

def _live_scalar_replacement(value: Any) -> str:
    normalized = _normalized(value)
    if normalized in LIVE_READINESS_VALUES:
        return "paper_only"
    if normalized in LIVE_SCALAR_VALUES:
        return "paper"
    return "[LIVE_INTENT_REDACTED]"


def sanitize_and_detect(value: Any, path: str = "$") -> dict[str, Any]:
    """Recursively sanitize payload shape while finding live/private execution intent."""
    live_paths: list[str] = []
    secret_paths: list[str] = []
    count = [0]

    def walk(node: Any, node_path: str, suppress_live_scan: bool = False) -> Any:
        count[0] += 1
        if count[0] > MAX_SCAN_ITEMS:
            live_paths.append(f"{node_path}:scan_limit_exceeded")
            return "[TRUNCATED]"
        if isinstance(node, dict):
            sanitized: dict[str, Any] = {}
            for raw_key, child in node.items():
                key = str(raw_key)
                normalized_key = _normalized(key)
                key_forms = _key_forms(key)
                child_path = f"{node_path}.{key}"
                child_suppressed = suppress_live_scan or normalized_key in AUDIT_TEXT_KEYS
                if key_forms & (PERMISSION_KEYS | GENERIC_PERMISSION_KEYS):
                    if not child_suppressed and bool(child):
                        live_paths.append(f"{child_path}:live_permission_true")
                    sanitized[key] = False
                    continue
                if key_forms & READINESS_PERMISSION_KEYS:
                    if not child_suppressed and bool(child):
                        live_paths.append(f"{child_path}:live_readiness_true")
                    sanitized[key] = False
                    continue
                if key_forms & MODE_KEYS and isinstance(child, (str, int, float, bool)):
                    if not child_suppressed and str(child).strip().lower().startswith("live"):
                        live_paths.append(f"{child_path}:live_mode")
                        sanitized[key] = "paper"
                        continue
                elif not child_suppressed and not (key_forms & PERMISSION_KEYS) and _contains_live_text(key):
                    live_paths.append(f"{child_path}:live_key")
                if _looks_secret_key(key):
                    secret_paths.append(child_path)
                    sanitized[key] = "[REDACTED]"
                    continue
                sanitized[key] = walk(child, child_path, child_suppressed)
            return sanitized
        if isinstance(node, (list, tuple, set)):
            return [walk(child, f"{node_path}[{idx}]", suppress_live_scan) for idx, child in enumerate(list(node))]
        if _contains_live_text(node):
            if not suppress_live_scan:
                live_paths.append(f"{node_path}:live_value")
            return _live_scalar_replacement(node)
        if _looks_secret_value(node):
            secret_paths.append(node_path)
            return "[REDACTED]"
        if isinstance(node, (str, int, float, bool)) or node is None:
            return node
        return _redact_text(node)

    sanitized = walk(value, path)
    return {
        "sanitized": sanitized,
        "live_intent": bool(live_paths),
        "live_paths": sorted(set(live_paths)),
        "secret_paths": sorted(set(secret_paths)),
        "fingerprint": _stable_fingerprint(sanitized),
    }


def redact_secrets(value: Any) -> str:
    return _redact_text(sanitize_and_detect(value)["sanitized"])


def contains_live_intent(request: Any) -> bool:
    return bool(sanitize_and_detect(request)["live_intent"])


def paper_action_allowed(decision: dict[str, Any]) -> bool:
    return bool(decision.get("paper_action_allowed", decision.get("allowed", False)))


def evaluate_live_permission(request: Any, config: dict[str, Any] | None = None, output_path: Path = LATEST_PATH) -> dict[str, Any]:
    effective = evaluate_mode(config or load_runtime_config())
    scanned = sanitize_and_detect(request)
    errors: list[str] = []
    if scanned["live_intent"]:
        errors.append("live_intent_blocked_phase_a")
    if effective.get("feature_flags", {}).get("live_orders") or effective.get("live_execution_enabled"):
        errors.append("live_config_blocked_phase_a")
    allowed = not errors
    payload = {
        "schema_version": SCHEMA_VERSION,
        "checked_at": utc_now(),
        "paper_action_allowed": allowed,
        "allowed": allowed,
        "live_permission": False,
        "can_place_live_orders": False,
        "mode": effective.get("mode"),
        "reason": "ok" if allowed else ";".join(errors),
        "errors": errors,
        "request_sanitized": scanned["sanitized"],
        "request_redacted": _redact_text(scanned["sanitized"]),
        "secret_paths": scanned["secret_paths"],
        "live_intent_paths": scanned["live_paths"],
        "request_fingerprint": scanned["fingerprint"],
    }
    write_json_atomic(output_path, payload)
    return payload


def require_paper_only(request: Any, config: dict[str, Any] | None = None) -> None:
    decision = evaluate_live_permission(request, config)
    if not paper_action_allowed(decision):
        raise PermissionError(decision["reason"])
