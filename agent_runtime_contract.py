"""Runtime artifact contract for supervised background agents."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import read_json, write_json_atomic
from event_store import safe_append_event
from timebase import parse_utc, seconds_between, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
REGISTRY_PATH = STATE_DIR / "agent_registry.json"
WRITER_LEASE_DIR = STATE_DIR / "writer_leases"
PRODUCER_BUILD_ID = os.environ.get("TRADING_AGENT_BUILD_ID") or "local-dev"
REQUIRED_SCOPE_FIELDS = ("environment", "account_scope", "credential_fingerprint", "source_ledger_id", "producer_build_id", "writer_epoch")


@dataclass(frozen=True)
class AgentRuntimeSpec:
    name: str
    require_pid: bool = True
    require_heartbeat: bool = True
    require_latest: bool = True
    require_history: bool = True
    max_heartbeat_age_seconds: int = 900
    pid_path: Path | None = None
    heartbeat_path: Path | None = None
    latest_path: Path | None = None
    history_path: Path | None = None
    require_scope_metadata: bool = False
    require_history_link: bool = False
    require_latest_schema: bool = False
    history_missing_is_error: bool = False

    @property
    def pid_file(self) -> Path:
        return self.pid_path or STATE_DIR / f"{self.name}.pid"

    @property
    def heartbeat_file(self) -> Path:
        return self.heartbeat_path or STATE_DIR / f"{self.name}_heartbeat.json"

    @property
    def latest_file(self) -> Path:
        return self.latest_path or MEMORY_DIR / f"{self.name}_latest.json"

    @property
    def history_file(self) -> Path:
        return self.history_path or MEMORY_DIR / f"{self.name}_history.jsonl"

    def to_manifest(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "pid_file": str(self.pid_file),
            "heartbeat_file": str(self.heartbeat_file),
            "latest_file": str(self.latest_file),
            "history_file": str(self.history_file),
            "max_heartbeat_age_seconds": self.max_heartbeat_age_seconds,
            "require_pid": self.require_pid,
            "require_heartbeat": self.require_heartbeat,
            "require_latest": self.require_latest,
            "require_history": self.require_history,
            "require_scope_metadata": self.require_scope_metadata,
            "require_history_link": self.require_history_link,
            "require_latest_schema": self.require_latest_schema,
        }


def _memory(name: str) -> Path:
    return MEMORY_DIR / name


def _state(name: str) -> Path:
    return STATE_DIR / name


REGISTERED_ARTIFACTS: dict[str, dict[str, Any]] = {
    "dashboard": {"require_heartbeat": False, "require_latest": False, "require_history": False, "latest": None, "history": None},
    "market_observer": {"latest": _state("market_updates_latest.json"), "history": _state("market_updates.jsonl"), "stale": 420},
    "news_observer": {"latest": _memory("news_latest.json"), "history": _memory("news_history.jsonl"), "stale": 900, "history_missing_is_error": False},
    "dream_cycle": {"latest": _memory("dream_cycle_latest.json"), "history": _memory("dream_cycle_history.jsonl"), "stale": 2400, "history_missing_is_error": False},
    "reflection_agent": {"latest": _memory("daily_reflection_latest.md"), "history": _memory("daily_reflections.jsonl"), "stale": 2400, "history_missing_is_error": False},
    "cognitive_supervisor": {"latest": _memory("cognitive_state_latest.json"), "history": _memory("cognitive_state_history.jsonl"), "stale": 1500},
    "llm_reasoning_agent": {"latest": _memory("llm_reasoning_latest.json"), "history": _memory("llm_reasoning_history.jsonl"), "stale": 900},
    "paper_candidate_feeder": {"latest": _memory("paper_candidate_feeder_latest.json"), "history": _memory("paper_candidate_feeder_history.jsonl"), "stale": 180},
    "autonomous_paper_trading_loop": {"latest": _memory("autonomous_paper_trading_loop_latest.json"), "history": _memory("autonomous_paper_trading_loop_history.jsonl"), "stale": 180},
    "paper_execution_lifecycle_loop": {"latest": _memory("paper_execution_lifecycle_latest.json"), "history": _memory("paper_execution_lifecycle_history.jsonl"), "stale": 120},
    "microstructure_observer_loop": {"latest": _memory("microstructure_observer_loop_latest.json"), "history": _memory("microstructure_observer_loop_history.jsonl"), "stale": 180},
    "whale_flow_observer": {"latest": _memory("whale_flow_latest.json"), "history": _memory("whale_flow_history.jsonl"), "stale": 600},
    "counterfactual_replay_agent": {"latest": _memory("counterfactual_latest.json"), "history": _memory("counterfactual_replays.jsonl"), "stale": 900, "history_missing_is_error": False},
    "learning_exam_benchmark": {"latest": _memory("learning_exam_benchmark_latest.json"), "history": _memory("learning_exam_benchmark_history.jsonl"), "stale": 4500},
    "test_result_memory_agent": {"latest": _memory("test_result_memory_latest.json"), "history": _memory("test_result_memory_history.jsonl"), "stale": 2700},
    "shadow_trade_evaluator_loop": {"latest": _memory("shadow_trade_evaluator_loop_latest.json"), "history": _memory("shadow_trade_evaluator_loop_history.jsonl"), "stale": 1800},
    "promotion_evaluator_loop": {"latest": _memory("promotion_evaluator_loop_latest.json"), "history": _memory("promotion_evaluator_loop_history.jsonl"), "stale": 600},
    "self_model": {"latest": _memory("self_model_latest.json"), "history": _memory("self_model_history.jsonl"), "stale": 900},
    "memory_consolidation_agent": {"latest": _memory("memory_consolidation_latest.json"), "history": _memory("memory_consolidation_history.jsonl"), "stale": 2700},
    "skill_forge_agent": {"latest": _memory("skill_forge_latest.json"), "history": _memory("skill_forge_history.jsonl"), "stale": 2700},
    "self_improvement_agent": {"latest": _memory("self_improvement_latest.json"), "history": _memory("self_improvement_history.jsonl"), "stale": 28800},
    "daily_exam_agent": {"latest": _memory("daily_exam_latest.json"), "history": _memory("daily_exam_history.jsonl"), "stale": 900},
}


def canonical_path(path: Path) -> str:
    return str(path.resolve()).lower()


def stable_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def heartbeat_age_seconds(path: Path) -> float | None:
    payload = read_json(path, default={})
    ts = payload.get("ts") or payload.get("updated_at") or payload.get("checked_at")
    if not parse_utc(ts):
        return None
    return seconds_between(ts, utc_now())


def spec_from_registry(name: str) -> AgentRuntimeSpec:
    row = REGISTERED_ARTIFACTS.get(name, {})
    latest = row.get("latest")
    history = row.get("history")
    return AgentRuntimeSpec(
        name=name,
        require_pid=bool(row.get("require_pid", True)),
        require_heartbeat=bool(row.get("require_heartbeat", True)),
        require_latest=bool(row.get("require_latest", latest is not None)),
        require_history=bool(row.get("require_history", history is not None)),
        max_heartbeat_age_seconds=int(row.get("stale") or row.get("max_heartbeat_age_seconds") or 900),
        latest_path=latest if isinstance(latest, Path) else None,
        history_path=history if isinstance(history, Path) else None,
        require_scope_metadata=bool(row.get("require_scope_metadata", False)),
        require_history_link=bool(row.get("require_history_link", False)),
        require_latest_schema=bool(row.get("require_latest_schema", False)),
        history_missing_is_error=bool(row.get("history_missing_is_error", False)),
    )


def registered_runtime_specs() -> list[AgentRuntimeSpec]:
    return [spec_from_registry(name) for name in REGISTERED_ARTIFACTS]


def spec_from_supervisor_spec(supervisor_spec: Any) -> AgentRuntimeSpec:
    base = spec_from_registry(str(supervisor_spec.name))
    return AgentRuntimeSpec(
        name=base.name,
        require_pid=base.require_pid,
        require_heartbeat=base.require_heartbeat,
        require_latest=base.require_latest,
        require_history=base.require_history,
        max_heartbeat_age_seconds=int(supervisor_spec.max_heartbeat_age_seconds or base.max_heartbeat_age_seconds),
        pid_path=supervisor_spec.pid_file,
        heartbeat_path=supervisor_spec.heartbeat_file or base.heartbeat_file,
        latest_path=base.latest_file if base.require_latest else None,
        history_path=base.history_file if base.require_history else None,
        require_scope_metadata=base.require_scope_metadata,
        require_history_link=base.require_history_link,
        require_latest_schema=base.require_latest_schema,
        history_missing_is_error=base.history_missing_is_error,
    )


def specs_from_supervisor(supervisor_specs: list[Any]) -> list[AgentRuntimeSpec]:
    return [spec_from_supervisor_spec(spec) for spec in supervisor_specs]


def _read_jsonl(path: Path, limit: int = 1000) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()[-limit:]:
        try:
            payload = json.loads(line)
            if isinstance(payload, dict):
                rows.append(payload)
        except Exception:
            continue
    return rows


def _history_contains_seq(path: Path, seq: Any) -> bool:
    for row in _read_jsonl(path):
        if row.get("history_seq") == seq or row.get("seq") == seq or row.get("event_seq") == seq:
            return True
    return False


def _validate_latest_payload(spec: AgentRuntimeSpec, payload: dict[str, Any]) -> tuple[list[str], list[str], dict[str, Any]]:
    errors: list[str] = []
    warnings: list[str] = []
    details: dict[str, Any] = {}
    if spec.require_latest_schema and "schema_version" not in payload:
        warnings.append("missing_latest_schema_version")
    if payload.get("last_error"):
        errors.append("latest_last_error_present")
    if spec.require_scope_metadata:
        missing = [field for field in REQUIRED_SCOPE_FIELDS if field not in payload]
        if missing:
            errors.extend(f"missing_scope_field:{field}" for field in missing)
    if spec.require_history_link:
        latest_seq = payload.get("last_history_seq", payload.get("event_seq_max"))
        details["latest_history_seq"] = latest_seq
        if latest_seq is None:
            errors.append("missing_latest_history_seq")
        elif not spec.history_file.exists() or not _history_contains_seq(spec.history_file, latest_seq):
            errors.append("latest_history_seq_missing_from_history")
    return errors, warnings, details


def validate_agent_runtime(spec: AgentRuntimeSpec) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    details: dict[str, Any] = {}
    artifacts = {
        "pid": str(spec.pid_file),
        "heartbeat": str(spec.heartbeat_file),
        "latest": str(spec.latest_file) if spec.require_latest else None,
        "history": str(spec.history_file) if spec.require_history else None,
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
            details["heartbeat_age_seconds"] = age
            if age is None:
                errors.append("invalid_heartbeat_ts")
            elif age > spec.max_heartbeat_age_seconds:
                errors.append("stale_heartbeat")
    if spec.require_latest:
        if not spec.latest_file.exists():
            errors.append("missing_latest_json")
        elif spec.latest_file.suffix.lower() == ".json":
            payload = read_json(spec.latest_file, default={})
            latest_errors, latest_warnings, latest_details = _validate_latest_payload(spec, payload)
            errors.extend(latest_errors)
            warnings.extend(latest_warnings)
            details.update(latest_details)
    if spec.require_history and not spec.history_file.exists():
        (errors if spec.history_missing_is_error else warnings).append("missing_history_jsonl")
    return {
        "schema_version": SCHEMA_VERSION,
        "checked_at": utc_now(),
        "agent": spec.name,
        "ok": not errors,
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
        "artifacts": artifacts,
        "details": details,
        "can_place_live_orders": False,
        "live_permission": False,
    }


def duplicate_output_path_errors(specs: list[AgentRuntimeSpec]) -> list[dict[str, Any]]:
    seen: dict[str, list[str]] = {}
    for spec in specs:
        if not spec.require_latest:
            continue
        seen.setdefault(canonical_path(spec.latest_file), []).append(spec.name)
    return [
        {"output_path": path, "agents": sorted(names), "error": "duplicate_latest_writer"}
        for path, names in seen.items()
        if len(set(names)) > 1
    ]


def validate_agents(specs: list[AgentRuntimeSpec], output_path: Path = REGISTRY_PATH) -> dict[str, Any]:
    rows = [validate_agent_runtime(spec) for spec in specs]
    duplicate_errors = duplicate_output_path_errors(specs)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "checked_at": utc_now(),
        "agent_count": len(rows),
        "pass_count": sum(1 for row in rows if row["ok"]),
        "fail_count": sum(1 for row in rows if not row["ok"]) + len(duplicate_errors),
        "duplicate_output_errors": duplicate_errors,
        "agents": rows,
        "manifest": [spec.to_manifest() for spec in specs],
        "can_place_live_orders": False,
        "live_permission": False,
    }
    payload["ok"] = payload["fail_count"] == 0
    write_json_atomic(output_path, payload)
    safe_append_event("agent_runtime_contract", "runtime_contract_validation", {"ok": payload["ok"], "fail_count": payload["fail_count"], "agent_count": len(rows)})
    return payload


def write_registered_manifest(output_path: Path = REGISTRY_PATH) -> dict[str, Any]:
    return validate_agents(registered_runtime_specs(), output_path=output_path)


def spec_from_name(name: str, max_heartbeat_age_seconds: int = 900) -> AgentRuntimeSpec:
    return AgentRuntimeSpec(name=name, max_heartbeat_age_seconds=max_heartbeat_age_seconds)


def lease_path_for(output_path: Path, lease_dir: Path = WRITER_LEASE_DIR) -> Path:
    digest = hashlib.sha256(canonical_path(output_path).encode("utf-8")).hexdigest()
    return lease_dir / f"{digest}.lease.json"


def acquire_writer_lease(
    output_path: Path,
    agent: str,
    pid: int,
    build_id: str = PRODUCER_BUILD_ID,
    lease_dir: Path = WRITER_LEASE_DIR,
    process_alive: Callable[[int], bool] | None = None,
    supervisor_kill_proof: bool = False,
    writer_epoch: str | None = None,
) -> dict[str, Any]:
    lease_file = lease_path_for(output_path, lease_dir)
    existing = read_json(lease_file, default={})
    process_alive = process_alive or (lambda _pid: False)
    output_key = canonical_path(output_path)
    if existing:
        same_owner = existing.get("agent") == agent and int(existing.get("pid") or -1) == int(pid) and existing.get("build_id") == build_id
        existing_pid = int(existing.get("pid") or -1)
        if not same_owner and process_alive(existing_pid):
            return {"acquired": False, "error": "writer_lease_conflict_active", "lease": existing, "can_place_live_orders": False, "live_permission": False}
        if not same_owner and not supervisor_kill_proof:
            return {"acquired": False, "error": "stale_lease_requires_supervisor_kill_proof", "lease": existing, "can_place_live_orders": False, "live_permission": False}
    epoch = writer_epoch or f"{agent}:{pid}:{build_id}:{utc_now()}"
    lease = {
        "schema_version": SCHEMA_VERSION,
        "acquired_at": utc_now(),
        "agent": agent,
        "pid": int(pid),
        "build_id": build_id,
        "writer_epoch": epoch,
        "output_path": str(output_path.resolve()),
        "output_path_key": output_key,
        "lease_checksum": stable_hash({"agent": agent, "pid": pid, "build_id": build_id, "output_path": output_key, "writer_epoch": epoch}),
        "can_place_live_orders": False,
        "live_permission": False,
    }
    write_json_atomic(lease_file, lease)
    return {"acquired": True, "lease": lease, "lease_file": str(lease_file), "can_place_live_orders": False, "live_permission": False}


def release_writer_lease(output_path: Path, agent: str, pid: int, lease_dir: Path = WRITER_LEASE_DIR) -> dict[str, Any]:
    lease_file = lease_path_for(output_path, lease_dir)
    existing = read_json(lease_file, default={})
    if not existing:
        return {"released": False, "reason": "lease_missing", "can_place_live_orders": False, "live_permission": False}
    if existing.get("agent") != agent or int(existing.get("pid") or -1) != int(pid):
        return {"released": False, "reason": "lease_owner_mismatch", "lease": existing, "can_place_live_orders": False, "live_permission": False}
    try:
        lease_file.unlink()
    except FileNotFoundError:
        pass
    return {"released": True, "can_place_live_orders": False, "live_permission": False}
