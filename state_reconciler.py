"""State reconciliation for paper/local vs external snapshots."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
RECONCILE_LATEST = ROOT / "state" / "agent_memory" / "state_reconciliation_latest.json"
ALLOWED_SOURCE_MODES = {"paper_ledger", "shadow_readonly"}
LIVE_SCOPES = {"live", "real", "production", "mainnet"}


def key_position(pos: dict[str, Any]) -> tuple[str, str]:
    return (str(pos.get("symbol") or "").upper(), str(pos.get("side") or "").upper())


def validate_source_context(source_context: dict[str, Any] | None) -> tuple[list[str], dict[str, Any]]:
    source_context = dict(source_context or {})
    errors: list[str] = []
    source_mode = str(source_context.get("source_mode") or "")
    account_scope = str(source_context.get("account_scope") or "").lower()
    environment = str(source_context.get("environment") or "").lower()
    required = ("source_mode", "environment", "account_scope", "credential_fingerprint", "source_ledger_id")
    missing = [key for key in required if not source_context.get(key)]
    if missing:
        errors.extend(f"missing_{key}" for key in missing)
    if source_mode and source_mode not in ALLOWED_SOURCE_MODES:
        errors.append("invalid_source_mode")
    if account_scope in LIVE_SCOPES or environment in LIVE_SCOPES:
        errors.append("live_account_snapshot_forbidden")
    safe_context = {
        "source_mode": source_context.get("source_mode") or "missing",
        "environment": source_context.get("environment") or "missing",
        "account_scope": source_context.get("account_scope") or "missing",
        "credential_fingerprint": source_context.get("credential_fingerprint") or "missing",
        "source_ledger_id": source_context.get("source_ledger_id") or "missing",
    }
    return errors, safe_context

def reconcile_positions(local_positions: list[dict[str, Any]], external_positions: list[dict[str, Any]], output_path: Path = RECONCILE_LATEST, source_context: dict[str, Any] | None = None) -> dict[str, Any]:
    local = {key_position(pos): pos for pos in local_positions}
    external = {key_position(pos): pos for pos in external_positions}
    missing_external = [local[key] for key in local.keys() - external.keys()]
    unexpected_external = [external[key] for key in external.keys() - local.keys()]
    context_errors, safe_context = validate_source_context(source_context)
    errors = list(context_errors)
    if missing_external:
        errors.append("local_open_absent_external")
    if unexpected_external:
        errors.append("unexpected_external_position")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "checked_at": utc_now(),
        "ok": not errors,
        "errors": sorted(set(errors)),
        "source_context": safe_context,
        "paper_readiness_allowed": not errors,
        "can_place_live_orders": False,
        "missing_external": missing_external,
        "unexpected_external": unexpected_external,
    }
    write_json_atomic(output_path, payload)
    return payload
