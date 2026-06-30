import json
import socket
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import agent_process_supervisor as aps
import agent_status_dashboard as dash
import alert_manager as alerts
import autonomous_paper_trading_brain as brain
import backup_restore as br
import counterfactual_replay_agent as cf
import host_runtime_monitor as hrm
import operator_control as opc
import paper_account_reconciler as par
import recovery_drill as rd
import state_reconciler as sr


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_dashboard_healthz_returns_failure_when_status_builder_raises():
    def broken_status():
        raise RuntimeError("secret sk-testsecretvalue123456789 should not leak")

    payload = dash.build_healthz_payload(broken_status)

    assert payload["ok"] is False
    assert payload["status"] == "critical"
    assert payload["build_id"] == dash.DASHBOARD_BUILD_ID
    assert payload["error"] == "status_builder_failed"
    assert "secret" not in json.dumps(payload).lower()


def test_dashboard_healthz_http_route_uses_service_unavailable_on_failure(monkeypatch):
    monkeypatch.setattr(dash, "build_healthz_payload", lambda: {"ok": False, "status": "critical", "build_id": dash.DASHBOARD_BUILD_ID})
    server = dash.ReusableThreadingHTTPServer(("127.0.0.1", 0), dash.DashboardHandler)
    server.dashboard_bind_host = "127.0.0.1"
    server.dashboard_token = ""
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/healthz", timeout=5)
            assert False, "expected 503"
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
            payload = json.loads(exc.read().decode("utf-8"))
            assert payload["status"] == "critical"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_dashboard_port_registry_identity_and_bind_fallback(tmp_path: Path):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocked_port = blocker.getsockname()[1]
    try:
        server = dash.bind_dashboard_server("127.0.0.1", blocked_port, attempts=2)
        try:
            assert server.server_address[1] == blocked_port + 1
        finally:
            server.server_close()
    finally:
        blocker.close()

    registry = dash.write_dashboard_port_registry("127.0.0.1", 8123, token_required=True, path=tmp_path / "port.json")
    secret = str(dash.DASHBOARD_RUNTIME["probe_secret"])
    headers = {
        "X-Dashboard-Build-Id": dash.DASHBOARD_BUILD_ID,
        "X-Dashboard-Server-Identity": registry["server_identity"],
        "X-Dashboard-Identity-Signature": dash.dashboard_identity_signature(registry, secret),
    }
    ok = dash.verify_dashboard_probe_identity(registry, headers, {"identity": registry})
    missing_header = dash.verify_dashboard_probe_identity(registry, {"X-Dashboard-Build-Id": dash.DASHBOARD_BUILD_ID}, {"identity": registry})
    wrong_secret = dash.verify_dashboard_probe_identity(registry, headers, {"identity": registry}, probe_secret="wrong-secret")
    wrong = dash.verify_dashboard_probe_identity(registry, headers, {"identity": {**registry, "port": 9999, "pid": 99, "token_scope": "local_only", "server_identity": "wrong"}})
    tampered_owner = dash.verify_dashboard_probe_identity(registry, headers, {"identity": {**registry, "owner": "evil_dashboard"}})

    assert ok["ok"] is True
    assert missing_header["ok"] is False
    assert "missing_server_identity_header" in missing_header["errors"]
    assert wrong_secret["ok"] is False
    assert "wrong_probe_secret" in wrong_secret["errors"]
    assert wrong["ok"] is False
    assert tampered_owner["ok"] is False
    assert "wrong_owner" in tampered_owner["errors"]
    assert "wrong_port" in wrong["errors"]
    assert "wrong_pid" in wrong["errors"]
    assert "wrong_token_scope" in wrong["errors"]
    assert "wrong_server_identity" in wrong["errors"]
    assert registry["token_scope"] == "required"

def test_host_runtime_monitor_is_supervised():
    spec = next(row for row in aps.specs() if row.name == "host_runtime_monitor")

    assert spec.script == "host_runtime_monitor.py"
    assert spec.args == ("--interval-seconds", "300")
    assert spec.heartbeat_file.name == "host_runtime_monitor_heartbeat.json"
    assert spec.max_heartbeat_age_seconds == 900


def test_restart_storm_trips_circuit_breaker_and_opens_incident(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "agent.pid"
    spec = aps.AgentSpec("stormy", "stormy.py", tuple(), pid_file, None, None)
    now = datetime.now(timezone.utc)
    attempts = [(now - timedelta(seconds=idx * 10)).isoformat(timespec="seconds") for idx in range(aps.RESTART_MAX_PER_WINDOW)]
    write_json(tmp_path / "agent_restart_state.json", {"agents": {"stormy": {"attempts": attempts}}})
    started = []
    incidents = []
    monkeypatch.setattr(aps, "is_pid_running", lambda pid, expected_script=None: False)
    monkeypatch.setattr(aps, "start_agent", lambda row: started.append(row.name) or 123)
    monkeypatch.setattr(aps, "open_incident", lambda *args, **kwargs: incidents.append((args, kwargs)))
    monkeypatch.setattr(aps, "append_jsonl", lambda *args, **kwargs: None)

    row = aps.ensure_agent(spec)

    assert row["action"] == "quarantined"
    assert row["restart_gate"]["reason"] == "restart_circuit_breaker"
    assert started == []
    assert incidents

def test_restart_backoff_defers_and_quarantine_is_sticky(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "agent.pid"
    spec = aps.AgentSpec("stormy", "stormy.py", tuple(), pid_file, None, None)
    now = datetime.now(timezone.utc)
    state_path = tmp_path / "agent_restart_state.json"
    write_json(state_path, {"agents": {"stormy": {"attempts": [now.isoformat(timespec="seconds")]}}})
    started = []
    incidents = []
    monkeypatch.setattr(aps, "is_pid_running", lambda pid, expected_script=None: False)
    monkeypatch.setattr(aps, "start_agent", lambda row: started.append(row.name) or 123)
    monkeypatch.setattr(aps, "open_restart_incident", lambda *args, **kwargs: incidents.append((args, kwargs)))
    monkeypatch.setattr(aps, "append_jsonl", lambda *args, **kwargs: None)

    deferred = aps.ensure_agent(spec)

    assert deferred["action"] == "restart_deferred"
    assert deferred["restart_gate"]["reason"] == "restart_backoff_active"
    assert started == []
    assert incidents == []

    old = (now - timedelta(seconds=aps.RESTART_WINDOW_SECONDS * 2)).isoformat(timespec="seconds")
    write_json(state_path, {"agents": {"stormy": {"state": "quarantined", "attempts": [old], "quarantined_at": old, "reason": "restart_circuit_breaker"}}})

    quarantined = aps.ensure_agent(spec)

    assert quarantined["action"] == "quarantined"
    assert quarantined["restart_gate"]["reason"] == "restart_quarantined"
    assert started == []

def test_start_failure_records_restart_attempt_and_next_tick_backs_off(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "agent.pid"
    spec = aps.AgentSpec("flaky", "flaky.py", tuple(), pid_file, None, None)
    events = []
    monkeypatch.setattr(aps, "is_pid_running", lambda pid, expected_script=None: False)
    monkeypatch.setattr(aps, "start_agent", lambda row: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(aps, "open_restart_incident", lambda *args, **kwargs: events.append(("incident", args, kwargs)))
    monkeypatch.setattr(aps, "append_jsonl", lambda event, payload: events.append((event, payload)))

    failed = aps.ensure_agent(spec)
    deferred = aps.ensure_agent(spec)
    state = json.loads((tmp_path / "agent_restart_state.json").read_text(encoding="utf-8"))

    assert failed["action"] == "start_failed"
    assert failed["restart_gate"]["reason"] == "restart_backoff_active"
    assert deferred["action"] == "restart_deferred"
    assert deferred["restart_gate"]["reason"] == "restart_backoff_active"
    assert state["agents"]["flaky"]["restart_count_window"] == 1
    assert any(event == "agent_start_failed" for event, *_ in events)
    assert not any(event == "incident" for event, *_ in events)

def test_sticky_quarantine_does_not_emit_incident_every_tick(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "agent.pid"
    spec = aps.AgentSpec("stormy", "stormy.py", tuple(), pid_file, None, None)
    old = (datetime.now(timezone.utc) - timedelta(seconds=aps.RESTART_WINDOW_SECONDS * 2)).isoformat(timespec="seconds")
    write_json(tmp_path / "agent_restart_state.json", {"agents": {"stormy": {"state": "quarantined", "attempts": [old], "quarantined_at": old, "reason": "restart_circuit_breaker"}}})
    incidents = []
    monkeypatch.setattr(aps, "is_pid_running", lambda pid, expected_script=None: False)
    monkeypatch.setattr(aps, "open_restart_incident", lambda *args, **kwargs: incidents.append((args, kwargs)))
    monkeypatch.setattr(aps, "append_jsonl", lambda *args, **kwargs: None)

    first = aps.ensure_agent(spec)
    second = aps.ensure_agent(spec)

    assert first["action"] == "quarantined"
    assert second["action"] == "quarantined"
    assert first["restart_gate"]["reason"] == "restart_quarantined"
    assert incidents == []


def test_sleep_resume_gap_written_to_health_and_pauses_paper_opens(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(hrm, "ROOT", tmp_path)
    output = tmp_path / "host.json"
    proof = tmp_path / "autostart.json"
    write_json(output, {"checked_at": "2000-01-01T00:00:00+00:00"})
    write_json(
        proof,
        {
            "trigger": "AtStartup",
            "working_dir": str(tmp_path),
            "venv_python": str(tmp_path / "venv" / "Scripts" / "pythonw.exe"),
            "user_context": "ACER",
            "env_source": "sanitized_env",
            "run_whether_user_logged_on": True,
            "post_reboot_assertion": True,
            "verification_source": "task_scheduler",
            "task_query_ok": True,
            "verified_at": "2026-06-30T00:00:00+00:00",
        },
    )

    result = hrm.check_host_runtime(min_free_gb=0, output_path=output, autostart_proof_path=proof, sleep_gap_threshold_seconds=1)

    assert result["autostart_confirmed"] is True
    assert result["sleep_resume"]["detected"] is True
    assert result["sleep_resume"]["pause_paper_opens"] is True
    assert result["sleep_resume"]["promotion_window_valid"] is False
    assert "sleep_resume_gap_detected" in result["warnings"]

def test_sleep_resume_pause_latches_until_replay_ack_and_stale_fails_closed(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(hrm, "ROOT", tmp_path)
    output = tmp_path / "host.json"
    proof = tmp_path / "autostart.json"
    write_json(
        proof,
        {
            "trigger": "AtStartup",
            "working_dir": str(tmp_path),
            "venv_python": str(tmp_path / "venv" / "Scripts" / "pythonw.exe"),
            "user_context": "ACER",
            "env_source": "sanitized_env",
            "run_whether_user_logged_on": True,
            "post_reboot_assertion": True,
            "verification_source": "task_scheduler",
            "task_query_ok": True,
            "verified_at": "2026-06-30T00:00:00+00:00",
        },
    )
    write_json(
        output,
        {
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sleep_resume": {"pause_paper_opens": True, "replay_required": True, "promotion_window_valid": False},
        },
    )

    latched = hrm.check_host_runtime(min_free_gb=0, output_path=output, autostart_proof_path=proof, sleep_gap_threshold_seconds=999999)
    paused = hrm.paper_opens_paused_by_runtime(output, max_age_seconds=999999)
    ack = hrm.acknowledge_sleep_resume_replay(output, actor="test")
    after_ack = hrm.paper_opens_paused_by_runtime(output, max_age_seconds=999999)

    assert latched["sleep_resume"]["latched"] is True
    assert paused["paused"] is True
    assert paused["reason"] == "sleep_resume_replay_required"
    assert ack["ok"] is True
    assert after_ack["paused"] is False

    write_json(output, {"checked_at": "2000-01-01T00:00:00+00:00", "sleep_resume": {"pause_paper_opens": False, "replay_required": False}})
    stale = hrm.paper_opens_paused_by_runtime(output, max_age_seconds=1)
    missing = hrm.paper_opens_paused_by_runtime(tmp_path / "missing.json", max_age_seconds=1)

    assert stale["paused"] is True
    assert stale["reason"] == "host_runtime_stale"
    assert missing["paused"] is True
    assert missing["reason"] == "host_runtime_missing"


def test_autostart_proof_rejects_false_strings():
    proof = {
        "trigger": "AtStartup",
        "working_dir": "E:/repo",
        "venv_python": "E:/repo/venv/Scripts/pythonw.exe",
        "user_context": "ACER",
        "env_source": "sanitized_env",
        "run_whether_user_logged_on": "false",
        "post_reboot_assertion": "true",
        "verification_source": "task_scheduler",
        "task_query_ok": "false",
        "verified_at": "2026-06-30T00:00:00+00:00",
    }

    result = hrm.validate_autostart_proof(proof)

    assert result["ok"] is False
    assert result["run_whether_user_logged_on"] is False
    assert result["task_query_ok"] is False
    assert "run_whether_user_logged_on" not in result["missing"]
    assert "task_query_ok" not in result["missing"]


def test_paper_brain_consumes_runtime_pause_and_skips_open(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(brain, "BRAIN_LATEST", tmp_path / "brain.json")
    monkeypatch.setattr(brain, "BRAIN_HISTORY", tmp_path / "brain.jsonl")
    monkeypatch.setattr(brain, "PAPER_RISK_STATE", tmp_path / "risk.json")
    monkeypatch.setattr(brain, "paper_opens_paused_by_runtime", lambda: {"paused": True, "reason": "sleep_resume_gap_detected", "replay_required": True, "promotion_window_valid": False})

    decision = brain.decide_paper_action(
        [{"symbol": "BTCUSDT", "side": "LONG", "setup_id": "s", "score": 10, "entry": 100, "sl": 99, "tp": 102}],
        [{"setup_id": "s", "trades": 100, "expectancy": 0.1, "profit_factor": 2.0}],
        {"equity": "100", "cash": "100"},
    )

    assert decision["action"] == "skip"
    assert decision["errors"] == ["host_runtime_pause_paper_opens"]
    assert decision["risk_decision"]["can_open_paper"] is False

def test_paper_brain_requires_clean_lifecycle_preflight(monkeypatch, tmp_path: Path):
    captured = {}
    monkeypatch.setattr(brain, "BRAIN_LATEST", tmp_path / "brain.json")
    monkeypatch.setattr(brain, "BRAIN_HISTORY", tmp_path / "brain.jsonl")
    monkeypatch.setattr(brain, "PAPER_RISK_STATE", tmp_path / "risk.json")
    monkeypatch.setattr(brain, "paper_opens_paused_by_runtime", lambda: {"paused": False, "reason": "ok", "replay_required": False, "promotion_window_valid": True})
    monkeypatch.setattr(brain, "evaluate_candidate", lambda candidate: {"blocked": False, "action": "allow"})
    monkeypatch.setattr(brain, "evaluate_paper_order", lambda *args, **kwargs: {"can_open_paper": True, "errors": [], "risk_decision_id": "r1"})

    def fake_preflight(action, **kwargs):
        captured["action"] = action
        return {"allowed": False, "errors": ["trade_lifecycle_not_clean"]}

    monkeypatch.setattr(brain, "run_preflight", fake_preflight)
    decision = brain.decide_paper_action(
        [{"symbol": "BTCUSDT", "side": "LONG", "setup_id": "s", "score": 10, "entry": 100, "sl": 99, "tp": 102}],
        [{"setup_id": "s", "trades": 100, "expectancy": 0.1, "profit_factor": 2.0}],
        {"equity": "100", "cash": "100"},
    )

    assert captured["action"]["requires_lifecycle_clean"] is True
    assert decision["action"] == "skip"
    assert "trade_lifecycle_not_clean" in decision["errors"]

def test_paper_brain_sanitizes_candidate_live_intent_before_open(monkeypatch, tmp_path: Path):
    captured = {}
    monkeypatch.setattr(brain, "BRAIN_LATEST", tmp_path / "brain.json")
    monkeypatch.setattr(brain, "BRAIN_HISTORY", tmp_path / "brain.jsonl")
    monkeypatch.setattr(brain, "PAPER_RISK_STATE", tmp_path / "risk.json")
    monkeypatch.setattr(brain, "paper_opens_paused_by_runtime", lambda: {"paused": False, "reason": "ok", "replay_required": False, "promotion_window_valid": True})
    monkeypatch.setattr(brain, "evaluate_candidate", lambda candidate: {"blocked": False, "action": "allow"})
    monkeypatch.setattr(brain, "evaluate_paper_order", lambda *args, **kwargs: {"can_open_paper": True, "errors": [], "risk_decision_id": "r1"})

    def fake_preflight(action, **kwargs):
        captured["action"] = action
        candidate = dict(action["candidate"])
        candidate["canPlaceLiveOrders"] = False
        candidate["executionMode"] = "paper"
        return {"allowed": False, "errors": ["live_intent_blocked_phase_a"], "action": {**action, "candidate": candidate}}

    monkeypatch.setattr(brain, "run_preflight", fake_preflight)
    decision = brain.decide_paper_action(
        [{"symbol": "BTCUSDT", "side": "LONG", "setup_id": "s", "score": 10, "entry": 100, "sl": 99, "tp": 102, "canPlaceLiveOrders": True, "executionMode": "LIVE"}],
        [{"setup_id": "s", "trades": 100, "expectancy": 0.1, "profit_factor": 2.0}],
        {"equity": "100", "cash": "100"},
    )

    assert captured["action"]["candidate"]["canPlaceLiveOrders"] is True
    assert decision["action"] == "skip"
    assert "live_intent_blocked_phase_a" in decision["errors"]
    assert decision["candidate"]["canPlaceLiveOrders"] is False
    assert decision["candidate"]["executionMode"] == "paper"

def test_counterfactual_does_not_ack_sleep_resume_when_catchup_incomplete(monkeypatch, tmp_path: Path):
    memory = tmp_path / "agent_memory"
    memory.mkdir()
    host = tmp_path / "host_runtime.json"
    write_json(host, {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "sleep_resume": {"pause_paper_opens": True, "replay_required": True, "promotion_window_valid": False}})
    monkeypatch.setattr(cf, "MEMORY_DIR", memory)
    monkeypatch.setattr(cf, "PAPER_TRADES_JSONL", memory / "paper_trades.jsonl")
    monkeypatch.setattr(cf, "PAPER_BRAIN_HISTORY_JSONL", memory / "paper_trading_brain_history.jsonl")
    monkeypatch.setattr(cf, "PAPER_CANDIDATE_HISTORY_JSONL", memory / "paper_candidate_feeder_history.jsonl")
    monkeypatch.setattr(cf, "REPLAYS_JSONL", memory / "counterfactual_replays.jsonl")
    monkeypatch.setattr(cf, "LATEST_JSON", memory / "counterfactual_latest.json")
    monkeypatch.setattr(cf, "HEARTBEAT_PATH", tmp_path / "counterfactual_heartbeat.json")
    monkeypatch.setattr(cf, "acknowledge_sleep_resume_replay", lambda detail=None: hrm.acknowledge_sleep_resume_replay(host, actor="test", detail=detail))

    result = cf.run_once(limit=10)
    pause = hrm.paper_opens_paused_by_runtime(host, max_age_seconds=999999)

    assert result["host_runtime_replay_ack"]["reason"] == "replay_catchup_incomplete"
    assert pause["paused"] is True
    assert pause["replay_required"] is True

def test_counterfactual_sleep_ack_uses_uncapped_eligible_total(monkeypatch, tmp_path: Path):
    memory = tmp_path / "agent_memory"
    memory.mkdir()
    host = tmp_path / "host_runtime.json"
    write_json(host, {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "sleep_resume": {"pause_paper_opens": True, "replay_required": True, "promotion_window_valid": False}})
    monkeypatch.setattr(cf, "MEMORY_DIR", memory)
    monkeypatch.setattr(cf, "PAPER_TRADES_JSONL", memory / "paper_trades.jsonl")
    monkeypatch.setattr(cf, "PAPER_BRAIN_HISTORY_JSONL", memory / "paper_trading_brain_history.jsonl")
    monkeypatch.setattr(cf, "PAPER_CANDIDATE_HISTORY_JSONL", memory / "paper_candidate_feeder_history.jsonl")
    monkeypatch.setattr(cf, "REPLAYS_JSONL", memory / "counterfactual_replays.jsonl")
    monkeypatch.setattr(cf, "LATEST_JSON", memory / "counterfactual_latest.json")
    monkeypatch.setattr(cf, "HEARTBEAT_PATH", tmp_path / "counterfactual_heartbeat.json")
    monkeypatch.setattr(cf, "acknowledge_sleep_resume_replay", lambda detail=None: hrm.acknowledge_sleep_resume_replay(host, actor="test", detail=detail))
    rows = []
    for index in range(12):
        rows.append(
            {
                "updated_at": f"2026-06-30T00:{index:02d}:00+00:00",
                "candidates": [
                    {
                        "candidate_id": f"candidate_{index}",
                        "symbol": "BTCUSDT",
                        "side": "LONG",
                        "entry": "100",
                        "sl": "99",
                        "tp": "102",
                        "source_available_at_max": "2026-06-30T00:00:00+00:00",
                        "trial_seq_cutoff": "2026-06-30T00:00:00+00:00",
                    }
                ],
            }
        )
    cf.PAPER_CANDIDATE_HISTORY_JSONL.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(cf, "candles_for_signal", lambda signal: ([{"ts": "2026-06-30T00:00:00+00:00", "open": "100", "high": "102", "low": "99", "close": "101"}], {"source": "test"}))

    def fake_replay_signal(signal, candles, append=True, candle_source=None):
        signal_id = cf.signal_id_for(signal)
        return cf.finalize_replay_result(
            {
                "schema_version": cf.SCHEMA_VERSION,
                "replay_id": cf.replay_id(signal_id, "fake"),
                "signal_id": signal_id,
                "status": "complete",
                "created_at": cf.utc_now(),
            },
            append=append,
        )

    monkeypatch.setattr(cf, "replay_signal", fake_replay_signal)
    result = cf.run_once(limit=10)
    pause = hrm.paper_opens_paused_by_runtime(host, max_age_seconds=999999)

    assert result["eligible_scanned"] == 10
    assert result["eligible_total"] == 12
    assert result["summary"]["latest_complete_count"] == 10
    assert result["host_runtime_replay_ack"]["reason"] == "replay_catchup_incomplete"
    assert pause["paused"] is True
    assert pause["replay_required"] is True

def test_counterfactual_ack_requires_exact_eligible_id_membership(monkeypatch, tmp_path: Path):
    memory = tmp_path / "agent_memory"
    memory.mkdir()
    host = tmp_path / "host_runtime.json"
    write_json(host, {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "sleep_resume": {"pause_paper_opens": True, "replay_required": True, "promotion_window_valid": False}})
    monkeypatch.setattr(cf, "MEMORY_DIR", memory)
    monkeypatch.setattr(cf, "PAPER_TRADES_JSONL", memory / "paper_trades.jsonl")
    monkeypatch.setattr(cf, "PAPER_BRAIN_HISTORY_JSONL", memory / "paper_trading_brain_history.jsonl")
    monkeypatch.setattr(cf, "PAPER_CANDIDATE_HISTORY_JSONL", memory / "paper_candidate_feeder_history.jsonl")
    monkeypatch.setattr(cf, "REPLAYS_JSONL", memory / "counterfactual_replays.jsonl")
    monkeypatch.setattr(cf, "LATEST_JSON", memory / "counterfactual_latest.json")
    monkeypatch.setattr(cf, "HEARTBEAT_PATH", tmp_path / "counterfactual_heartbeat.json")
    monkeypatch.setattr(cf, "acknowledge_sleep_resume_replay", lambda detail=None: hrm.acknowledge_sleep_resume_replay(host, actor="test", detail=detail))
    cf.PAPER_CANDIDATE_HISTORY_JSONL.write_text(json.dumps({"updated_at": "2026-06-30T00:00:00+00:00", "candidates": [{"candidate_id": "needed", "symbol": "BTCUSDT", "side": "LONG", "entry": "100", "sl": "99", "tp": "102", "trial_seq_cutoff": "2026-06-30T00:00:00+00:00"}]}) + "\n", encoding="utf-8")
    cf.REPLAYS_JSONL.write_text(
        "\n".join(
            json.dumps({"replay_id": f"r{i}", "signal_id": f"other_{i}", "status": "complete", "created_at": f"2026-06-30T00:00:0{i}+00:00"})
            for i in range(3)
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cf, "candles_for_signal", lambda signal: (_ for _ in ()).throw(RuntimeError("do not replay")))

    result = cf.run_once(limit=0)
    pause = hrm.paper_opens_paused_by_runtime(host, max_age_seconds=999999)

    assert result["summary"]["latest_complete_count"] >= 1
    assert result["catchup"]["complete"] is False
    assert result["catchup"]["unresolved_ids"] == ["needed"]
    assert result["host_runtime_replay_ack"]["reason"] == "replay_catchup_incomplete"
    assert pause["paused"] is True

def test_counterfactual_candidate_census_sanitizes_live_intent():
    signal = cf._candidate_census_signal(
        {"candidate_id": "c_live", "symbol": "BTCUSDT", "side": "LONG", "entry": "100", "sl": "99", "tp": "102", "canPlaceLiveOrders": True, "executionMode": "LIVE", "permission": True},
        {"updated_at": "2026-06-30T00:00:00+00:00"},
        0,
    )
    sanitized = cf.sanitize_replay_signal(signal)

    assert sanitized["canPlaceLiveOrders"] is False
    assert sanitized["executionMode"] == "paper"
    assert sanitized["permission"] is False
    assert sanitized["blocked"] is True
    assert "live_intent_blocked_phase_a" in sanitized["block_reason"]


def test_backup_excludes_env_and_restore_scan_blocks_sentinel(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(br, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(br, "BACKUP_MANIFESTS", tmp_path / "manifests")
    env_file = tmp_path / ".env"
    token_file = tmp_path / "token.json"
    credential_file = tmp_path / "secrets" / "api.txt"
    safe_file = tmp_path / "state.json"
    credential_file.parent.mkdir()
    env_file.write_text("BINANCE_API_KEY=SECRET123456789012345678901234567890", encoding="utf-8")
    token_file.write_text('{"token":"SECRET123456789012345678901234567890"}', encoding="utf-8")
    credential_file.write_text("secret", encoding="utf-8")
    safe_file.write_text('{"ok":true}', encoding="utf-8")

    manifest = br.backup_files([env_file, token_file, credential_file, safe_file])

    assert str(env_file) in manifest["excluded_secret_paths"]
    assert str(token_file) in manifest["excluded_secret_paths"]
    assert str(credential_file) in manifest["excluded_secret_paths"]
    backed = [row for row in manifest["files"] if row.get("ok")]
    assert len(backed) == 1
    assert backed[0]["source_path"] == str(safe_file)

    bad_backup = tmp_path / "bad.bak"
    bad_backup.write_text("sk-testsecretvalue123456789", encoding="utf-8")
    target = tmp_path / "restore.json"
    restored = br.restore_backup(bad_backup, target)

    assert restored["ok"] is False
    assert "restore_secret_scan_failed" in restored["errors"]
    assert not target.exists()

    json_secret_backup = tmp_path / "json_secret.bak"
    json_secret_backup.write_text('{"api_key":"SECRET123456789012345678901234567890"}', encoding="utf-8")
    json_secret = br.restore_backup(json_secret_backup, tmp_path / "json_restore.json")
    assert json_secret["ok"] is False
    assert "api_key=<redacted>" in json_secret["secret_scan"]["pattern_hits"]

    binance_secret_backup = tmp_path / "binance_secret.bak"
    binance_secret_backup.write_text('{"BINANCE_API_KEY":"SECRET123456789012345678901234567890","auth":"Bearer abcdefghijklmnopqrstuvwxyz1234567890"}', encoding="utf-8")
    binance_secret = br.restore_backup(binance_secret_backup, tmp_path / "binance_restore.json")
    assert binance_secret["ok"] is False
    assert any("binance_api_key" in hit.lower() or "Bearer" in hit for hit in binance_secret["secret_scan"]["pattern_hits"])

    bearer_secret_backup = tmp_path / "bearer_secret.bak"
    bearer_secret_backup.write_text("Authorization: Bearer abcd/efgh+ijkl/mnop+qrst/uvwx+yz12/3456~abcd", encoding="utf-8")
    bearer_secret = br.restore_backup(bearer_secret_backup, tmp_path / "bearer_restore.json")
    assert bearer_secret["ok"] is False

    placeholder_backup = tmp_path / "placeholder.bak"
    placeholder_backup.write_text("api_key=placeholder_012345678901234567890", encoding="utf-8")
    placeholder = br.restore_backup(placeholder_backup, tmp_path / "placeholder_restore.json")
    assert placeholder["ok"] is True

    pem_secret_backup = tmp_path / "pem_secret.bak"
    pem_secret_backup.write_text("-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----", encoding="utf-8")
    pem_secret = br.restore_backup(pem_secret_backup, tmp_path / "pem_restore.json")
    assert pem_secret["ok"] is False

    clean_backup = tmp_path / "clean.bak"
    clean_backup.write_text('{"hash":"0123456789abcdef0123456789abcdef0123456789abcdef"}', encoding="utf-8")
    clean_target = tmp_path / "clean_restore.json"
    clean = br.restore_backup(clean_backup, clean_target)
    assert clean["ok"] is True
    assert clean_target.exists()


def test_incident_schema_requires_ack_resolve_close_fields(tmp_path: Path):
    latest = alerts.open_incident(
        "Sev2",
        "dashboard failed",
        {"error": "fail"},
        source="dashboard_probe",
        owner="operator",
        runbook_id="runbook_dashboard_healthz",
        history_path=tmp_path / "incidents.jsonl",
        latest_path=tmp_path / "incidents.json",
    )
    incident = latest["latest"]

    for key in ("incident_id", "severity", "status", "owner", "opened_at", "acked_at", "resolved_at", "closed_at", "action_required", "runbook_id"):
        assert key in incident
    assert alerts.incident_complete(incident) is False
    closed = {**incident, "status": "closed", "acked_at": "2026-06-30T00:01:00+00:00", "resolved_at": "2026-06-30T00:02:00+00:00", "closed_at": "2026-06-30T00:03:00+00:00"}
    assert alerts.incident_complete(closed) is True

def test_incident_reopen_persists_history_and_latest(tmp_path: Path):
    history = tmp_path / "incidents.jsonl"
    latest_path = tmp_path / "incidents.json"
    opened = alerts.open_incident("Sev2", "dashboard failed", {"error": "fail"}, source="dashboard_probe", history_path=history, latest_path=latest_path)
    closed = alerts.update_incident_status(opened["latest"], "closed", history_path=history, latest_path=latest_path)
    reopened = alerts.open_incident("Sev2", "dashboard failed", {"error": "again"}, source="dashboard_probe", history_path=history, latest_path=latest_path)

    rows = history.read_text(encoding="utf-8").splitlines()
    latest = json.loads(latest_path.read_text(encoding="utf-8"))

    assert closed["status"] == "closed"
    assert reopened["latest"]["status"] == "open"
    assert len(rows) == 3
    assert latest["latest"]["status"] == "open"
    assert latest["open_count"] == 1


def test_incident_lifecycle_and_slo_burn_catalog(tmp_path: Path):
    incident = {
        "incident_id": "inc_1",
        "severity": "Sev2",
        "status": "open",
        "owner": "operator",
        "opened_at": "2026-06-30T00:00:00+00:00",
        "acked_at": None,
        "resolved_at": None,
        "closed_at": None,
        "runbook_id": "runbook_dashboard_healthz",
    }

    history = tmp_path / "incidents.jsonl"
    latest = tmp_path / "incidents.json"
    alerts.append_jsonl(history, incident)
    acked = alerts.update_incident_status(incident, "acked", actor="operator", history_path=history, latest_path=latest)
    closed = alerts.update_incident_status(acked, "closed", actor="operator", history_path=history, latest_path=latest)
    burn = alerts.evaluate_slo_burn({"dashboard_healthz_failed": 1, "event_bus_lag_high": 700})

    assert acked["acked_at"]
    assert alerts.incident_complete(closed) is True
    persisted = json.loads(latest.read_text(encoding="utf-8"))
    assert persisted["latest"]["status"] == "closed"
    assert persisted["open_count"] == 0
    assert burn["status"] == "breached"
    assert burn["incident_count"] == 2
    assert {row["runbook_id"] for row in burn["incidents"]} == {"runbook_dashboard_healthz", "runbook_event_bus_lag"}

def test_phase21_runbooks_have_hidden_runner_and_catalog_fields():
    host = (Path(__file__).resolve().parents[1] / "host_runtime_runbook.md").read_text(encoding="utf-8")
    incident = (Path(__file__).resolve().parents[1] / "incident_runbook.md").read_text(encoding="utf-8")
    runner = (Path(__file__).resolve().parents[1] / "scripts" / "run_supervisor_hidden.ps1").read_text(encoding="utf-8")

    assert "-NoProfile -NonInteractive" in host
    assert "-WindowStyle Hidden" in host
    assert "New-ScheduledTaskTrigger -AtStartup" in host
    assert "LastTaskResult" in host
    assert "Set-StrictMode" in runner
    assert "Set-Location -LiteralPath" in runner
    assert "$ErrorActionPreference = \"Stop\"" in runner
    assert "exit $LASTEXITCODE" in runner
    for field in ("Owner:", "Escalation:", "Expected output:", "Rollback:", "Validation:", "Postmortem trigger:"):
        assert field in incident

def test_paper_account_reconciler_rebuilds_ledger_and_blocks_live_scope(tmp_path: Path):
    trades = tmp_path / "paper_trades.jsonl"
    trades.write_text(
        "\n".join(
            [
                json.dumps({"event": "paper_open", "trade_id": "t1", "symbol": "BTCUSDT", "side": "LONG"}),
                json.dumps({"event": "paper_close", "trade_id": "t1", "net": "1.25"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    ok = par.reconcile_paper_account(trades_path=trades, output_path=tmp_path / "reconcile.json", latest_account={"starting_equity": "100", "equity": "101.25", "open_positions": []})
    live = par.reconcile_paper_account(trades_path=trades, output_path=tmp_path / "live_reconcile.json", latest_account={"starting_equity": "100", "equity": "101.25", "environment": "live", "account_scope": "paper", "open_positions": []})

    assert ok["ok"] is True
    assert ok["source_mode"] == "paper_ledger"
    assert ok["credential_fingerprint"] == "none"
    assert live["ok"] is False
    assert "live_account_snapshot_forbidden" in live["errors"]
    assert live["environment"] == "live"

def test_restore_gate_cold_start_and_split_brain_drills(tmp_path: Path):
    blocked = rd.restore_serve_gate_status({"event_replay_ok": True, "erasure_replay_ok": False}, output_path=tmp_path / "gate.json")
    allowed = rd.restore_serve_gate_status({key: True for key in rd.RESTORE_SERVE_GATES}, output_path=tmp_path / "gate_ok.json")
    canonical = tmp_path / "events.jsonl"
    latest = tmp_path / "latest.json"
    canonical.write_text("{}", encoding="utf-8")
    latest.write_text("stale", encoding="utf-8")
    cold = rd.cold_start_from_crash_drill([canonical], [latest], output_path=tmp_path / "cold.json")
    split = rd.split_brain_drill(100, [100, 101], {"owner": "evil_dashboard", "pid": 101}, output_path=tmp_path / "split.json")

    assert blocked["can_serve_dashboard"] is False
    assert "erasure_replay_ok" in blocked["missing_gates"]
    assert allowed["can_serve_dashboard"] is True
    assert cold["ok"] is True
    assert latest.exists() is False
    assert cold["stale_latest_trusted"] is False
    assert split["ok"] is False
    assert "duplicate_process_detected" in split["errors"]
    assert split["writer_allowed"] is False

def test_operator_governance_and_signed_idempotent_commands(tmp_path: Path):
    governance = opc.validate_ops_governance()
    payload = {"incident_id": "inc_1"}
    signature = opc.command_signature("acknowledge_incident", "operator", "n1", payload)
    first = opc.record_operator_command("acknowledge_incident", "operator", payload, nonce="n1", signature=signature, audit_path=tmp_path / "ops.jsonl", latest_path=tmp_path / "ops_latest.json")
    second = opc.record_operator_command("acknowledge_incident", "operator", payload, nonce="n1", signature=signature, audit_path=tmp_path / "ops.jsonl", latest_path=tmp_path / "ops_latest.json")
    bad = opc.record_operator_command("live_order", "operator", {}, nonce="n2", audit_path=tmp_path / "ops.jsonl", latest_path=tmp_path / "ops_latest.json")
    tampered = opc.record_operator_command("acknowledge_incident", "operator", {"incident_id": "inc_2"}, nonce="n1", signature=signature, audit_path=tmp_path / "ops.jsonl", latest_path=tmp_path / "ops_latest.json")

    assert governance["ok"] is True
    assert "primary_operator" in governance["raci"]
    assert "restore_drill" in governance["allowed_commands"]
    assert first["accepted"] is True
    assert first["can_place_live_orders"] is False
    assert second["duplicate"] is True
    assert len((tmp_path / "ops.jsonl").read_text(encoding="utf-8").splitlines()) == 3
    assert bad["accepted"] is False
    assert "unknown_operator_command" in bad["errors"]
    assert tampered["accepted"] is False
    assert "idempotency_payload_mismatch" in tampered["errors"]

def test_state_reconciler_context_guard_forbids_live_readiness(tmp_path: Path):
    missing = sr.reconcile_positions([], [], output_path=tmp_path / "missing.json")
    live = sr.reconcile_positions([], [{"symbol": "BTCUSDT", "side": "LONG"}], output_path=tmp_path / "live.json", source_context={"source_mode": "live_account", "environment": "live", "account_scope": "paper", "credential_fingerprint": "secret", "source_ledger_id": "external"})
    paper = sr.reconcile_positions([], [], output_path=tmp_path / "paper.json", source_context={"source_mode": "paper_ledger", "environment": "paper", "account_scope": "paper", "credential_fingerprint": "none", "source_ledger_id": "ledger_1"})

    assert missing["ok"] is False
    assert "missing_source_mode" in missing["errors"]
    assert live["ok"] is False
    assert "live_account_snapshot_forbidden" in live["errors"]
    assert "invalid_source_mode" in live["errors"]
    assert paper["ok"] is True
    assert paper["paper_readiness_allowed"] is True
