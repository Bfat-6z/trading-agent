import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import agent_process_supervisor as aps


def test_reflection_supervisor_uses_short_interval_for_dashboard_freshness():
    reflection = next(spec for spec in aps.specs() if spec.name == "reflection_agent")

    assert reflection.args == ("--interval-hours", "0.5")
    assert reflection.max_heartbeat_age_seconds == 2400

def test_news_observer_is_supervised_with_news_freshness_window():
    news = next(spec for spec in aps.specs() if spec.name == "news_observer")

    assert news.script == "news_observer.py"
    assert news.heartbeat_file.name == "news_observer_heartbeat.json"
    assert news.max_heartbeat_age_seconds == 900

def test_self_improvement_agent_is_supervised_for_daily_learning():
    agent = next(spec for spec in aps.specs() if spec.name == "self_improvement_agent")

    assert agent.script == "self_improvement_agent.py"
    assert agent.args == ("--interval-hours", "6")
    assert agent.heartbeat_file.name == "self_improvement_agent_heartbeat.json"
    assert agent.max_heartbeat_age_seconds == 28800

def test_self_model_is_supervised_for_self_awareness_snapshots():
    agent = next(spec for spec in aps.specs() if spec.name == "self_model")

    assert agent.script == "self_model.py"
    assert agent.args == ("--interval-minutes", "10")
    assert agent.heartbeat_file.name == "self_model_heartbeat.json"
    assert agent.max_heartbeat_age_seconds == 900


def test_daily_exam_agent_is_supervised_for_midnight_quality_exam():
    agent = next(spec for spec in aps.specs() if spec.name == "daily_exam_agent")

    assert agent.script == "daily_exam_agent.py"
    assert agent.args == ("--check-seconds", "300")
    assert agent.heartbeat_file.name == "daily_exam_agent_heartbeat.json"
    assert agent.max_heartbeat_age_seconds == 900

def test_llm_reasoning_agent_is_supervised_for_large_model_learning():
    agent = next(spec for spec in aps.specs() if spec.name == "llm_reasoning_agent")

    assert agent.script == "llm_reasoning_agent.py"
    assert agent.args == ("--interval-minutes", "60")
    assert agent.heartbeat_file.name == "llm_reasoning_agent_heartbeat.json"
    assert agent.max_heartbeat_age_seconds == 900

def test_counterfactual_replay_agent_is_supervised_for_objective_learning():
    agent = next(spec for spec in aps.specs() if spec.name == "counterfactual_replay_agent")

    assert agent.script == "counterfactual_replay_agent.py"
    assert agent.args == ("--interval-seconds", "300")
    assert agent.heartbeat_file.name == "counterfactual_replay_agent_heartbeat.json"
    assert agent.max_heartbeat_age_seconds == 900

def test_shadow_trade_evaluator_loop_is_supervised_for_fresh_shadow_learning():
    agent = next(spec for spec in aps.specs() if spec.name == "shadow_trade_evaluator_loop")

    assert agent.script == "shadow_trade_evaluator_loop.py"
    assert agent.args == ("--interval-seconds", "600", "--max-age-hours", "24", "--max-trades", "100")
    assert agent.heartbeat_file.name == "shadow_trade_evaluator_loop_heartbeat.json"
    assert agent.max_heartbeat_age_seconds == 1800

def test_stale_detects_old_heartbeat(tmp_path: Path):
    heartbeat = tmp_path / "hb.json"
    old = (datetime.now(timezone.utc) - timedelta(seconds=500)).isoformat(timespec="seconds")
    heartbeat.write_text(f'{{"ts":"{old}"}}', encoding="utf-8")
    spec = aps.AgentSpec("test", "test.py", tuple(), tmp_path / "test.pid", heartbeat, 60)

    assert aps.stale(spec) is True


def test_ensure_agent_starts_missing_process(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "agent.pid"
    spec = aps.AgentSpec("test", "test_agent.py", tuple(), pid_file, None, None)
    started = []
    monkeypatch.setattr(aps, "is_pid_running", lambda pid, expected_script=None: False)
    monkeypatch.setattr(aps, "start_agent", lambda row: started.append(row.name) or 1234)

    row = aps.ensure_agent(spec)

    assert row["action"] == "started"
    assert row["pid"] == 1234
    assert started == ["test"]


def test_ensure_agent_restarts_running_stale_process(tmp_path: Path, monkeypatch):
    heartbeat = tmp_path / "hb.json"
    old = (datetime.now(timezone.utc) - timedelta(seconds=500)).isoformat(timespec="seconds")
    heartbeat.write_text(f'{{"ts":"{old}"}}', encoding="utf-8")
    pid_file = tmp_path / "agent.pid"
    pid_file.write_text("42", encoding="ascii")
    spec = aps.AgentSpec("test", "test_agent.py", tuple(), pid_file, heartbeat, 60)
    stopped = []
    monkeypatch.setattr(aps, "is_pid_running", lambda pid, expected_script=None: True)
    monkeypatch.setattr(aps, "stop_pid", lambda pid, expected_script: stopped.append((pid, expected_script)))
    monkeypatch.setattr(aps, "start_agent", lambda row: 99)

    row = aps.ensure_agent(spec)

    assert row["action"] == "restarted"
    assert row["pid"] == 99
    assert stopped == [(42, "test_agent.py")]

def test_start_agent_does_not_race_child_owned_pid_file(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "child.pid"
    heartbeat = tmp_path / "child_heartbeat.json"
    pid_file.write_text("123", encoding="ascii")
    spec = aps.AgentSpec("child", "child.py", tuple(), pid_file, heartbeat, 60)

    class FakeProc:
        pid = 456

    monkeypatch.setattr(aps, "ROOT", tmp_path)
    monkeypatch.setattr(aps, "STATE_DIR", tmp_path)
    monkeypatch.setattr(aps, "default_python", lambda: "python")
    monkeypatch.setattr(aps, "append_jsonl", lambda *args, **kwargs: None)
    monkeypatch.setattr(aps.subprocess, "Popen", lambda *args, **kwargs: FakeProc())

    pid = aps.start_agent(spec)

    assert pid == 456
    assert not pid_file.exists()

def test_default_python_prefers_pythonw_for_background_agents_on_windows(tmp_path: Path, monkeypatch):
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    pythonw = scripts / "pythonw.exe"
    pythonw.write_text("", encoding="ascii")
    (scripts / "python.exe").write_text("", encoding="ascii")

    monkeypatch.setattr(aps, "ROOT", tmp_path)
    monkeypatch.setattr(aps.os, "name", "nt", raising=False)

    assert aps.default_python() == str(pythonw)

def test_start_agent_uses_no_window_flag_for_supervised_children(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "agent.pid"
    spec = aps.AgentSpec("agent", "agent.py", tuple(), pid_file, None, None)
    calls = []

    class FakeProc:
        pid = 789

    monkeypatch.setattr(aps, "ROOT", tmp_path)
    monkeypatch.setattr(aps, "STATE_DIR", tmp_path)
    monkeypatch.setattr(aps, "default_python", lambda: "pythonw")
    monkeypatch.setattr(aps.os, "name", "nt", raising=False)
    monkeypatch.setattr(aps.subprocess, "CREATE_NO_WINDOW", 134217728, raising=False)
    monkeypatch.setattr(aps, "append_jsonl", lambda *args, **kwargs: None)
    monkeypatch.setattr(aps.subprocess, "Popen", lambda *args, **kwargs: calls.append(kwargs) or FakeProc())

    aps.start_agent(spec)

    assert calls
    assert calls[0]["creationflags"] == 134217728

def test_start_agent_parent_writes_dashboard_pid(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "dashboard.pid"
    spec = aps.AgentSpec("dashboard", "agent_status_dashboard.py", tuple(), pid_file, None, None)

    class FakeProc:
        pid = 789

    monkeypatch.setattr(aps, "ROOT", tmp_path)
    monkeypatch.setattr(aps, "STATE_DIR", tmp_path)
    monkeypatch.setattr(aps, "default_python", lambda: "python")
    monkeypatch.setattr(aps, "append_jsonl", lambda *args, **kwargs: None)
    monkeypatch.setattr(aps.subprocess, "Popen", lambda *args, **kwargs: FakeProc())

    aps.start_agent(spec)

    assert pid_file.read_text(encoding="ascii") == "789"

def test_dedupe_agent_processes_keeps_preferred_pid_and_stops_extras(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "agent.pid"
    spec = aps.AgentSpec("agent", "agent.py", tuple(), pid_file, tmp_path / "agent_hb.json", 60)
    stopped = []
    events = []

    monkeypatch.setattr(aps, "running_script_pids", lambda script: [111, 222, 333])
    monkeypatch.setattr(aps, "stop_pid", lambda pid, script: stopped.append((pid, script)))
    monkeypatch.setattr(aps, "append_jsonl", lambda event, payload: events.append((event, payload)))

    keep = aps.dedupe_agent_processes(spec, 222)

    assert keep == 222
    assert stopped == [(111, "agent.py"), (333, "agent.py")]
    assert all(event == "agent_duplicate_stop" for event, _ in events)

def test_dedupe_agent_processes_updates_stale_pid_file_to_live_pid(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "agent.pid"
    pid_file.write_text("999", encoding="ascii")
    spec = aps.AgentSpec("agent", "agent.py", tuple(), pid_file, tmp_path / "agent_hb.json", 60)

    monkeypatch.setattr(aps, "running_script_pids", lambda script: [222])
    monkeypatch.setattr(aps, "stop_pid", lambda pid, script: None)
    monkeypatch.setattr(aps, "append_jsonl", lambda *args, **kwargs: None)

    keep = aps.dedupe_agent_processes(spec, 999)

    assert keep == 222
    assert pid_file.read_text(encoding="ascii") == "222"

def test_cleanup_runtime_stops_supervised_processes_and_removes_pid_files(tmp_path: Path, monkeypatch):
    supervisor_pid = tmp_path / "supervisor.pid"
    supervisor_lock = tmp_path / "supervisor.lock"
    dashboard_pid = tmp_path / "dashboard.pid"
    market_pid = tmp_path / "market.pid"
    for path, pid in ((supervisor_pid, 10), (dashboard_pid, 20), (market_pid, 30)):
        path.write_text(str(pid), encoding="ascii")
    supervisor_lock.write_text("10", encoding="ascii")
    fake_specs = [
        aps.AgentSpec("dashboard", "agent_status_dashboard.py", tuple(), dashboard_pid, None, None),
        aps.AgentSpec("market", "market_observer.py", tuple(), market_pid, tmp_path / "market_hb.json", 60),
    ]
    stopped = []

    monkeypatch.setattr(aps, "PID_FILE", supervisor_pid)
    monkeypatch.setattr(aps, "LOCK_FILE", supervisor_lock)
    monkeypatch.setattr(aps, "specs", lambda: fake_specs)
    monkeypatch.setattr(aps, "os", aps.os)
    monkeypatch.setattr(aps.os, "getpid", lambda: 99)
    monkeypatch.setattr(
        aps,
        "running_script_pids",
        lambda script: {
            "agent_process_supervisor.py": [10, 99],
            "agent_status_dashboard.py": [20, 21],
            "market_observer.py": [30],
        }.get(script, []),
    )
    monkeypatch.setattr(aps, "stop_pid", lambda pid, script: stopped.append((pid, script)))
    monkeypatch.setattr(aps, "append_jsonl", lambda *args, **kwargs: None)

    summary = aps.cleanup_runtime(exclude_current_supervisor=True)

    assert (10, "agent_process_supervisor.py") in stopped
    assert (99, "agent_process_supervisor.py") not in stopped
    assert (20, "agent_status_dashboard.py") in stopped
    assert (21, "agent_status_dashboard.py") in stopped
    assert (30, "market_observer.py") in stopped
    assert not supervisor_pid.exists()
    assert not supervisor_lock.exists()
    assert not dashboard_pid.exists()
    assert not market_pid.exists()
    assert "agent_process_supervisor.py" in summary["stopped"]
    assert str(supervisor_lock) in summary["lock_files_removed"]

def test_acquire_supervisor_lock_reclaims_stale_lock(tmp_path: Path, monkeypatch):
    lock_file = tmp_path / "supervisor.lock"
    lock_file.write_text("123", encoding="ascii")
    events = []

    monkeypatch.setattr(aps, "LOCK_FILE", lock_file)
    monkeypatch.setattr(aps.os, "getpid", lambda: 999)
    monkeypatch.setattr(aps, "is_pid_running", lambda pid, expected_script=None: False)
    monkeypatch.setattr(aps, "append_jsonl", lambda event, payload: events.append((event, payload)))

    assert aps.acquire_supervisor_lock() is True
    assert lock_file.read_text(encoding="ascii") == "999"
    assert events[-1] == ("supervisor_lock_acquired", {"pid": 999})

def test_acquire_supervisor_lock_refuses_live_owner(tmp_path: Path, monkeypatch):
    lock_file = tmp_path / "supervisor.lock"
    lock_file.write_text("123", encoding="ascii")
    events = []

    monkeypatch.setattr(aps, "LOCK_FILE", lock_file)
    monkeypatch.setattr(aps.os, "getpid", lambda: 999)
    monkeypatch.setattr(aps, "is_pid_running", lambda pid, expected_script=None: True)
    monkeypatch.setattr(aps, "append_jsonl", lambda event, payload: events.append((event, payload)))

    assert aps.acquire_supervisor_lock() is False
    assert lock_file.read_text(encoding="ascii") == "123"
    assert events[-1] == ("supervisor_lock_busy", {"pid": 999, "owner_pid": 123})

def test_collapse_launcher_processes_prefers_child_interpreter():
    rows = [
        (100, 50, "venv\\Scripts\\python.exe agent_process_supervisor.py"),
        (101, 100, "venv\\Scripts\\python.exe agent_process_supervisor.py"),
        (200, 50, "venv\\Scripts\\python.exe market_observer.py"),
    ]

    collapsed = aps.collapse_launcher_processes(rows)

    assert collapsed == [
        (101, "venv\\Scripts\\python.exe agent_process_supervisor.py"),
        (200, "venv\\Scripts\\python.exe market_observer.py"),
    ]

def test_status_reports_duplicate_process_counts(tmp_path: Path, monkeypatch, capsys):
    supervisor_pid = tmp_path / "supervisor.pid"
    dashboard_pid = tmp_path / "dashboard.pid"
    supervisor_pid.write_text("10", encoding="ascii")
    dashboard_pid.write_text("20", encoding="ascii")
    fake_specs = [aps.AgentSpec("dashboard", "agent_status_dashboard.py", tuple(), dashboard_pid, None, None)]

    monkeypatch.setattr(aps, "PID_FILE", supervisor_pid)
    monkeypatch.setattr(aps, "specs", lambda: fake_specs)
    monkeypatch.setattr(
        aps,
        "running_script_pids",
        lambda script: {
            "agent_status_dashboard.py": [20, 21, 22],
        }.get(script, []),
    )
    monkeypatch.setattr(aps, "supervisor_loop_pids", lambda: [10, 11])
    monkeypatch.setattr(aps, "is_pid_running", lambda pid, expected_script=None: True)
    monkeypatch.setattr(aps, "heartbeat_age_seconds", lambda path: None)
    monkeypatch.setattr(aps, "stale", lambda spec: False)

    assert aps.status() == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["supervisor_duplicate_count"] == 1
    assert payload["agents"][0]["duplicate_count"] == 2
    assert payload["agents"][0]["matching_pids"] == [20, 21, 22]

def test_stop_other_supervisors_logs_duplicates_without_killing(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "supervisor.pid"
    pid_file.write_text("99", encoding="ascii")
    events = []

    monkeypatch.setattr(aps, "PID_FILE", pid_file)
    monkeypatch.setattr(aps.os, "getpid", lambda: 99)
    monkeypatch.setattr(aps, "supervisor_loop_pids", lambda: [10, 11])
    monkeypatch.setattr(aps, "append_jsonl", lambda event, payload: events.append((event, payload)))

    aps.stop_other_supervisors()

    assert events == [("supervisor_duplicate_detected", {"pids": [10, 11], "owner_pid": 99, "action": "cleanup_required"})]
