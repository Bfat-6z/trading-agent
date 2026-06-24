"""Runtime artifact contract for supervised background agents."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import read_json, write_json_atomic
from timebase import parse_utc, seconds_between, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
REGISTRY_PATH = STATE_DIR / "agent_registry.json"


@dataclass(frozen=True)
class AgentRuntimeSpec:
    name: str
    require_pid: bool = True
    require_heartbeat: bool = True
    require_latest: bool = True
    require_history: bool = True
    max_heartbeat_age_seconds: int = 900

    @property
    def pid_file(self) -> Path:
        return STATE_DIR / f"{self.name}.pid"

    @property
    def heartbeat_file(self) -> Path:
        return STATE_DIR / f"{self.name}_heartbeat.json"

    @property
    def latest_file(self) -> Path:
        return MEMORY_DIR / f"{self.name}_latest.json"

    @property
    def history_file(self) -> Path:
        return MEMORY_DIR / f"{self.name}_history.jsonl"


def heartbeat_age_seconds(path: Path) -> float | None:
    payload = read_json(path, default={})
    ts = payload.get("ts") or payload.get("updated_at") or payload.get("checked_at")
    if not parse_utc(ts):
        return None
    return seconds_between(ts, utc_now())


def validate_agent_runtime(spec: AgentRuntimeSpec) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    artifacts = {
        "pid": str(spec.pid_file),
        "heartbeat": str(spec.heartbeat_file),
        "latest": str(spec.latest_file),
        "history": str(spec.history_file),
    }
    if spec.require_pid:
        if not spec.pid_file.exists():
            errors.append("missing_pid_file")
        else:
            try:
                int(spec.pid_file.read_text(encoding="ascii", errors="ignore").strip())
            except Exception:
                errors.append("invalid_pid_file")
    if spec.require_heartbeat:
        if not spec.heartbeat_file.exists():
            errors.append("missing_heartbeat")
        else:
            age = heartbeat_age_seconds(spec.heartbeat_file)
            if age is None:
                errors.append("invalid_heartbeat_ts")
            elif age > spec.max_heartbeat_age_seconds:
                errors.append("stale_heartbeat")
    if spec.require_latest and not spec.latest_file.exists():
        errors.append("missing_latest_json")
    if spec.require_history and not spec.history_file.exists():
        warnings.append("missing_history_jsonl")
    return {
        "schema_version": SCHEMA_VERSION,
        "checked_at": utc_now(),
        "agent": spec.name,
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "artifacts": artifacts,
    }


def validate_agents(specs: list[AgentRuntimeSpec], output_path: Path = REGISTRY_PATH) -> dict[str, Any]:
    rows = [validate_agent_runtime(spec) for spec in specs]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "checked_at": utc_now(),
        "agent_count": len(rows),
        "pass_count": sum(1 for row in rows if row["ok"]),
        "fail_count": sum(1 for row in rows if not row["ok"]),
        "agents": rows,
    }
    write_json_atomic(output_path, payload)
    return payload


def spec_from_name(name: str, max_heartbeat_age_seconds: int = 900) -> AgentRuntimeSpec:
    return AgentRuntimeSpec(name=name, max_heartbeat_age_seconds=max_heartbeat_age_seconds)
