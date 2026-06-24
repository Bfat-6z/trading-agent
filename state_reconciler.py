"""State reconciliation for paper/local vs external snapshots."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
RECONCILE_LATEST = ROOT / "state" / "agent_memory" / "state_reconciliation_latest.json"


def key_position(pos: dict[str, Any]) -> tuple[str, str]:
    return (str(pos.get("symbol") or "").upper(), str(pos.get("side") or "").upper())


def reconcile_positions(local_positions: list[dict[str, Any]], external_positions: list[dict[str, Any]], output_path: Path = RECONCILE_LATEST) -> dict[str, Any]:
    local = {key_position(pos): pos for pos in local_positions}
    external = {key_position(pos): pos for pos in external_positions}
    missing_external = [local[key] for key in local.keys() - external.keys()]
    unexpected_external = [external[key] for key in external.keys() - local.keys()]
    errors = []
    if missing_external:
        errors.append("local_open_absent_external")
    if unexpected_external:
        errors.append("unexpected_external_position")
    payload = {"schema_version": SCHEMA_VERSION, "checked_at": utc_now(), "ok": not errors, "errors": errors, "missing_external": missing_external, "unexpected_external": unexpected_external}
    write_json_atomic(output_path, payload)
    return payload
