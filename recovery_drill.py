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
