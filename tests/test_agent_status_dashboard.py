import json
from pathlib import Path

import agent_status_dashboard as dash


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_summarize_paper_counts_closed_trades():
    rows = [
        {"event": "paper_open", "position": {"qty": "0.1"}},
        {"event": "paper_close", "net": "0.12"},
        {"event": "paper_close", "net": "-0.04"},
        {"event": "risk_block"},
    ]

    summary = dash.summarize_paper(rows)

    assert summary["opens"] == 1
    assert summary["closes"] == 2
    assert summary["wins"] == 1
    assert summary["losses"] == 1
    assert summary["win_rate"] == 0.5
    assert summary["net"] == 0.08
    assert summary["risk_blocks"] == 1

def test_summarize_paper_account_counts_open_futures_positions():
    summary = dash.summarize_paper_account(
        {
            "open_positions": [
                {"symbol": "BTCUSDT", "margin": "7.5", "notional": "75"},
                {"symbol": "ETHUSDT", "margin": "2.25", "notional": "22.5"},
            ]
        }
    )

    assert summary["open_position_count"] == 2
    assert summary["open_margin"] == 9.75
    assert summary["open_notional"] == 97.5

def test_summarize_paper_report_builds_progress_metrics():
    rows = [
        {"event": "paper_close", "trade_id": "t1", "close_ts": "2026-06-21T00:01:00+00:00", "symbol": "BTCUSDT", "side": "LONG", "net": "1.5"},
        {"event": "paper_close", "trade_id": "t2", "close_ts": "2026-06-21T00:02:00+00:00", "symbol": "ETHUSDT", "side": "SHORT", "net": "-0.5"},
        {"event": "paper_close", "trade_id": "t2", "close_ts": "2026-06-21T00:02:00+00:00", "symbol": "ETHUSDT", "side": "SHORT", "net": "-0.5"},
    ]

    report = dash.summarize_paper_report(rows, {"starting_equity": "100", "equity": "101"})

    assert report["closed_trades"] == 2
    assert report["wins"] == 1
    assert report["losses"] == 1
    assert report["net"] == 1.0
    assert report["profit_factor"] == 3.0
    assert len(report["curve"]) == 3
    assert report["recent_closes"][-1]["symbol"] == "ETHUSDT"
    assert report["return_pct"] == 0.01
    assert report["rolling"]["5"]["count"] == 2
    assert report["breakdown"]["by_symbol"][0]["trades"] == 1
    assert report["pnl_histogram"]
    assert "best_trade" in report
    assert "worst_trade" in report

def test_summarize_paper_report_separates_current_reset_from_history():
    rows = [
        {"event": "paper_close", "trade_id": "old1", "close_ts": "2026-06-23T23:59:00+00:00", "symbol": "OLDUSDT", "side": "LONG", "net": "-10"},
        {"event": "paper_close", "trade_id": "new1", "close_ts": "2026-06-24T04:26:00+00:00", "symbol": "BTCUSDT", "side": "LONG", "net": "2"},
        {"event": "paper_close", "trade_id": "new2", "close_ts": "2026-06-24T04:27:00+00:00", "symbol": "ETHUSDT", "side": "SHORT", "net": "-0.5"},
    ]

    report = dash.summarize_paper_report(
        rows,
        {
            "created_at": "2026-06-24T04:25:34+00:00",
            "starting_equity": "100",
            "equity": "101.5",
            "realized_pnl": "1.5",
            "closed_trades": 2,
            "open_positions": [{"symbol": "BNBUSDT", "margin": "10", "notional": "50"}],
        },
    )

    assert report["window"] == "current_reset"
    assert report["closed_trades"] == 2
    assert report["net"] == 1.5
    assert report["historical"]["closed_trades"] == 3
    assert report["historical"]["net"] == -8.5
    assert report["account_alignment"]["closed_trade_count_delta"] == 0
    assert report["account_alignment"]["realized_pnl_delta"] == 0
    assert report["open_margin"] == 10

def test_paper_runtime_status_uses_new_paper_loops():
    heartbeats = [
        {"name": "paper_candidate_feeder", "state": "ok", "running": True},
        {"name": "autonomous_paper_trading_loop", "state": "ok", "running": True},
        {"name": "paper_execution_lifecycle_loop", "state": "ok", "running": True},
    ]

    runtime = dash.paper_runtime_status(heartbeats)

    assert runtime["state"] == "running"
    assert runtime["running"] is True
    assert runtime["healthy_count"] == 3

def test_paper_runtime_status_marks_degraded_when_a_loop_is_unhealthy():
    heartbeats = [
        {"name": "paper_candidate_feeder", "state": "ok", "running": True},
        {"name": "autonomous_paper_trading_loop", "state": "ok", "running": True},
        {"name": "paper_execution_lifecycle_loop", "state": "stale", "running": True},
    ]

    runtime = dash.paper_runtime_status(heartbeats)

    assert runtime["state"] == "degraded"
    assert runtime["running"] is True

def test_paper_runtime_status_includes_objective_learning_loops():
    heartbeats = [
        {"name": "paper_candidate_feeder", "state": "ok", "running": True},
        {"name": "autonomous_paper_trading_loop", "state": "ok", "running": True},
        {"name": "paper_execution_lifecycle_loop", "state": "ok", "running": True},
        {"name": "counterfactual_replay_agent", "state": "stale", "running": True},
        {"name": "shadow_trade_evaluator_loop", "state": "ok", "running": True},
        {"name": "promotion_evaluator_loop", "state": "ok", "running": True},
    ]

    runtime = dash.paper_runtime_status(heartbeats)

    assert runtime["state"] == "degraded"
    assert "counterfactual_replay_agent" in runtime["tracked"]
    assert "shadow_trade_evaluator_loop" in runtime["tracked"]


def test_compact_beliefs_sorts_by_confidence():
    ledger = {
        "beliefs": {
            "a": {"belief_id": "a", "statement": "low", "confidence": 0.2, "status": "weakened"},
            "b": {"belief_id": "b", "statement": "high", "confidence": 0.8, "status": "active"},
        }
    }

    compact = dash.compact_beliefs(ledger)

    assert compact["count"] == 2
    assert compact["top"][0]["id"] == "b"

def test_compact_news_preserves_tighten_only_contract():
    compact = dash.compact_news(
        {
            "macro_risk_score": 0.2,
            "crypto_regulatory_risk": 0.4,
            "top_events": [{"title": "SEC sues exchange"}],
            "source_health": [{"source": "fake", "status": "ok"}],
            "symbol_impacts": {"BTC": {"risk": 0.3}},
            "can_place_orders": False,
            "can_loosen_risk": False,
        }
    )

    assert compact["crypto_regulatory_risk"] == 0.4
    assert compact["risk_contract"] == "tighten_only"
    assert compact["can_place_orders"] is False
    assert compact["can_loosen_risk"] is False

def test_compact_shadow_performance_keeps_shadow_separate_and_quality_visible():
    compact = dash.compact_shadow_performance(
        {
            "schema_version": 1,
            "updated_at": "now",
            "assumption_hash": "abc",
            "metric_mode": "closed_only",
            "overall": {"trades": 3, "closed": 2, "wins": 1, "losses": 1, "win_rate": 0.5, "net": 0.04, "expectancy": 0.02, "profit_factor": 1.4},
            "fresh_window": {
                "start_ts": "2026-06-24T00:00:00+00:00",
                "row_count": 1,
                "overall": {"closed": 1, "wins": 1, "losses": 0, "win_rate": 1.0, "net": 0.03, "expectancy": 0.03, "profit_factor": 999.0},
                "data_quality": {"confidence": "low", "api_error_count": 0, "unresolved_count": 0, "ambiguous_count": 0},
            },
            "data_quality": {"confidence": "low", "unresolved_count": 1, "ambiguous_count": 1, "skipped_count": 0, "mixed_assumptions": False},
            "segments": {"by_symbol": [{"key": "BTCUSDT", "closed": 2, "expectancy": 0.02, "net": 0.04}]},
            "kill_candidates": [{"group": "by_side", "key": "LONG", "closed": 20, "expectancy": -0.01}],
        }
    )

    assert compact["closed"] == 2
    assert compact["under_sampled"] is True
    assert compact["data_quality"]["ambiguous_count"] == 1
    assert compact["fresh_window"]["closed"] == 1
    assert compact["fresh_window"]["expectancy"] == 0.03
    assert compact["top_segments"][0]["key"] == "BTCUSDT"
    assert compact["kill_candidates"][0]["key"] == "LONG"

def test_default_dashboard_tracks_all_core_agent_heartbeats():
    expected = {
        "agent_process_supervisor",
        "market_observer",
        "news_observer",
        "reflection_agent",
        "dream_cycle",
        "cognitive_supervisor",
        "llm_reasoning_agent",
        "self_model",
        "self_improvement_agent",
        "daily_exam_agent",
        "counterfactual_replay_agent",
        "shadow_trade_evaluator_loop",
        "promotion_evaluator_loop",
    }

    assert expected.issubset(set(dash.HEARTBEAT_FILES))

def test_dashboard_daily_exam_heartbeat_limit_matches_scheduler(tmp_path: Path, monkeypatch):
    heartbeat = tmp_path / "daily_exam_agent_heartbeat.json"
    heartbeat.write_text(
        json.dumps({"ts": "2026-06-21T17:31:17+00:00", "pid": 46928, "status": "ok", "waiting_for": "next_midnight"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(dash, "pid_running", lambda pid: True)
    monkeypatch.setattr(dash, "age_seconds", lambda ts: 4.7 * 60)

    status = dash.heartbeat_status("daily_exam_agent", heartbeat)

    assert status["state"] == "ok"
    assert dash.HEARTBEAT_FRESH_LIMITS["daily_exam_agent"] == 900

def test_heartbeat_status_marks_dead_process_even_when_heartbeat_is_fresh(tmp_path: Path, monkeypatch):
    heartbeat = tmp_path / "agent_heartbeat.json"
    write_json(heartbeat, {"ts": dash.utc_now(), "pid": 123, "status": "ok"})
    monkeypatch.setattr(dash, "pid_running", lambda pid: False)

    status = dash.heartbeat_status("market_observer", heartbeat)

    assert status["state"] == "dead"
    assert status["running"] is False

def test_heartbeat_status_falls_back_to_pid_file_after_restart(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    heartbeat = state / "llm_reasoning_agent_heartbeat.json"
    write_json(heartbeat, {"ts": dash.utc_now(), "pid": 111, "status": "waiting"})
    (state / "llm_reasoning_agent.pid").write_text("222", encoding="ascii")
    monkeypatch.setattr(dash, "STATE_DIR", state)
    monkeypatch.setattr(dash, "pid_running", lambda pid: int(pid) == 222)

    status = dash.heartbeat_status("llm_reasoning_agent", heartbeat)

    assert status["state"] == "ok"
    assert status["pid"] == 222
    assert status["heartbeat_pid"] == 111

def test_collapse_process_rows_prefers_actual_child_process():
    rows = [
        {"ProcessId": 10, "ParentProcessId": 1, "CommandLine": "python unified_monitor.py", "ExecutablePath": "venv/python.exe"},
        {"ProcessId": 11, "ParentProcessId": 10, "CommandLine": "python unified_monitor.py", "ExecutablePath": "C:/Python/python.exe"},
    ]

    collapsed = dash.collapse_process_rows(rows)

    assert collapsed == [{"pid": 11, "parent_pid": 10, "command": "python unified_monitor.py", "executable": "C:/Python/python.exe"}]

def test_live_monitor_status_is_read_only_external_monitor(monkeypatch):
    monkeypatch.setattr(
        dash,
        "script_processes",
        lambda script: [{"pid": 11, "parent_pid": 10, "command": "python unified_monitor.py", "executable": "python.exe"}],
    )

    monitors = dash.live_monitor_status()

    assert monitors[0]["name"] == "unified_monitor"
    assert monitors[0]["state"] == "ok"
    assert monitors[0]["pids"] == [11]
    assert monitors[0]["agent_controls"] is False
    assert monitors[0]["can_submit_reduce_only_orders"] is True


def test_load_dashboard_status_from_fake_state(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    memory = state / "agent_memory"
    monkeypatch.setattr(dash, "STATE_DIR", state)
    monkeypatch.setattr(dash, "MEMORY_DIR", memory)
    monkeypatch.setattr(
        dash,
        "HEARTBEAT_FILES",
        {"market_observer": state / "market_observer_heartbeat.json"},
    )
    monkeypatch.setattr(
        dash,
        "LOG_FILES",
        {
            "scalp_autotrader": state / "scalp_autotrader.jsonl",
            "scalp_watchdog": state / "scalp_watchdog.jsonl",
        },
    )
    monkeypatch.setattr(dash, "pid_running", lambda pid: True)
    monkeypatch.setattr(dash, "live_monitor_status", lambda: [{"name": "unified_monitor", "state": "ok", "pids": [11], "agent_controls": False}])

    write_json(memory / "execution_bias.json", {"risk_posture": "defensive", "min_signal_score": 8, "market_learning": {"regime": "risk_on", "tags": ["risk_on"]}})
    write_json(memory / "market_model.json", {"last_market_state": {"primary_regime": "risk_on", "tags": ["risk_on"]}})
    write_json(memory / "dream_cycle_latest.json", {"ts": "now", "bias_patch": {"high_risk_count": 3, "paper_candidates": []}, "cycle": {"blocks": []}})
    write_json(memory / "news_latest.json", {"ts": "now", "macro_risk_score": 0.1, "source_health": [{"source": "fake", "status": "ok", "count": 1}]})
    write_json(memory / "shadow_performance_latest.json", {"overall": {"closed": 2, "wins": 1, "losses": 1, "win_rate": 0.5, "net": 0.02}, "data_quality": {"confidence": "low"}})
    write_json(memory / "self_improvement_latest.json", {"ts": "now", "overall_learning_score": 0.42, "readiness": "not_ready", "blindspots": [{"type": "negative_shadow_edge"}], "learning_curriculum": [{"task": "Freeze promotion"}], "guardrail_proposal": {"can_loosen": False, "can_trade_live": False}})
    write_json(memory / "daily_exam_latest.json", {"ts": "now", "local_date": "2026-06-21", "exam_type": "risk_gate_review", "quality_score": 62.5, "quality_grade": "D", "exam_score": 85, "passed": True, "rubric": {"scores": {"risk_discipline": {"score": 0.9}, "performance_improvement": {"score": 0.25}}}, "contract": {"paper_only": True}})
    write_json(memory / "llm_reasoning_latest.json", {"ts": "now", "status": "ok", "provider": {"provider": "9router", "deep_model": "gpt-5.5", "quick_model": "gpt-5.5"}, "reasoning": {"summary": "gom thêm mẫu", "critical_blindspots": ["negative_shadow_edge"], "risk_proposal": {"can_place_live_orders": False, "can_loosen_risk": False}}})
    write_json(memory / "promotion_board_latest.json", {"evaluated_at": "now", "state": "paper_learning", "passed": False, "failures": ["insufficient_paper_trades"], "requirements": {"paper_trades": 300}, "metrics": {"paper_trades": 2}, "can_place_live_orders": False})
    write_json(memory / "walk_forward_latest.json", {"updated_at": "now", "experiment_count": 1, "by_status": {"running": 1}, "rows": [{"patch_id": "p1", "setup_id": "fade", "status": "running", "test_metrics": {"trades": 3, "expectancy_after_fees": 0.01, "profit_factor": 1.2}, "errors": ["insufficient_future_trades"]}], "can_place_live_orders": False})
    write_json(memory / "model_usage_latest.json", {"call_count": 3, "input_tokens_est": 100, "output_tokens_est": 50, "cost_usd_est": 0, "can_place_live_orders": False})
    write_json(state / "paper_account.json", {"equity": "100.0", "starting_equity": "100.0", "open_positions": [{"symbol": "BTCUSDT", "margin": "5", "notional": "50"}]})
    write_json(state / "market_observer_heartbeat.json", {"ts": dash.utc_now(), "pid": 123, "status": "ok"})
    (state / "scalp_autotrader.jsonl").write_text('{"event":"paper_close","net":"0.1"}\n', encoding="utf-8")

    status = dash.load_dashboard_status()

    assert status["overview"]["risk_posture"] == "defensive"
    assert status["overview"]["regime"] == "risk_on"
    assert status["paper"]["closes"] == 1
    assert status["paper"]["account"]["equity"] == "100.0"
    assert status["paper"]["account_summary"]["open_position_count"] == 1
    assert status["paper"]["account_summary"]["open_margin"] == 5.0
    assert status["paper_report"]["closed_trades"] == 1
    assert status["paper_report"]["curve"][-1]["equity"] == 100.1
    assert status["heartbeats"][0]["state"] == "ok"
    assert status["news"]["macro_risk_score"] == 0.1
    assert status["live_monitors"][0]["name"] == "unified_monitor"
    assert status["shadow_performance"]["closed"] == 2
    assert status["shadow_performance"]["data_quality"]["confidence"] == "low"
    assert status["self_improvement"]["overall_learning_score"] == 0.42
    assert status["self_improvement"]["readiness"] == "not_ready"
    assert status["daily_exam"]["exam_type"] == "risk_gate_review"
    assert status["daily_exam"]["quality_score"] == 62.5
    assert status["daily_exam"]["passed"] is True
    assert status["daily_exam"]["score_snapshot"]["performance_improvement"] == 0.25
    assert status["llm_reasoning"]["status"] == "ok"
    assert status["llm_reasoning"]["provider"] == "9router"
    assert status["llm_reasoning"]["deep_model"] == "gpt-5.5"
    assert status["llm_reasoning"]["can_place_live_orders"] is False
    assert status["ops"]["promotion"]["state"] == "paper_learning"
    assert status["ops"]["promotion"]["can_place_live_orders"] is False
    assert status["ops"]["experiments"]["experiment_count"] == 1
    assert status["ops"]["experiments"]["running"] == 1
    assert status["ops"]["experiments"]["can_place_live_orders"] is False
    assert status["ops"]["model_usage"]["call_count"] == 3


def test_html_is_single_page_dashboard():
    assert "Bảng điều khiển Trading Agent" in dash.HTML
    assert "/api/status" in dash.HTML
    assert "view-news" in dash.HTML
    assert "view-report" in dash.HTML
    assert "Báo cáo" in dash.HTML
    assert "Báo cáo trader mô phỏng" in dash.HTML
    assert "equityChart" in dash.HTML
    assert dash.HTML.count("function equityChart(report)") == 1
    assert "tradeBars" in dash.HTML
    assert "data-tip" in dash.HTML
    assert "charttip" in dash.HTML
    assert "charthit-band" in dash.HTML
    assert "chartprobe" in dash.HTML
    assert "chartcursor" in dash.HTML
    assert "barcursor" in dash.HTML
    assert "charttime" in dash.HTML
    assert "shortTs" in dash.HTML
    assert "probe-active" in dash.HTML
    assert "Lệnh equity #" in dash.HTML
    assert "Lệnh lời" in dash.HTML
    assert "Ngày lời" in dash.HTML
    assert "Bucket PnL" in dash.HTML
    assert "ensureChartTooltip" in dash.HTML
    assert "nearestByX" in dash.HTML
    assert "tooltipTargetAtCursor" in dash.HTML
    assert "subtabs" in dash.HTML
    assert "Paper & học" in dash.HTML
    assert "lệnh đóng" in dash.HTML
    assert "lệnh đang mở" in dash.HTML
    assert "Lệnh đang mở" in dash.HTML
    assert "Vị thế paper đang mở" in dash.HTML
    assert "paperRuntimeLabel" in dash.HTML
    assert "Curriculum" in dash.HTML
    assert "External Live Monitors" in dash.HTML
    assert "Shadow Performance" in dash.HTML
    assert "Shadow / would-trade only" in dash.HTML
    assert "Shadow fresh" in dash.HTML
    assert "Self Improvement" in dash.HTML
    assert "Starting equity" in dash.HTML
    assert "Heartbeat" in dash.HTML
    assert "Promotion board" in dash.HTML
    assert "Ops học máy" in dash.HTML
    assert "Post-trade review" in dash.HTML
    assert "Review lệnh" in dash.HTML
    assert "renderPostTradeLearning" in dash.HTML
    assert "Counterfactual attach" in dash.HTML
    assert "Counterfactual replay" in dash.HTML
    assert "renderCounterfactualLearning" in dash.HTML
    assert "Walk-forward validation" in dash.HTML
    assert "renderWalkForwardLearning" in dash.HTML
    assert "Bằng chứng cải thiện" in dash.HTML
    assert "renderImprovementProof" in dash.HTML
    assert "window.open" not in dash.HTML
