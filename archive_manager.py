"""Archive manager for learning files with manifest preservation."""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
ARCHIVE_DIR = STATE_DIR / "archive"
ARCHIVE_MANIFESTS = STATE_DIR / "archive_manifests"


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def archive_file(path: Path, reason: str = "retention") -> dict[str, Any]:
    if not path.exists():
        return {"ok": False, "error": "missing_file", "path": str(path)}
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_MANIFESTS.mkdir(parents=True, exist_ok=True)
    digest = file_hash(path)[:16]
    target = ARCHIVE_DIR / f"{path.name}.{digest}.archive"
    shutil.copy2(path, target)
    manifest = {"schema_version": SCHEMA_VERSION, "archived_at": utc_now(), "source_path": str(path), "archive_path": str(target), "sha256": file_hash(target), "reason": reason, "size_bytes": target.stat().st_size}
    write_json_atomic(ARCHIVE_MANIFESTS / f"{target.name}.json", manifest)
    append_jsonl(ARCHIVE_MANIFESTS / "archive_history.jsonl", manifest)
    return manifest
