"""Health timeline and stale/degraded detection for registered agents."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, read_json, write_json_atomic
from timebase import parse_utc, seconds_between, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
HEALTH_LATEST = MEMORY_DIR / "agent_health_latest.json"
HEALTH_HISTORY = MEMORY_DIR / "agent_health_history.jsonl"


def file_age_seconds(path: Path) -> float | None:
    payload = read_json(path, default={})
    ts = payload.get("ts") or payload.get("updated_at") or payload.get("checked_at")
    if not parse_utc(ts):
        return None
    return seconds_between(ts, utc_now())


def evaluate_agent_health(agents: list[dict[str, Any]], output_path: Path = HEALTH_LATEST, history_path: Path = HEALTH_HISTORY) -> dict[str, Any]:
    rows = []
    incidents = []
    for agent in agents:
        name = str(agent.get("name"))
        heartbeat = Path(str(agent.get("heartbeat_file"))) if agent.get("heartbeat_file") else None
        latest = Path(str(agent.get("latest_file"))) if agent.get("latest_file") else None
        max_age = int(agent.get("max_age_seconds") or 900)
        age = file_age_seconds(heartbeat) if heartbeat else None
        state = "ok"
        errors = []
        if heartbeat and not heartbeat.exists():
            state = "missing"
            errors.append("missing_heartbeat")
        elif age is None:
            state = "stale"
            errors.append("invalid_heartbeat_ts")
        elif age > max_age:
            state = "stale"
            errors.append("stale_heartbeat")
        if latest and not latest.exists():
            state = "degraded" if state == "ok" else state
            errors.append("missing_latest")
        row = {"name": name, "state": state, "age_seconds": age, "errors": errors}
        rows.append(row)
        if state != "ok":
            incidents.append({"agent": name, "state": state, "errors": errors})
    payload = {"schema_version": SCHEMA_VERSION, "checked_at": utc_now(), "status": "ok" if not incidents else "degraded", "agents": rows, "incident_count": len(incidents), "incidents": incidents}
    write_json_atomic(output_path, payload)
    append_jsonl(history_path, payload)
    return payload
