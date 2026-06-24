"""External signal intake with prompt-injection stripping and trust scoring."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl_once, write_json_atomic
from live_permission_firewall import contains_live_intent, redact_secrets
from signal_source_registry import get_source
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
MEMORY_DIR = ROOT / "state" / "agent_memory"
EXTERNAL_SIGNALS = MEMORY_DIR / "external_signals.jsonl"
EXTERNAL_LATEST = MEMORY_DIR / "external_signals_latest.json"

INJECTION_PATTERNS = [r"(?i)ignore previous instructions", r"(?i)system prompt", r"(?i)developer message", r"(?i)place order", r"(?i)all[- ]?in"]


def signal_id(source_id: str, text: str) -> str:
    return "extsig_" + hashlib.sha256(f"{source_id}:{text}".encode("utf-8")).hexdigest()[:20]


def clean_signal_text(text: str) -> tuple[str, list[str]]:
    warnings = []
    cleaned = redact_secrets(text)
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, cleaned):
            warnings.append("prompt_injection_stripped")
            cleaned = re.sub(pattern, "[stripped]", cleaned)
    return cleaned[:2000], sorted(set(warnings))


def ingest_external_signal(source_id: str, source_type: str, text: str, metadata: dict[str, Any] | None = None, path: Path = EXTERNAL_SIGNALS, latest_path: Path = EXTERNAL_LATEST) -> dict[str, Any]:
    cleaned, warnings = clean_signal_text(text)
    source = get_source(source_id, source_type)
    errors = []
    if contains_live_intent(text):
        warnings.append("live_intent_removed_external_signal")
    symbol = (metadata or {}).get("symbol")
    timeframe = (metadata or {}).get("timeframe")
    if source_type == "screenshot" and (not symbol or not timeframe):
        errors.append("screenshot_missing_symbol_or_timeframe")
    row = {
        "schema_version": SCHEMA_VERSION,
        "signal_id": signal_id(source_id, cleaned),
        "ts": utc_now(),
        "source_id": source_id,
        "source_type": source_type,
        "trust_score": source.get("trust_score", 0.35),
        "text": cleaned,
        "metadata": metadata or {},
        "status": "incomplete" if errors else "hypothesis_only",
        "paper_only": True,
        "can_bypass_risk_gate": False,
        "errors": errors,
        "warnings": sorted(set(warnings)),
    }
    append_jsonl_once(path, row, "signal_id")
    write_json_atomic(latest_path, row)
    return row
