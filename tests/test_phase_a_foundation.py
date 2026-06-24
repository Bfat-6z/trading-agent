from datetime import datetime, timedelta, timezone
from pathlib import Path

import agent_data_contracts as contracts
import agent_runtime_contract as arc
import atomic_state
import episodic_task_ledger as ledger
import instrument_registry as registry
import live_permission_firewall as firewall
import paper_portfolio_manager as ppm
import runtime_config
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
