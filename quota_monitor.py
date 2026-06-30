"""Quota and rate-limit monitor for data/model sources."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import read_json, write_json_atomic
from data_source_registry import mark_source_event
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
QUOTA_LATEST = ROOT / "state" / "quota_latest.json"


def evaluate_quota(source_id: str, used: int, limit: int | None, cooldown_until: str | None = None, output_path: Path = QUOTA_LATEST, event_db_path: Path | None = None) -> dict[str, Any]:
    errors = []
    warnings = []
    usage_ratio = used / limit if limit else 0.0
    if limit and used >= limit:
        errors.append("quota_exhausted")
    elif limit and usage_ratio >= 0.8:
        warnings.append("quota_near_limit")
    if cooldown_until:
        warnings.append("source_cooldown_active")
    latest = read_json(output_path, default={"sources": {}})
    latest.setdefault("sources", {})[source_id] = {"source_id": source_id, "used": used, "limit": limit, "usage_ratio": round(usage_ratio, 4), "cooldown_until": cooldown_until, "status": "blocked" if errors else "warn" if warnings else "ok", "errors": errors, "warnings": warnings, "checked_at": utc_now()}
    latest.update({"schema_version": SCHEMA_VERSION, "updated_at": utc_now()})
    write_json_atomic(output_path, latest)
    if "quota_exhausted" in errors:
        mark_source_event(source_id, "quota_exhausted", path=output_path.with_name("data_sources_latest.json"), event_db_path=event_db_path)
    return latest["sources"][source_id]
