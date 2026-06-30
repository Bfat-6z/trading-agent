import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import agent_data_contracts as contracts
import agent_process_supervisor as aps
import agent_runtime_contract as arc
import atomic_state
import episodic_task_ledger as ledger
import instrument_registry as registry
import llm_output_quality_gate as lqg
import legacy_live_blocker as llb
import live_permission_firewall as firewall
import paper_portfolio_manager as ppm
import preflight_guard as pfg
import runtime_config
import security_import_guard as sig
import timebase
import trade_lifecycle_validator as tlv


def paper_open(trade_id="t1", open_ts="2026-06-21T00:00:00+00:00"):
    return {
        "event": "paper_open",
        "trade_id": trade_id,
        "mode": "paper",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "setup_id": "aplus_pure",
        "open_ts": open_ts,
        "entry": "100",
        "qty": "0.15",
        "margin": "5",
        "leverage": "3",
        "sl": "99",
        "tp": "102",
        "risk_decision_id": "risk_1",
        "status": "open",
    }


def paper_close(trade_id="t1", open_ts="2026-06-21T00:00:00+00:00", close_ts="2026-06-21T00:03:00+00:00"):
    return {
        **paper_open(trade_id, open_ts),
        "event": "paper_close",
        "status": "closed",
        "close_ts": close_ts,
        "exit": "101",
        "fee": "0.01",
        "slippage": "0.001",
    }


def test_atomic_json_write_and_read_roundtrip(tmp_path: Path):
    path = tmp_path / "state" / "sample.json"
    atomic_state.write_json_atomic(path, {"b": 2, "a": 1})

    assert atomic_state.read_json(path) == {"a": 1, "b": 2}


def test_timebase_detects_close_before_open():
    errors = timebase.validate_event_order("2026-06-21T00:01:00+00:00", "2026-06-21T00:00:00+00:00")

    assert "close_before_open" in errors


def test_contract_validation_reports_missing_required_fields():
    result = contracts.validate_contract("paper_trade_event", {"trade_id": "x"})

    assert not result.ok
    assert any(error.startswith("missing:") for error in result.errors)


def test_runtime_config_forces_live_flag_into_degraded_safe():
    effective = runtime_config.evaluate_mode(
        {
            "mode": "paper_learning",
            "live_execution_enabled": True,
            "feature_flags": {"paper_trading": True, "live_orders": True},
        }
    )

    assert effective["mode"] == "degraded_safe"
    assert effective["feature_flags"]["live_orders"] is False
    assert "live_execution_not_allowed_in_phase_a" in effective["errors"]


def test_lifecycle_detects_duplicate_open_and_orphan_close():
    rows = [paper_open("t1"), paper_open("t1"), paper_close("orphan")]

    report = tlv.validate_trade_events(rows, now=datetime(2026, 6, 21, 0, 10, tzinfo=timezone.utc))

    all_errors = {error for item in report["invalid_events"] for error in item["errors"]}
    assert report["duplicate_opens"] == 1
    assert report["orphan_closes"] == 1
    assert "duplicate_open" in all_errors
    assert "orphan_close" in all_errors
    assert report["learning_allowed"] is False


def test_lifecycle_detects_stale_snapshot():
    row = paper_open("t1")
    row["market_snapshot_ts"] = "2026-06-20T23:00:00+00:00"

    report = tlv.validate_trade_events([row], max_snapshot_age_seconds=60, now=datetime(2026, 6, 21, 0, 10, tzinfo=timezone.utc))

    assert "stale_market_snapshot" in report["invalid_events"][0]["errors"]

def test_lifecycle_close_snapshot_is_checked_against_close_time_not_open_time():
    rows = [paper_open("t1"), paper_close("t1")]
    rows[1]["market_snapshot_ts"] = "2026-06-21T00:02:55+00:00"

    report = tlv.validate_trade_events(rows, max_snapshot_age_seconds=60, now=datetime(2026, 6, 21, 0, 10, tzinfo=timezone.utc))

    assert report["invalid_events_count"] == 0
    assert report["learning_allowed"] is True

def test_lifecycle_ignores_events_before_account_reset_window():
    rows = [
        paper_open("old", "2026-06-20T00:00:00+00:00"),
        paper_open("new", "2026-06-21T00:00:00+00:00"),
        paper_close("new", "2026-06-21T00:00:00+00:00", "2026-06-21T00:03:00+00:00"),
    ]

    report = tlv.validate_trade_events(
        rows,
        min_open_ts="2026-06-21T00:00:00+00:00",
        now=datetime(2026, 6, 21, 0, 10, tzinfo=timezone.utc),
    )

    assert report["ignored_events"] == 1
    assert report["stale_open_trades"] == []
    assert report["learning_allowed"] is True

def test_lifecycle_quarantines_known_contaminated_trade():
    bad_open = paper_open("bad")
    bad_open["market_snapshot_ts"] = "2026-06-20T23:00:00+00:00"
    rows = [
        bad_open,
        paper_close("bad"),
        paper_open("good"),
        paper_close("good"),
    ]

    report = tlv.validate_trade_events(
        rows,
        max_snapshot_age_seconds=60,
        now=datetime(2026, 6, 21, 0, 10, tzinfo=timezone.utc),
        quarantine_entries=[
            {
                "quarantine_id": "q_bad",
                "trade_id": "bad",
                "scope": "trade",
                "status": "active",
                "reason": "known_stale_market_snapshot_pre_guard",
                "open_ts": "2026-06-21T00:00:00+00:00",
            }
        ],
    )

    assert report["invalid_events_count"] == 0
    assert report["quarantined_events_count"] == 2
    assert report["valid_events"] == 2
    assert report["learning_allowed"] is True

def test_lifecycle_cli_uses_account_reset_window_and_quarantine(monkeypatch, tmp_path: Path):
    trades = tmp_path / "paper_trades.jsonl"
    account = tmp_path / "paper_account.json"
    quarantine = tmp_path / "trade_lifecycle_quarantine.jsonl"
    output = tmp_path / "latest.json"
    old_bad = paper_open("old", "2026-06-20T00:00:00+00:00")
    old_bad["market_snapshot_ts"] = "2026-06-19T00:00:00+00:00"
    bad = paper_open("bad", "2026-06-21T00:00:00+00:00")
    bad["market_snapshot_ts"] = "2026-06-20T23:00:00+00:00"
    rows = [old_bad, bad, paper_close("bad"), paper_open("good"), paper_close("good")]
    trades.write_text("\n".join(atomic_state.canonical_json(row) for row in rows) + "\n", encoding="utf-8")
    account.write_text('{"created_at":"2026-06-21T00:00:00+00:00"}\n', encoding="utf-8")
    quarantine.write_text(
        '{"quarantine_id":"q_bad","trade_id":"bad","scope":"trade","status":"active","reason":"known_stale_market_snapshot_pre_guard","open_ts":"2026-06-21T00:00:00+00:00"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(tlv, "PAPER_ACCOUNT", account)
    monkeypatch.setattr(tlv, "TRADE_LIFECYCLE_QUARANTINE", quarantine)

    code = tlv.main(["--output", str(output), str(trades)])
    report = atomic_state.read_json(output)

    assert code == 0
    assert report["min_open_ts"] == "2026-06-21T00:00:00+00:00"
    assert report["ignored_events"] == 1
    assert report["invalid_events_count"] == 0
    assert report["quarantined_events_count"] == 2
    assert report["learning_allowed"] is True

def test_lifecycle_detects_close_snapshot_after_close_time():
    rows = [paper_open("t1"), paper_close("t1")]
    rows[1]["market_snapshot_ts"] = "2026-06-21T00:03:05+00:00"

    report = tlv.validate_trade_events(rows, max_snapshot_age_seconds=60, now=datetime(2026, 6, 21, 0, 10, tzinfo=timezone.utc))

    assert "market_snapshot_after_trade_close" in report["invalid_events"][0]["errors"]


def test_episode_append_is_idempotent(tmp_path: Path):
    path = tmp_path / "episodes.jsonl"
    latest = tmp_path / "episodes_latest.json"
    row = ledger.build_episode("paper_close", "review closed paper trade", quality=0.8, episode_id="episode_fixed")

    first = ledger.append_episode(row, path, latest)
    second = ledger.append_episode(row, path, latest)

    assert first["last_inserted"] is True
    assert second["last_inserted"] is False
    assert second["episode_count"] == 1


def test_paper_portfolio_blocks_impossible_leverage(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(ppm, "RISK_STATE_PATH", tmp_path / "risk.json")

    decision = ppm.evaluate_paper_order(
        "BTCUSDT",
        "LONG",
        entry="100",
        sl="99",
        tp="102",
        requested_margin="5",
        requested_leverage="50",
        account=ppm.default_account(),
        instrument={"symbol": "BTCUSDT", "status": "trading", "tick_size": "0.1", "step_size": "0.001", "min_notional": "5", "max_leverage": "20"},
        config={"mode": "paper_learning", "live_execution_enabled": False, "feature_flags": {"paper_trading": True, "live_orders": False}},
    )

    assert decision["can_open_paper"] is False
    assert "requested_leverage_above_cap" in decision["errors"]

def test_paper_portfolio_allows_50x_when_instrument_allows(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(ppm, "RISK_STATE_PATH", tmp_path / "risk.json")

    decision = ppm.evaluate_paper_order(
        "BTCUSDT",
        "LONG",
        entry="100",
        sl="99.9",
        tp="101",
        requested_margin="1",
        requested_leverage="50",
        account=ppm.default_account(),
        instrument={"symbol": "BTCUSDT", "status": "trading", "tick_size": "0.1", "step_size": "0.001", "min_notional": "5", "max_leverage": "50"},
        config={"mode": "paper_learning", "live_execution_enabled": False, "feature_flags": {"paper_trading": True, "live_orders": False}},
    )

    assert decision["can_open_paper"] is True
    assert decision["leverage"] == "50"
    assert decision["can_place_live_orders"] is False

def test_paper_portfolio_rejects_below_min_notional_after_rounding(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(ppm, "RISK_STATE_PATH", tmp_path / "risk.json")

    decision = ppm.evaluate_paper_order(
        "BTCUSDT",
        "LONG",
        entry="100",
        sl="99",
        tp="102",
        requested_margin="0.01",
        requested_leverage="1",
        account=ppm.default_account(),
        instrument={"symbol": "BTCUSDT", "status": "trading", "tick_size": "0.1", "step_size": "0.001", "min_notional": "5", "max_leverage": "50"},
        config={"mode": "paper_learning", "live_execution_enabled": False, "feature_flags": {"paper_trading": True, "live_orders": False}},
    )

    assert decision["can_open_paper"] is False
    assert "notional_below_exchange_minimum" in decision["errors"]


def test_live_firewall_rejects_order_intent(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(firewall, "LATEST_PATH", tmp_path / "firewall.json")

    decision = firewall.evaluate_live_permission(
        {"action": "create_order", "mode": "live", "api_key": "A" * 40},
        {"mode": "paper_learning", "live_execution_enabled": False, "feature_flags": {"live_orders": False}},
        output_path=tmp_path / "firewall.json",
    )

    assert decision["allowed"] is False
    assert "live_intent_blocked_phase_a" in decision["errors"]
    assert "A" * 40 not in decision["request_redacted"]


def test_live_firewall_blocks_nested_live_intent_and_redacts_nested_secret(tmp_path: Path):
    secret = "sk_" + "B" * 40
    decision = firewall.evaluate_live_permission(
        {"payload": [{"tool": {"action": "futures_create_order", "api_secret": secret}}]},
        {"mode": "paper_learning", "live_execution_enabled": False, "feature_flags": {"live_orders": False}},
        output_path=tmp_path / "firewall.json",
    )
    raw = (tmp_path / "firewall.json").read_text(encoding="utf-8")

    assert decision["allowed"] is False
    assert decision["paper_action_allowed"] is False
    assert decision["live_permission"] is False
    assert decision["can_place_live_orders"] is False
    assert "live_intent_blocked_phase_a" in decision["errors"]
    assert secret not in raw
    assert decision["request_sanitized"]["payload"][0]["tool"]["api_secret"] == "[REDACTED]"

def test_llm_quality_gate_masks_ready_for_live_and_bare_live(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(lqg, "QUALITY_LATEST", tmp_path / "llm_latest.json")
    monkeypatch.setattr(lqg, "QUALITY_HISTORY", tmp_path / "llm_history.jsonl")

    result = lqg.sanitize_output(
        {
            "summary": "unsafe",
            "data_ids": ["d1"],
            "recommendations": [{"status": "LIVE", "ready_for_live": True, "readiness": "ready_for_live", "permission": True, "reason": "mainnet"}],
        },
        kind="council_synthesis",
    )
    serialized = json.dumps(result["sanitized"], sort_keys=True)

    assert result["ok"] is False
    assert "unsafe_live_intent" in result["errors"]
    assert "unsafe_risk_or_live_permission" in result["errors"]
    assert '"LIVE"' not in serialized
    assert result["sanitized"]["recommendations"][0]["status"] == "paper"
    assert result["sanitized"]["recommendations"][0]["ready_for_live"] is False
    assert result["sanitized"]["recommendations"][0]["readiness"] == "paper_only"
    assert result["sanitized"]["recommendations"][0]["permission"] is False
    assert result["sanitized"]["recommendations"][0]["reason"] == "paper"


def test_live_firewall_blocks_camel_case_and_dash_live_intent(tmp_path: Path):
    decision = firewall.evaluate_live_permission(
        {
            "canPlaceLiveOrders": True,
            "executionMode": "live",
            "CAN-TRADE-LIVE": True,
            "can-submit-live-orders": True,
            "live-eligible": True,
            "liveEligible": True,
        },
        {"mode": "paper_learning", "live_execution_enabled": False, "feature_flags": {"live_orders": False}},
        output_path=tmp_path / "firewall.json",
    )

    assert decision["allowed"] is False
    assert "live_intent_blocked_phase_a" in decision["errors"]
    assert decision["live_intent_paths"]
    assert decision["request_sanitized"]["canPlaceLiveOrders"] is False
    assert decision["request_sanitized"]["executionMode"] == "paper"
    assert decision["request_sanitized"]["CAN-TRADE-LIVE"] is False
    assert decision["request_sanitized"]["can-submit-live-orders"] is False
    assert decision["request_sanitized"]["live-eligible"] is False
    assert decision["request_sanitized"]["liveEligible"] is False

def test_live_firewall_sanitizes_bare_live_and_readiness_markers(tmp_path: Path):
    decision = firewall.evaluate_live_permission(
        {"status": "LIVE", "readiness": "ready_for_live", "readyForLive": True, "permission": True, "errors": ["LIVE"], "reason": "ready_for_live"},
        {"mode": "paper_learning", "live_execution_enabled": False, "feature_flags": {"live_orders": False}},
        output_path=tmp_path / "firewall.json",
    )

    assert decision["allowed"] is False
    assert "live_intent_blocked_phase_a" in decision["errors"]
    assert decision["request_sanitized"]["status"] == "paper"
    assert decision["request_sanitized"]["readiness"] == "paper_only"
    assert decision["request_sanitized"]["readyForLive"] is False
    assert decision["request_sanitized"]["permission"] is False
    assert decision["request_sanitized"]["errors"] == ["paper"]
    assert decision["request_sanitized"]["reason"] == "paper_only"


def test_preflight_kill_switch_blocks_and_sanitizes_action(monkeypatch, tmp_path: Path):
    state = tmp_path / "state"
    memory = state / "agent_memory"
    memory.mkdir(parents=True)
    monkeypatch.setattr(pfg, "STATE_DIR", state)
    monkeypatch.setattr(pfg, "MEMORY_DIR", memory)
    pfg.write_json_atomic(state / "KILL_SWITCH_ACTIVE.json", {"active": True})
    secret = "token=" + "C" * 40

    result = pfg.run_preflight(
        {"action": "paper_decision", "requires_fresh_market": False, "requires_lifecycle_clean": False, "nested": {"token": secret}},
        config={"mode": "paper_learning", "live_execution_enabled": False, "feature_flags": {"paper_trading": True, "live_orders": False}},
        output_path=state / "preflight_latest.json",
    )
    raw = (state / "preflight_latest.json").read_text(encoding="utf-8")

    assert result["allowed"] is False
    assert result["paper_action_allowed"] is False
    assert result["live_permission"] is False
    assert result["can_place_live_orders"] is False
    assert "kill_switch_active" in result["errors"]
    assert secret not in raw
    assert result["action"]["nested"]["token"] == "[REDACTED]"


def test_runtime_config_env_allowlist_and_live_key_fingerprint_only():
    secret = "D" * 40
    config = runtime_config.load_runtime_config(
        path=Path("missing_runtime_config.json"),
        env={
            "TRADING_AGENT_MODE": "paper_learning",
            "TRADING_AGENT_LIVE_ORDERS": "true",
            "BINANCE_API_SECRET": secret,
            "UNAPPROVED_ENV": "ignored",
        },
    )
    effective = runtime_config.evaluate_mode(config)
    raw = str(effective)

    assert effective["mode"] == "degraded_safe"
    assert "live_execution_not_allowed_in_phase_a" in effective["errors"]
    assert "live_trading_env_keys_present_phase_a" in effective["errors"]
    assert "UNAPPROVED_ENV" not in effective["runtime_config"]["env_used_fingerprints"]
    assert secret not in raw
    assert effective["runtime_config"]["dotenv_loaded"] is False
    assert effective["can_place_live_orders"] is False


def test_llm_quality_gate_blocks_nested_live_permission_and_redacts_secret(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(lqg, "QUALITY_LATEST", tmp_path / "llm_latest.json")
    monkeypatch.setattr(lqg, "QUALITY_HISTORY", tmp_path / "llm_history.jsonl")
    secret = "E" * 40

    result = lqg.sanitize_output(
        {
            "summary": "nested",
            "data_ids": ["d1"],
            "risk_proposal": {"nested": {"can_place_live_orders": True, "api_key": secret}},
            "recommendations": [{"action": "place real order"}],
        },
        kind="council_synthesis",
    )
    raw = (tmp_path / "llm_latest.json").read_text(encoding="utf-8")

    assert result["ok"] is False
    assert "unsafe_live_intent" in result["errors"]
    assert "unsafe_risk_or_live_permission" in result["errors"]
    assert result["sanitized"]["risk_proposal"]["nested"]["can_place_live_orders"] is False
    assert result["sanitized"]["risk_proposal"]["nested"]["api_key"] == "[REDACTED]"
    assert secret not in raw


def test_security_import_guard_blocks_legacy_live_script(tmp_path: Path):
    module = tmp_path / "execute_live_trade.py"
    module.write_text("client.futures_create_order(symbol='BTCUSDT')\n", encoding="utf-8")

    result = sig.scan_import_guard([module], output_path=tmp_path / "guard.json")

    assert result["ok"] is False
    assert result["violations"][0]["classification"] == "blocked_legacy_live"
    assert "futures_create_order" in result["violations"][0]["forbidden_calls"]
    assert result["can_place_live_orders"] is False


def test_legacy_live_blocker_denies_direct_execute_script(monkeypatch, tmp_path: Path):
    script = tmp_path / "execute_live_trade.py"
    script.write_text("client.futures_create_order(symbol='BTCUSDT')\n", encoding="utf-8")
    monkeypatch.setattr(llb, "DENIAL_HISTORY", tmp_path / "denials.jsonl")
    monkeypatch.setattr(llb, "DENIAL_LATEST", tmp_path / "denial.json")
    monkeypatch.setattr(llb, "MEMORY_DIR", tmp_path)
    codes = []

    event = llb.block_if_legacy_entrypoint([str(script)], exit_fn=codes.append)

    assert event["event"] == "legacy_script_blocked"
    assert event["classification"] == "blocked_legacy_live"
    assert event["can_place_live_orders"] is False
    assert event["live_permission"] is False
    assert codes == [78]
    assert (tmp_path / "denials.jsonl").exists()


def test_supervisor_scrubs_child_live_env():
    clean = aps.scrub_child_env(
        {
            "PATH": "ok",
            "NINEROUTER_API_KEY": "llm",
            "BINANCE_API_KEY": "live",
            "WALLET_PRIVATE_KEY": "private",
            "UNAPPROVED": "drop",
        }
    )

    assert clean["PATH"] == "ok"
    assert clean["NINEROUTER_API_KEY"] == "llm"
    assert clean["TRADING_AGENT_LIVE_ORDERS"] == "false"
    assert clean["TRADING_AGENT_CHILD_ENV_SCRUBBED"] == "1"
    assert "BINANCE_API_KEY" not in clean
    assert "WALLET_PRIVATE_KEY" not in clean
    assert "UNAPPROVED" not in clean


def test_instrument_registry_missing_blocks_paper_candidate():
    decision = registry.can_trade_paper("BTCUSDT", {"updated_at": registry.utc_now(), "registry_version": "test", "instruments": {}})

    assert decision["can_trade_paper"] is False
    assert "instrument_missing" in decision["errors"]


def test_agent_runtime_contract_detects_missing_artifacts(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(arc, "STATE_DIR", tmp_path)
    monkeypatch.setattr(arc, "MEMORY_DIR", tmp_path / "agent_memory")

    result = arc.validate_agent_runtime(arc.AgentRuntimeSpec("paper_agent"))

    assert result["ok"] is False
    assert "missing_pid_file" in result["errors"]
    assert "missing_heartbeat" in result["errors"]


def test_agent_runtime_contract_accepts_required_artifacts(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(arc, "STATE_DIR", tmp_path)
    monkeypatch.setattr(arc, "MEMORY_DIR", tmp_path / "agent_memory")
    (tmp_path / "agent_memory").mkdir()
    (tmp_path / "paper_agent.pid").write_text("123", encoding="ascii")
    atomic_state.write_json_atomic(tmp_path / "paper_agent_heartbeat.json", {"ts": timebase.utc_now()})
    atomic_state.write_json_atomic(tmp_path / "agent_memory" / "paper_agent_latest.json", {"updated_at": timebase.utc_now()})

    result = arc.validate_agent_runtime(arc.AgentRuntimeSpec("paper_agent"))

    assert result["ok"] is True
    assert result["warnings"] == ["missing_history_jsonl"]
