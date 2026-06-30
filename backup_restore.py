"""Backup and restore helpers for critical state files."""
from __future__ import annotations

import hashlib
import re
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
BACKUP_EXCLUDE_NAMES = {".env", ".env.local", ".env.production", "token.json", "tokens.json", "secrets.json", "credentials.json", "credential.json", "id_rsa", "id_ed25519", "wallet.dat"}
BACKUP_EXCLUDE_PARTS = {".ssh", "secret", "secrets", "credential", "credentials", "keys", "wallet", "wallets"}
BACKUP_EXCLUDE_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
RESTORE_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b", re.I),
    re.compile(r"\bAKIA[0-9A-Z]{12,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{12,}\b", re.I),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9_\-.=+/~]{20,}\b", re.I),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
SECRET_ASSIGNMENT_RE = re.compile(r"""(?ix)
    ["']?\b([A-Z0-9_]*(?:api[_-]?key|api[_-]?secret|secret[_-]?key|secret|token|password|private[_-]?key|access[_-]?token))\b["']?
    \s*[:=]\s*
    ["']?([^"',\s}]{12,})["']?
""")
REDACTED_VALUES = {"redacted", "masked", "none", "null", "changeme", "example", "placeholder"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def backup_excluded(path: Path) -> bool:
    name = path.name.lower()
    parts = {part.lower() for part in path.parts}
    return (
        name in BACKUP_EXCLUDE_NAMES
        or name.startswith(".env")
        or name.endswith(".token")
        or bool(parts & BACKUP_EXCLUDE_PARTS)
        or any(name.endswith(suffix) for suffix in BACKUP_EXCLUDE_SUFFIXES)
    )


def secret_assignment_hits(data: str) -> list[str]:
    hits = []
    for match in SECRET_ASSIGNMENT_RE.finditer(data):
        key = match.group(1).lower()
        value = match.group(2).strip().strip("\"'")
        lowered = value.lower()
        if lowered in REDACTED_VALUES or any(lowered.startswith(f"{marker}_") for marker in REDACTED_VALUES) or lowered.startswith("redacted"):
            continue
        if set(value) <= {"*", "x", "X"}:
            continue
        hits.append(f"{key}=<redacted>")
    return hits


def scan_file_for_secret(path: Path, max_bytes: int = 32_000_000) -> dict[str, Any]:
    if not path.exists():
        return {"ok": False, "errors": ["missing"]}
    try:
        if path.stat().st_size > max_bytes:
            return {"ok": False, "errors": ["secret_scan_file_too_large"], "secret_paths": [], "pattern_hits": [], "fingerprint": None}
    except Exception:
        pass
    try:
        data = path.read_bytes().decode("utf-8", errors="ignore")
    except Exception as exc:
        return {"ok": False, "errors": ["read_failed"], "error": str(exc)[:160]}
    pattern_hits = [pattern.pattern for pattern in RESTORE_SECRET_PATTERNS if pattern.search(data)]
    all_hits = [*pattern_hits, *secret_assignment_hits(data)]
    return {"ok": not all_hits, "secret_paths": [], "pattern_hits": all_hits, "fingerprint": hashlib.sha256(data.encode("utf-8", errors="ignore")).hexdigest()[:12]}


def backup_files(paths: list[Path], reason: str = "manual", *, exclude_secrets: bool = True) -> dict[str, Any]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_MANIFESTS.mkdir(parents=True, exist_ok=True)
    rows = []
    stamp = utc_now().replace(":", "").replace("+", "Z")
    for path in paths:
        if not path.exists():
            rows.append({"source_path": str(path), "ok": False, "error": "missing"})
            continue
        if exclude_secrets and backup_excluded(path):
            rows.append({"source_path": str(path), "ok": False, "skipped": True, "error": "excluded_secret_path"})
            continue
        target = BACKUP_DIR / f"{path.name}.{stamp}.bak"
        shutil.copy2(path, target)
        rows.append({"source_path": str(path), "backup_path": str(target), "ok": True, "sha256": sha256_file(target)})
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        "reason": reason,
        "files": rows,
        "excluded_secret_paths": [row["source_path"] for row in rows if row.get("error") == "excluded_secret_path"],
        "rpo": "local_state_snapshot",
        "rto": "manual_restore_drill",
    }
    write_json_atomic(BACKUP_MANIFESTS / f"backup_{stamp}.json", manifest)
    return manifest


def restore_backup(backup_path: Path, target_path: Path, *, scan_secrets: bool = True) -> dict[str, Any]:
    errors = []
    if not backup_path.exists():
        errors.append("backup_missing")
    scan = scan_file_for_secret(backup_path) if scan_secrets and not errors else {"ok": True, "secret_paths": []}
    if not scan.get("ok"):
        errors.append("restore_secret_scan_failed")
    if not errors:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup_path, target_path)
    return {"schema_version": SCHEMA_VERSION, "restored_at": utc_now(), "ok": not errors, "errors": errors, "backup_path": str(backup_path), "target_path": str(target_path), "secret_scan": scan, "runbook_id": "runbook_restore_drill"}

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
