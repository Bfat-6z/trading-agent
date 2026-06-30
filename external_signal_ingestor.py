"""External signal intake with prompt-injection stripping and trust scoring."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl_once, write_json_atomic
from data_trust import sanitize_external_text, source_policy
from live_permission_firewall import contains_live_intent, redact_secrets
from signal_source_registry import get_source
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
MEMORY_DIR = ROOT / "state" / "agent_memory"
EXTERNAL_SIGNALS = MEMORY_DIR / "external_signals.jsonl"
EXTERNAL_LATEST = MEMORY_DIR / "external_signals_latest.json"

ALLOWED_METADATA_KEYS = {"symbol", "timeframe", "url", "permalink", "channel", "posted_at", "source_posted_at", "parser_version"}


def signal_id(source_id: str, text: str) -> str:
    return "extsig_" + hashlib.sha256(f"{source_id}:{text}".encode("utf-8")).hexdigest()[:20]


def clean_signal_text(text: str) -> tuple[str, list[str]]:
    sanitized = sanitize_external_text(redact_secrets(text), max_chars=2000)
    warnings = ["prompt_injection_stripped" if flag == "external_instruction_stripped" else flag for flag in sanitized["flags"]]
    return sanitized["text"], sorted(set(warnings))


def ingest_external_signal(source_id: str, source_type: str, text: str, metadata: dict[str, Any] | None = None, path: Path = EXTERNAL_SIGNALS, latest_path: Path = EXTERNAL_LATEST) -> dict[str, Any]:
    cleaned, warnings = clean_signal_text(text)
    source = get_source(source_id, source_type)
    policy = source_policy(source_type, (metadata or {}).get("rights"))
    errors = []
    if contains_live_intent(text):
        warnings.append("live_intent_removed_external_signal")
        errors.append("external_signal_live_intent_quarantined")
    if "prompt_injection_stripped" in warnings and any(token in str(text).lower() for token in ("place order", "all-in", "all in", "50x", "100x")):
        errors.append("external_signal_live_intent_quarantined")
    symbol = (metadata or {}).get("symbol")
    timeframe = (metadata or {}).get("timeframe")
    if source_type == "screenshot" and (not symbol or not timeframe):
        errors.append("screenshot_missing_symbol_or_timeframe")
    safe_metadata = {key: value for key, value in (metadata or {}).items() if key in ALLOWED_METADATA_KEYS}
    text_meta = sanitize_external_text(text, max_chars=2000)
    row = {
        "schema_version": SCHEMA_VERSION,
        "signal_id": signal_id(source_id, cleaned),
        "ts": utc_now(),
        "source_id": source_id,
        "source_type": source_type,
        "trust_score": source.get("trust_score", 0.35),
        "text": cleaned,
        "text_hash": text_meta["content_hash"],
        "metadata": safe_metadata,
        "source_identity": {"source_id": source_id, "source_type": source_type, **safe_metadata},
        "taint_class": policy["taint_class"],
        "allowed_effect": policy["allowed_effect"],
        "status": "quarantined" if errors or "prompt_injection_stripped" in warnings else "hypothesis_only",
        "paper_only": True,
        "can_bypass_risk_gate": False,
        "errors": errors,
        "warnings": sorted(set(warnings)),
    }
    append_jsonl_once(path, row, "signal_id")
    write_json_atomic(latest_path, row)
    return row
