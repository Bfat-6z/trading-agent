"""Trust registry for external signal sources."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import read_json, write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
MEMORY_DIR = ROOT / "state" / "agent_memory"
SOURCE_REGISTRY = MEMORY_DIR / "signal_source_registry.json"


def load_registry(path: Path = SOURCE_REGISTRY) -> dict[str, Any]:
    payload = read_json(path, default={})
    if not isinstance(payload, dict) or "sources" not in payload:
        return {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "sources": {}}
    return payload


def source_row(source_id: str, source_type: str = "manual") -> dict[str, Any]:
    return {"source_id": source_id, "source_type": source_type, "trust_score": 0.35, "signals": 0, "hits": 0, "misses": 0, "updated_at": utc_now()}


def get_source(source_id: str, source_type: str = "manual", path: Path = SOURCE_REGISTRY) -> dict[str, Any]:
    registry = load_registry(path)
    row = registry["sources"].get(source_id) or source_row(source_id, source_type)
    registry["sources"][source_id] = row
    write_json_atomic(path, registry)
    return row


def update_source_outcome(source_id: str, hit: bool, path: Path = SOURCE_REGISTRY) -> dict[str, Any]:
    registry = load_registry(path)
    row = registry["sources"].get(source_id) or source_row(source_id)
    row["signals"] = int(row.get("signals") or 0) + 1
    row["hits"] = int(row.get("hits") or 0) + (1 if hit else 0)
    row["misses"] = int(row.get("misses") or 0) + (0 if hit else 1)
    total = max(1, int(row["signals"]))
    row["trust_score"] = round(max(0.05, min(0.95, 0.25 + 0.7 * (row["hits"] / total))), 4)
    row["updated_at"] = utc_now()
    registry["sources"][source_id] = row
    registry["updated_at"] = utc_now()
    write_json_atomic(path, registry)
    return row
