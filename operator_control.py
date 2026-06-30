"""Operator command contract for paper-only recovery controls."""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, read_jsonl, write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
OPERATOR_AUDIT = MEMORY_DIR / "operator_command_audit.jsonl"
OPERATOR_LATEST = MEMORY_DIR / "operator_command_latest.json"

ALLOWED_COMMANDS = {
    "status",
    "pause_opens",
    "resume_opens",
    "cancel_pending",
    "reduce_exposure",
    "kill_switch_on",
    "backup_now",
    "restore_drill",
    "rotate_token",
    "quarantine_daemon",
    "acknowledge_incident",
    "close_incident",
}
RACI = {
    "primary_operator": "daily operation and first ack",
    "backup_operator": "takeover when primary misses SLA",
    "incident_commander": "Sev1/Sev2 coordination",
    "dashboard_security_owner": "dashboard/token/tunnel exposure",
    "backup_key_custodian": "restore and backup key custody",
    "trial_owner": "paper-trial validity and abort authority",
    "hotfix_approver": "approve emergency code changes",
    "final_report_approver": "accept closure evidence",
}
ESCALATION_POLICY = {
    "Sev1": {"ack_sla_minutes": 5, "escalate_after_minutes": 10, "page": True},
    "Sev2": {"ack_sla_minutes": 15, "escalate_after_minutes": 30, "page": True},
    "Sev3": {"ack_sla_minutes": 1440, "escalate_after_minutes": 2880, "page": False},
    "Sev4": {"ack_sla_minutes": 10080, "escalate_after_minutes": 20160, "page": False},
}

def command_signature(command: str, actor: str, nonce: str, payload: dict[str, Any] | None = None, secret: str = "paper-only-local-operator-secret") -> str:
    material = json.dumps({"command": command, "actor": actor, "nonce": nonce, "payload": payload or {}}, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    digest = hmac.new(secret.encode("utf-8"), material.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest}"

def validate_ops_governance() -> dict[str, Any]:
    required_roles = {"primary_operator", "backup_operator", "incident_commander", "dashboard_security_owner", "backup_key_custodian", "trial_owner", "hotfix_approver", "final_report_approver"}
    required_commands = {"status", "pause_opens", "resume_opens", "cancel_pending", "reduce_exposure", "kill_switch_on", "backup_now", "restore_drill", "rotate_token", "quarantine_daemon", "acknowledge_incident", "close_incident"}
    missing_roles = sorted(required_roles - set(RACI))
    missing_commands = sorted(required_commands - ALLOWED_COMMANDS)
    missing_escalation = [severity for severity in ("Sev1", "Sev2", "Sev3", "Sev4") if severity not in ESCALATION_POLICY]
    return {
        "schema_version": SCHEMA_VERSION,
        "checked_at": utc_now(),
        "ok": not (missing_roles or missing_commands or missing_escalation),
        "missing_roles": missing_roles,
        "missing_commands": missing_commands,
        "missing_escalation": missing_escalation,
        "raci": RACI,
        "allowed_commands": sorted(ALLOWED_COMMANDS),
        "escalation_policy": ESCALATION_POLICY,
    }

def record_operator_command(
    command: str,
    actor: str,
    payload: dict[str, Any] | None = None,
    *,
    nonce: str,
    signature: str | None = None,
    audit_path: Path = OPERATOR_AUDIT,
    latest_path: Path = OPERATOR_LATEST,
) -> dict[str, Any]:
    payload = payload or {}
    errors = []
    if command not in ALLOWED_COMMANDS:
        errors.append("unknown_operator_command")
    expected = command_signature(command, actor, nonce, payload)
    if signature and not hmac.compare_digest(signature, expected):
        errors.append("bad_operator_signature")
    command_id = hashlib.sha256(f"{actor}:{command}:{nonce}".encode("utf-8")).hexdigest()[:20]
    existing_rows = [row for row in read_jsonl(audit_path) if str(row.get("command_id")) == command_id]
    duplicate = bool(existing_rows)
    if duplicate:
        first = existing_rows[0]
        if first.get("payload") != payload or first.get("signature") != (signature or expected):
            errors.append("idempotency_payload_mismatch")
    row = {
        "schema_version": SCHEMA_VERSION,
        "command_id": command_id,
        "recorded_at": utc_now(),
        "actor": actor,
        "command": command,
        "payload": payload,
        "nonce": nonce,
        "signature": signature or expected,
        "accepted": not errors and not (duplicate and "idempotency_payload_mismatch" in errors),
        "duplicate": duplicate,
        "errors": errors,
        "paper_only": True,
        "can_place_live_orders": False,
    }
    if not duplicate or errors:
        append_jsonl(audit_path, row)
    if not duplicate or errors:
        write_json_atomic(latest_path, row)
    return row
