"""Recovery drill utilities for restoring latest files from archive copies."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import read_json, write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
DRILL_LATEST = STATE_DIR / "recovery_drill_latest.json"


def restore_from_manifest(manifest_path: Path, output_path: Path | None = None) -> dict[str, Any]:
    manifest = read_json(manifest_path, default={})
    archive_path = Path(str(manifest.get("archive_path") or ""))
    target = output_path or Path(str(manifest.get("source_path") or ""))
    errors = []
    if not archive_path.exists():
        errors.append("archive_missing")
    if not target:
        errors.append("target_missing")
    if not errors:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(archive_path, target)
    result = {"schema_version": SCHEMA_VERSION, "ran_at": utc_now(), "ok": not errors, "errors": errors, "manifest_path": str(manifest_path), "restored_to": str(target)}
    write_json_atomic(DRILL_LATEST, result)
    return result


def run_noop_drill(output_path: Path = DRILL_LATEST) -> dict[str, Any]:
    result = {"schema_version": SCHEMA_VERSION, "ran_at": utc_now(), "ok": True, "mode": "noop", "checks": ["dashboard_readable", "archive_manifest_path_required_for_restore"]}
    write_json_atomic(output_path, result)
    return result

RESTORE_SERVE_GATES = (
    "event_replay_ok",
    "erasure_replay_ok",
    "account_reconciled",
    "secret_scan_ok",
    "owner_approved",
    "identity_verified",
)

def restore_serve_gate_status(gates: dict[str, Any], output_path: Path = DRILL_LATEST) -> dict[str, Any]:
    missing = [key for key in RESTORE_SERVE_GATES if not bool(gates.get(key))]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "checked_at": utc_now(),
        "mode": "restore_serve_gate",
        "can_serve_dashboard": not missing,
        "can_start_agents": not missing,
        "missing_gates": missing,
        "required_gates": list(RESTORE_SERVE_GATES),
        "paper_only": True,
        "can_place_live_orders": False,
    }
    write_json_atomic(output_path, payload)
    return payload

def cold_start_from_crash_drill(canonical_paths: list[Path], latest_paths: list[Path], output_path: Path = DRILL_LATEST) -> dict[str, Any]:
    canonical_missing = [str(path) for path in canonical_paths if not path.exists()]
    quarantined_latest = []
    for path in latest_paths:
        if path.exists():
            quarantine = path.with_suffix(path.suffix + ".stale")
            path.replace(quarantine)
            quarantined_latest.append(str(quarantine))
    errors = []
    if canonical_missing:
        errors.append("canonical_source_missing")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "ran_at": utc_now(),
        "mode": "cold_start_from_crash",
        "ok": not errors,
        "errors": errors,
        "canonical_missing": canonical_missing,
        "quarantined_latest": quarantined_latest,
        "stale_latest_trusted": False,
    }
    write_json_atomic(output_path, payload)
    return payload

def split_brain_drill(owner_pid: int | None, observed_pids: list[int], registry: dict[str, Any] | None = None, output_path: Path = DRILL_LATEST) -> dict[str, Any]:
    registry = registry or {}
    duplicates = [pid for pid in observed_pids if owner_pid is None or int(pid) != int(owner_pid)]
    stale_registry = bool(registry) and (
        (owner_pid is not None and int(registry.get("pid") or -1) != int(owner_pid))
        or registry.get("owner") not in (None, "agent_status_dashboard", "agent_process_supervisor")
    )
    errors = []
    if duplicates:
        errors.append("duplicate_process_detected")
    if stale_registry:
        errors.append("stale_or_wrong_registry_identity")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "ran_at": utc_now(),
        "mode": "split_brain",
        "ok": not errors,
        "errors": errors,
        "owner_pid": owner_pid,
        "observed_pids": observed_pids,
        "duplicates_quarantined": duplicates,
        "stale_registry": stale_registry,
        "writer_allowed": not errors,
    }
    write_json_atomic(output_path, payload)
    return payload
