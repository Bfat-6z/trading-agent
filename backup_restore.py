"""Backup and restore helpers for critical state files."""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
BACKUP_DIR = STATE_DIR / "backups"
BACKUP_MANIFESTS = STATE_DIR / "backup_manifests"
MIGRATION_MANIFEST = STATE_DIR / "schema_migration_latest.json"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def backup_files(paths: list[Path], reason: str = "manual") -> dict[str, Any]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_MANIFESTS.mkdir(parents=True, exist_ok=True)
    rows = []
    stamp = utc_now().replace(":", "").replace("+", "Z")
    for path in paths:
        if not path.exists():
            rows.append({"source_path": str(path), "ok": False, "error": "missing"})
            continue
        target = BACKUP_DIR / f"{path.name}.{stamp}.bak"
        shutil.copy2(path, target)
        rows.append({"source_path": str(path), "backup_path": str(target), "ok": True, "sha256": sha256_file(target)})
    manifest = {"schema_version": SCHEMA_VERSION, "created_at": utc_now(), "reason": reason, "files": rows}
    write_json_atomic(BACKUP_MANIFESTS / f"backup_{stamp}.json", manifest)
    return manifest


def restore_backup(backup_path: Path, target_path: Path) -> dict[str, Any]:
    errors = []
    if not backup_path.exists():
        errors.append("backup_missing")
    if not errors:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup_path, target_path)
    return {"schema_version": SCHEMA_VERSION, "restored_at": utc_now(), "ok": not errors, "errors": errors, "backup_path": str(backup_path), "target_path": str(target_path)}

def migrate_json_state(paths: list[Path], target_schema_version: int = SCHEMA_VERSION, dry_run: bool = False, output_path: Path = MIGRATION_MANIFEST) -> dict[str, Any]:
    rows = []
    for path in paths:
        if not path.exists():
            rows.append({"path": str(path), "ok": False, "action": "missing"})
            continue
        try:
            import json
            payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception as exc:
            rows.append({"path": str(path), "ok": False, "action": "invalid_json", "error": str(exc)[:160]})
            continue
        if not isinstance(payload, dict):
            rows.append({"path": str(path), "ok": False, "action": "not_object"})
            continue
        previous = payload.get("schema_version")
        if previous == target_schema_version:
            rows.append({"path": str(path), "ok": True, "action": "already_current", "schema_version": previous})
            continue
        if not dry_run:
            backup_files([path], reason="pre_schema_migration")
            payload["schema_version"] = target_schema_version
            payload["migrated_at"] = utc_now()
            write_json_atomic(path, payload)
        rows.append({"path": str(path), "ok": True, "action": "migrated" if not dry_run else "would_migrate", "from_schema_version": previous, "to_schema_version": target_schema_version})
    manifest = {"schema_version": SCHEMA_VERSION, "checked_at": utc_now(), "dry_run": dry_run, "ok": all(row.get("ok") for row in rows), "files": rows}
    write_json_atomic(output_path, manifest)
    return manifest
