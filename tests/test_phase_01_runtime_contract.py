import json
from pathlib import Path

import agent_runtime_contract as arc
import agent_process_supervisor as supervisor


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_registered_nonstandard_latest_path_passes(tmp_path: Path):
    pid = tmp_path / "paper_execution_lifecycle_loop.pid"
    heartbeat = tmp_path / "paper_execution_lifecycle_loop_heartbeat.json"
    latest = tmp_path / "agent_memory" / "paper_execution_lifecycle_latest.json"
    history = tmp_path / "agent_memory" / "paper_execution_lifecycle_history.jsonl"
    pid.write_text("123", encoding="ascii")
    write_json(heartbeat, {"ts": arc.utc_now(), "pid": 123, "status": "ok"})
    write_json(latest, {"schema_version": 1, "ts": arc.utc_now()})
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text('{"history_seq":1}\n', encoding="utf-8")

    spec = arc.AgentRuntimeSpec(
        "paper_execution_lifecycle_loop",
        pid_path=pid,
        heartbeat_path=heartbeat,
        latest_path=latest,
        history_path=history,
    )
    result = arc.validate_agent_runtime(spec)

    assert result["ok"] is True
    assert result["artifacts"]["latest"].endswith("paper_execution_lifecycle_latest.json")


def test_missing_history_can_be_hard_failure(tmp_path: Path):
    pid = tmp_path / "agent.pid"
    heartbeat = tmp_path / "agent_heartbeat.json"
    latest = tmp_path / "latest.json"
    pid.write_text("123", encoding="ascii")
    write_json(heartbeat, {"ts": arc.utc_now(), "pid": 123, "status": "ok"})
    write_json(latest, {"schema_version": 1, "ts": arc.utc_now()})

    result = arc.validate_agent_runtime(
        arc.AgentRuntimeSpec("x", pid_path=pid, heartbeat_path=heartbeat, latest_path=latest, history_path=tmp_path / "missing.jsonl", history_missing_is_error=True)
    )

    assert result["ok"] is False
    assert "missing_history_jsonl" in result["errors"]


def test_stale_heartbeat_marks_runtime_unhealthy(tmp_path: Path):
    pid = tmp_path / "agent.pid"
    heartbeat = tmp_path / "agent_heartbeat.json"
    latest = tmp_path / "latest.json"
    history = tmp_path / "history.jsonl"
    pid.write_text("123", encoding="ascii")
    write_json(heartbeat, {"ts": "2026-01-01T00:00:00+00:00", "pid": 123, "status": "ok"})
    write_json(latest, {"schema_version": 1, "ts": arc.utc_now()})
    history.write_text("{}\n", encoding="utf-8")

    result = arc.validate_agent_runtime(
        arc.AgentRuntimeSpec("x", pid_path=pid, heartbeat_path=heartbeat, latest_path=latest, history_path=history, max_heartbeat_age_seconds=1)
    )

    assert result["ok"] is False
    assert "stale_heartbeat" in result["errors"]


def test_duplicate_latest_writer_is_flagged(tmp_path: Path):
    latest = tmp_path / "shared_latest.json"
    specs = [
        arc.AgentRuntimeSpec("a", require_pid=False, require_heartbeat=False, latest_path=latest, require_history=False),
        arc.AgentRuntimeSpec("b", require_pid=False, require_heartbeat=False, latest_path=latest, require_history=False),
    ]

    result = arc.validate_agents(specs, output_path=tmp_path / "registry.json")

    assert result["ok"] is False
    assert result["duplicate_output_errors"][0]["error"] == "duplicate_latest_writer"


def test_latest_history_seq_must_exist_in_history(tmp_path: Path):
    pid = tmp_path / "agent.pid"
    heartbeat = tmp_path / "agent_heartbeat.json"
    latest = tmp_path / "latest.json"
    history = tmp_path / "history.jsonl"
    pid.write_text("123", encoding="ascii")
    write_json(heartbeat, {"ts": arc.utc_now(), "pid": 123, "status": "ok"})
    write_json(latest, {"schema_version": 1, "ts": arc.utc_now(), "last_history_seq": 7})
    history.write_text('{"history_seq":6}\n', encoding="utf-8")

    result = arc.validate_agent_runtime(
        arc.AgentRuntimeSpec("x", pid_path=pid, heartbeat_path=heartbeat, latest_path=latest, history_path=history, require_history_link=True)
    )

    assert result["ok"] is False
    assert "latest_history_seq_missing_from_history" in result["errors"]


def test_latest_scope_metadata_required_when_enabled(tmp_path: Path):
    pid = tmp_path / "agent.pid"
    heartbeat = tmp_path / "agent_heartbeat.json"
    latest = tmp_path / "latest.json"
    history = tmp_path / "history.jsonl"
    pid.write_text("123", encoding="ascii")
    write_json(heartbeat, {"ts": arc.utc_now(), "pid": 123, "status": "ok"})
    write_json(latest, {"schema_version": 1, "ts": arc.utc_now(), "environment": "paper"})
    history.write_text("{}\n", encoding="utf-8")

    result = arc.validate_agent_runtime(
        arc.AgentRuntimeSpec("x", pid_path=pid, heartbeat_path=heartbeat, latest_path=latest, history_path=history, require_scope_metadata=True)
    )

    assert result["ok"] is False
    assert "missing_scope_field:account_scope" in result["errors"]
    assert "missing_scope_field:writer_epoch" in result["errors"]


def test_writer_lease_blocks_active_conflict_and_recovers_with_kill_proof(tmp_path: Path):
    output = tmp_path / "latest.json"
    lease_dir = tmp_path / "leases"
    first = arc.acquire_writer_lease(output, "a", 111, "build1", lease_dir=lease_dir)
    conflict = arc.acquire_writer_lease(output, "b", 222, "build1", lease_dir=lease_dir, process_alive=lambda pid: pid == 111)
    stale_no_proof = arc.acquire_writer_lease(output, "b", 222, "build1", lease_dir=lease_dir, process_alive=lambda pid: False)
    recovered = arc.acquire_writer_lease(output, "b", 222, "build1", lease_dir=lease_dir, process_alive=lambda pid: False, supervisor_kill_proof=True)

    assert first["acquired"] is True
    assert conflict["acquired"] is False
    assert conflict["error"] == "writer_lease_conflict_active"
    assert stale_no_proof["error"] == "stale_lease_requires_supervisor_kill_proof"
    assert recovered["acquired"] is True
    assert recovered["lease"]["agent"] == "b"


def test_supervisor_registry_uses_known_nonstandard_artifact_paths():
    specs = arc.specs_from_supervisor(supervisor.specs())
    by_name = {spec.name: spec for spec in specs}

    assert by_name["paper_execution_lifecycle_loop"].latest_file.name == "paper_execution_lifecycle_latest.json"
    assert by_name["whale_flow_observer"].latest_file.name == "whale_flow_latest.json"
    assert by_name["dashboard"].require_latest is False
