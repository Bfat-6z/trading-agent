"""DONT_DO memory for repeated mistakes with decay and counter-evidence."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import read_json, write_json_atomic
from timebase import parse_utc, utc_now

ROOT = Path(__file__).resolve().parent
MEMORY_DIR = ROOT / "state" / "agent_memory"
DONT_DO_PATH = MEMORY_DIR / "dont_do_memory.json"


def rule_id(condition: str, scope: str = "global") -> str:
    return "dont_do_" + hashlib.sha256(f"{scope}:{condition.lower()}".encode("utf-8")).hexdigest()[:18]


def load_rules(path: Path = DONT_DO_PATH) -> dict[str, Any]:
    payload = read_json(path, default={})
    if not isinstance(payload, dict) or "rules" not in payload:
        return {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "rules": []}
    return payload


def save_rules(payload: dict[str, Any], path: Path = DONT_DO_PATH) -> dict[str, Any]:
    payload = {**payload, "schema_version": SCHEMA_VERSION, "updated_at": utc_now()}
    write_json_atomic(path, payload)
    return payload


def add_or_update_rule(condition: str, scope: str = "global", severity: str = "medium", evidence_delta: int = 1, expires_at: str | None = None, path: Path = DONT_DO_PATH, evidence_ids: list[str] | None = None) -> dict[str, Any]:
    payload = load_rules(path)
    rid = rule_id(condition, scope)
    clean_evidence = sorted({str(item) for item in (evidence_ids or []) if item})
    for row in payload["rules"]:
        if row.get("rule_id") == rid:
            row["evidence_count"] = int(row.get("evidence_count") or 0) + evidence_delta
            row["severity"] = severity
            row["expires_at"] = expires_at
            if clean_evidence:
                existing = [str(item) for item in row.get("evidence_ids", []) if item] if isinstance(row.get("evidence_ids"), list) else []
                row["evidence_ids"] = sorted(set(existing + clean_evidence))[-100:]
            row["updated_at"] = utc_now()
            save_rules(payload, path)
            return row
    row = {"schema_version": SCHEMA_VERSION, "rule_id": rid, "condition": condition, "scope": scope, "severity": severity, "evidence_count": evidence_delta, "evidence_ids": clean_evidence, "counter_evidence_count": 0, "expires_at": expires_at, "created_at": utc_now(), "updated_at": utc_now()}
    payload["rules"].append(row)
    save_rules(payload, path)
    return row


def add_counter_evidence(rule_id_value: str, amount: int = 1, path: Path = DONT_DO_PATH) -> dict[str, Any] | None:
    payload = load_rules(path)
    found = None
    for row in payload["rules"]:
        if row.get("rule_id") == rule_id_value:
            row["counter_evidence_count"] = int(row.get("counter_evidence_count") or 0) + amount
            row["updated_at"] = utc_now()
            found = row
            break
    save_rules(payload, path)
    return found


def rule_active(row: dict[str, Any]) -> bool:
    expires = parse_utc(row.get("expires_at"))
    if expires and parse_utc(utc_now()) and expires <= parse_utc(utc_now()):
        return False
    evidence = int(row.get("evidence_count") or 0)
    counter = int(row.get("counter_evidence_count") or 0)
    return evidence > counter


def match_rule(row: dict[str, Any], candidate: dict[str, Any]) -> bool:
    text = " ".join(str(v).lower() for v in candidate.values() if not isinstance(v, (dict, list)))
    condition_terms = [term for term in str(row.get("condition") or "").lower().replace("_", " ").split() if len(term) >= 3]
    if not condition_terms:
        return False
    hits = sum(1 for term in condition_terms if term in text)
    return hits >= max(1, min(3, len(condition_terms)))


def evaluate_candidate(candidate: dict[str, Any], path: Path = DONT_DO_PATH) -> dict[str, Any]:
    payload = load_rules(path)
    matches = [row for row in payload.get("rules", []) if rule_active(row) and match_rule(row, candidate)]
    high = any(row.get("severity") == "high" for row in matches)
    return {"schema_version": SCHEMA_VERSION, "checked_at": utc_now(), "blocked": bool(matches), "action": "block_paper" if high else "shadow_only" if matches else "allow", "matches": matches}
