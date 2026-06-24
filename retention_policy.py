"""Retention policy for long-running learning logs."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from archive_manager import archive_file
from atomic_state import write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
RETENTION_LATEST = MEMORY_DIR / "retention_latest.json"


def evaluate_retention(paths: list[Path], max_size_bytes: int = 10_000_000, archive: bool = False, output_path: Path = RETENTION_LATEST) -> dict[str, Any]:
    rows = []
    archives = []
    for path in paths:
        size = path.stat().st_size if path.exists() else 0
        should_archive = path.exists() and size > max_size_bytes
        rows.append({"path": str(path), "exists": path.exists(), "size_bytes": size, "should_archive": should_archive})
        if should_archive and archive:
            archives.append(archive_file(path, reason="size_threshold"))
    payload = {"schema_version": SCHEMA_VERSION, "checked_at": utc_now(), "ok": not any(row["should_archive"] for row in rows), "files": rows, "archives": archives}
    write_json_atomic(output_path, payload)
    return payload
