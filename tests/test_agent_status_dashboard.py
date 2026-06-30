import json
import re
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import agent_status_dashboard as dash
import event_store


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
        {"event": "paper_close", "trade_id": "t1", "close_ts": "2026-06-21T00:01:00+00:00", "symbol": "BTCUSDT", "side": "LONG", "net": "1.5", "funding_payment": "-0.01", "position": {"notional": "15"}},
        {"event": "paper_close", "trade_id": "t2", "close_ts": "2026-06-21T00:02:00+00:00", "symbol": "ETHUSDT", "side": "SHORT", "net": "-0.5", "funding_payment": "0.02", "position": {"notional": "5"}},
        {"event": "paper_close", "trade_id": "t2", "close_ts": "2026-06-21T00:02:00+00:00", "symbol": "ETHUSDT", "side": "SHORT", "net": "-0.5", "funding_payment": "0.02", "position": {"notional": "5"}},
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
    assert report["total_funding_payment"] == 0.01
    assert report["avg_notional"] == 10.0
    assert "best_trade" in report
    assert "worst_trade" in report

def test_summarize_paper_report_does_not_emit_pf_sentinel():
    rows = [
        {"event": "paper_close", "trade_id": "t1", "close_ts": "2026-06-21T00:01:00+00:00", "symbol": "BTCUSDT", "side": "LONG", "net": "1.5", "position": {"notional": "15"}},
        {"event": "paper_close", "trade_id": "t2", "close_ts": "2026-06-21T00:02:00+00:00", "symbol": "BTCUSDT", "side": "LONG", "net": "0.5", "position": {"notional": "5"}},
    ]

    report = dash.summarize_paper_report(rows, {"starting_equity": "100", "equity": "102"})

    assert report["profit_factor"] is None
    assert report["breakdown"]["by_symbol"][0]["profit_factor"] is None

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
        {"name": "learning_exam_benchmark", "state": "ok", "running": True},
        {"name": "test_result_memory_agent", "state": "ok", "running": True},
        {"name": "shadow_trade_evaluator_loop", "state": "ok", "running": True},
        {"name": "promotion_evaluator_loop", "state": "ok", "running": True},
    ]

    runtime = dash.paper_runtime_status(heartbeats)

    assert runtime["state"] == "degraded"
    assert "counterfactual_replay_agent" in runtime["tracked"]
    assert "learning_exam_benchmark" in runtime["tracked"]
    assert "test_result_memory_agent" in runtime["tracked"]
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
        "learning_exam_benchmark",
        "test_result_memory_agent",
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
    assert monitors[0]["can_submit_reduce_only_orders"] is False


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
    write_json(memory / "learning_exam_benchmark_latest.json", {"score": 1.0, "scenario_count": 5, "failed_count": 0, "can_place_live_orders": False})
    write_json(memory / "test_result_memory_latest.json", {"lesson_count": 2, "high_severity_count": 1, "known_gaps": ["counterfactual_coverage_low"], "can_place_live_orders": False})
    write_json(state / "paper_account.json", {"created_at": "2026-06-21T00:00:00+00:00", "equity": "100.0", "starting_equity": "100.0", "open_positions": [{"symbol": "BTCUSDT", "margin": "5", "notional": "50"}]})
    write_json(state / "market_observer_heartbeat.json", {"ts": dash.utc_now(), "pid": 123, "status": "ok"})
    (state / "scalp_autotrader.jsonl").write_text(
        "\n".join(
            [
                '{"event":"paper_close","ts":"2026-06-20T23:59:00+00:00","net":"-9.9"}',
                '{"event":"paper_close","ts":"2026-06-21T00:01:00+00:00","net":"0.1"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    status = dash.load_dashboard_status()

    assert status["overview"]["risk_posture"] == "defensive"
    assert status["overview"]["regime"] == "risk_on"
    assert status["paper"]["closes"] == 1
    assert status["paper"]["net"] == 0.1
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
    assert status["ops"]["learning_benchmark"]["scenario_count"] == 5
    assert status["ops"]["test_result_memory"]["lesson_count"] == 2


def test_dashboard_paper_summary_reads_full_reset_window(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    memory = state / "agent_memory"
    monkeypatch.setattr(dash, "STATE_DIR", state)
    monkeypatch.setattr(dash, "MEMORY_DIR", memory)
    monkeypatch.setattr(dash, "HEARTBEAT_FILES", {})
    monkeypatch.setattr(
        dash,
        "LOG_FILES",
        {
            "scalp_autotrader": state / "scalp_autotrader.jsonl",
            "paper_lifecycle": memory / "paper_trades.jsonl",
            "scalp_watchdog": state / "scalp_watchdog.jsonl",
        },
    )
    monkeypatch.setattr(dash, "pid_running", lambda pid: True)
    monkeypatch.setattr(dash, "live_monitor_status", lambda: [])

    write_json(
        state / "paper_account.json",
        {
            "created_at": "2026-06-21T00:00:00+00:00",
            "starting_equity": "100",
            "equity": "100.25",
            "realized_pnl": "0.25",
            "closed_trades": 1,
            "wins": 1,
            "losses": 0,
            "open_positions": [],
        },
    )
    rows = ['{"event":"paper_close","ts":"2026-06-21T00:01:00+00:00","net":"0.25"}']
    rows.extend(
        '{"event":"heartbeat","ts":"2026-06-21T00:%02d:00+00:00"}' % (i % 60)
        for i in range(650)
    )
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "paper_trades.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")

    status = dash.load_dashboard_status()

    assert status["paper"]["closes"] == 1
    assert status["paper"]["net"] == 0.25
    assert status["paper_report"]["closed_trades"] == 1
    assert status["paper_report"]["account_alignment"]["closed_trade_count_delta"] == 0


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
    assert "{id:'summary',label:'Tổng hợp',count:4}" in dash.HTML
    assert "{id:'charts',label:'Biểu đồ',count:4}" in dash.HTML
    assert "Paper & học" in dash.HTML
    assert "lệnh đóng" in dash.HTML
    assert "Lệnh đang mở" in dash.HTML
    assert "Audit số liệu" in dash.HTML
    assert "PnL đã chốt" in dash.HTML
    assert "PnL equity" in dash.HTML
    assert "funding" in dash.HTML
    assert "Vị thế paper đang mở" in dash.HTML
    assert "paperRuntimeLabel" in dash.HTML

def test_dashboard_inline_javascript_is_syntax_valid(tmp_path: Path):
    scripts = re.findall(r"<script>([\s\S]*?)</script>", dash.HTML)
    assert scripts
    for idx, script in enumerate(scripts):
        script_path = tmp_path / f"dashboard_inline_{idx}.js"
        script_path.write_text(script, encoding="utf-8")
        result = subprocess.run(
            ["node", "--check", str(script_path)],
            capture_output=True,
            encoding="utf-8",
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
    assert "Curriculum" in dash.HTML
    assert "External Live Monitors" in dash.HTML
    assert "Shadow Performance" in dash.HTML
    assert "Shadow / would-trade only" in dash.HTML
    assert "Shadow fresh" in dash.HTML
    assert "Self Improvement" in dash.HTML
    assert "Starting equity" in dash.HTML
    assert "Heartbeat" in dash.HTML
    assert "Supervisor paper" in dash.HTML
    assert "Live watchdog cũ" in dash.HTML
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
    assert "showDashboardError" in dash.HTML
    assert "unhandledrejection" in dash.HTML
    assert "window.open" not in dash.HTML

def test_neurocore_payload_handles_missing_files(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    memory = state / "agent_memory"
    monkeypatch.setattr(dash, "STATE_DIR", state)
    monkeypatch.setattr(dash, "MEMORY_DIR", memory)
    monkeypatch.setattr(dash, "FEATURE_STORE_DIR", state / "feature_store")
    monkeypatch.setattr(dash, "EVENT_STORE_DB", state / "agent_state.db")
    monkeypatch.setattr(dash, "HEARTBEAT_FILES", {})
    monkeypatch.setattr(
        dash,
        "LOG_FILES",
        {
            "scalp_autotrader": state / "scalp_autotrader.jsonl",
            "paper_lifecycle": memory / "paper_trades.jsonl",
            "scalp_watchdog": state / "scalp_watchdog.jsonl",
        },
    )
    monkeypatch.setattr(dash, "live_monitor_status", lambda: [])
    monkeypatch.setattr(dash, "pid_running", lambda pid: True)
    write_json(state / "paper_account.json", {"created_at": "2026-06-21T00:00:00+00:00", "starting_equity": "100", "equity": "100", "open_positions": []})

    status = dash.load_dashboard_status()

    assert status["neurocore"]["schema_version"] == "neurocore_dashboard.v1"
    assert status["neurocore"]["paper_only"] is True
    assert status["neurocore"]["live_eligible"] is False
    assert status["neurocore"]["event_bus"]["state"] == "missing"
    assert status["neurocore"]["scoring"]["state"] == "missing"
    assert status["neurocore"]["scoring"]["as_of"] is None
    assert status["neurocore"]["features"]["freshness"]["path"] == str(state / "feature_store")
    assert status["neurocore"]["features"]["freshness"]["state"] == "missing"
    assert "mandatory_tooltip_fields" in status["neurocore"]["chart_contract"]
    assert "cost_vector" in status["neurocore"]["chart_contract"]["mandatory_tooltip_fields"]
    for window in status["neurocore"]["scoring"]["windows"]:
        assert "setup_contract_hash" in window
        assert "ci_lower_bound" in window
        assert "cost_vector" in window
    assert status["dashboard_contract"]["payload_budget_bytes"] == dash.DASHBOARD_PAYLOAD_BUDGET_BYTES

def test_feature_health_uses_decision_capability_mask_and_source_watermark(tmp_path: Path, monkeypatch):
    store = tmp_path / "feature_store"
    monkeypatch.setattr(dash, "FEATURE_STORE_DIR", store)
    write_json(
        store / "BTCUSDT_1m.json",
        {
            "feature_id": "btc_1m",
            "symbol": "BTCUSDT",
            "timeframe": "1m",
            "computed_at": "2020-01-01T00:00:00+00:00",
            "decision_data_capability_mask": {
                "action": "allow",
                "source_confidence": 0.83,
                "missing_required": [],
                "missing_optional": ["liquidations"],
            },
            "source_trust": {"checked_at": "2020-01-01T00:00:00+00:00"},
        },
    )

    status = dash.feature_health_dashboard()

    assert status["state"] == "ok"
    assert status["missing_or_degraded_rate"] == 0.0
    assert status["avg_source_trust"] == 0.83
    assert status["latest_rows"][0]["action"] == "allow"
    assert status["freshness"]["state"] == "stale"
    assert status["freshness"]["source_watermark"] == "2020-01-01T00:00:00+00:00"

def test_scoring_dashboard_masks_pf_sentinel_from_api(tmp_path: Path, monkeypatch):
    memory = tmp_path / "agent_memory"
    monkeypatch.setattr(dash, "MEMORY_DIR", memory)
    write_json(
        memory / "real_scoring_board_latest.json",
        {
            "passed": True,
            "snapshot_id": "score_1",
            "as_of": "2026-06-30T00:00:00+00:00",
            "overall": {
                "trades": 2,
                "profit_factor_after_costs": 999.0,
                "expectancy_after_costs": 0.1,
                "win_rate": 1.0,
                "cost_completeness": True,
            },
        },
    )

    status = dash.scoring_dashboard({"closed_trades": 2, "profit_factor": 999.0, "expectancy": 0.1, "rolling": {}}, {})

    overall = [row for row in status["windows"] if row["window"] == "real_scoring_overall"][0]
    assert overall["profit_factor"] is None
    assert overall["expectancy"] == 0.1

def test_compact_experiments_masks_nested_pf_sentinel():
    status = dash.compact_experiments(
        {
            "by_status": {"passed": 1},
            "rows": [
                {
                    "experiment_id": "e1",
                    "test_metrics": {"profit_factor": 999.0, "profit_factor_after_costs": 999.0, "pf": 999.0},
                }
            ],
        }
    )

    metrics = status["rows"][0]["test_metrics"]
    assert metrics["profit_factor"] is None
    assert metrics["profit_factor_after_costs"] is None
    assert metrics["pf"] is None

def test_payload_budget_emergency_floor_stays_under_budget():
    payload = {
        "now": "2026-06-30T00:00:00+00:00",
        "overview": {"huge": "x" * 80_000},
        "process": {"huge": "x" * 80_000},
        "paper": {"latest_events": [{"huge": "x" * 80_000}]},
        "paper_report": {"recent_closes": [{"huge": "x" * 80_000}], "curve": [{"huge": "x" * 80_000}]},
        "neurocore": {"schema_version": "neurocore_dashboard.v1", "snapshot_id": "n1", "top_blockers": [{"detail": "x" * 80_000}]},
        "dashboard_contract": {"build_id": dash.DASHBOARD_BUILD_ID},
    }

    result = dash.enforce_dashboard_payload_budget(payload, budget_bytes=20_000)

    assert result["dashboard_contract"]["budget_trimmed_emergency"] is True
    assert result["dashboard_contract"]["estimated_payload_bytes"] <= 20_000

def test_neurocore_event_bus_dashboard_reads_offsets(tmp_path: Path):
    db = tmp_path / "bus.db"
    result = event_store.append_event_envelope(
        "candidate.generated",
        {"candidate_id": "c1", "symbol": "BTCUSDT", "side": "LONG"},
        "paper_candidate_feeder",
        "paper_candidate_feeder",
        "c1",
        db_path=db,
    )
    assert result["ok"] is True
    event_store.create_subscription("dashboard_test", ["candidate.generated"], db_path=db)

    bus = dash.event_bus_dashboard(db)

    assert bus["state"] == "ok"
    assert bus["total_events"] == 1
    assert bus["latest_seq"] == 1
    assert bus["subscriptions"][0]["consumer_id"] == "dashboard_test"
    assert bus["subscriptions"][0]["lag_events"] == 1

def test_dashboard_bind_policy_blocks_nonlocal_without_token():
    denied = dash.validate_dashboard_bind("0.0.0.0", "")
    weak = dash.validate_dashboard_bind("0.0.0.0", "short")
    allowed = dash.validate_dashboard_bind("0.0.0.0", "A9x!B8y@C7z#D6w$E5v%F4u^")
    hex_allowed = dash.validate_dashboard_bind("0.0.0.0", "a" * 64)

    assert denied["ok"] is False
    assert denied["errors"] == ["non_local_bind_requires_dashboard_token"]
    assert weak["ok"] is False
    assert "dashboard_token_too_weak" in weak["errors"]
    assert allowed["ok"] is True
    assert hex_allowed["ok"] is True
    assert allowed["token_required"] is True

def test_dashboard_handler_requires_header_token_and_rejects_query_token(monkeypatch):
    server = dash.ReusableThreadingHTTPServer(("127.0.0.1", 0), dash.DashboardHandler)
    server.dashboard_bind_host = "0.0.0.0"
    server.dashboard_token = "secret-token"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/api/status"
    monkeypatch.setattr(dash, "load_dashboard_status", lambda: {"ok": True})
    try:
        with pytest_raises_http(403):
            urllib.request.urlopen(url + "?token=secret-token", timeout=5)
        with pytest_raises_http(403):
            urllib.request.urlopen(url + "?Token=secret-token", timeout=5)
        with pytest_raises_http(403):
            urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/?token=secret-token", timeout=5)
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/", timeout=5) as response:
            assert response.status == 200
            assert b"dashboardHeaders" in response.read()
        req = urllib.request.Request(url, headers={"X-Dashboard-Token": "secret-token"})
        with urllib.request.urlopen(req, timeout=5) as response:
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["X-Dashboard-Build-Id"] == dash.DASHBOARD_BUILD_ID
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

def test_dashboard_handler_gates_log_and_drilldown_with_header_token(monkeypatch, tmp_path: Path):
    server = dash.ReusableThreadingHTTPServer(("127.0.0.1", 0), dash.DashboardHandler)
    server.dashboard_bind_host = "0.0.0.0"
    server.dashboard_token = "secret-token"
    log_path = tmp_path / "dashboard.log"
    log_path.write_text('{"api_key":"sk-testsecretvalue123456789","ok":true}\nnot-json sk-testsecretvalue123456789\n', encoding="utf-8")
    monkeypatch.setattr(dash, "LOG_FILES", {"safe": log_path})
    monkeypatch.setattr(dash, "explain_decision", lambda identifier: {"api_key": "sk-testsecretvalue123456789", "id": identifier})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with pytest_raises_http(403):
            urllib.request.urlopen(base + "/api/drilldown?id=x", timeout=5)
        with pytest_raises_http(403):
            urllib.request.urlopen(base + "/api/log?name=safe&lines=not-int", timeout=5)
        drill_req = urllib.request.Request(base + "/api/drilldown?id=x", headers={"X-Dashboard-Token": "secret-token"})
        with urllib.request.urlopen(drill_req, timeout=5) as response:
            text = response.read().decode("utf-8")
            assert "[REDACTED_SECRET]" in text
            assert "sk-testsecret" not in text
        log_req = urllib.request.Request(base + "/api/log?name=safe&lines=not-int", headers={"X-Dashboard-Token": "secret-token"})
        with urllib.request.urlopen(log_req, timeout=5) as response:
            text = response.read().decode("utf-8")
            assert response.status == 200
            assert "[REDACTED_SECRET]" in text
            assert "sk-testsecret" not in text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

def test_dashboard_handler_blocks_host_rebinding_without_token():
    server = dash.ReusableThreadingHTTPServer(("127.0.0.1", 0), dash.DashboardHandler)
    server.dashboard_bind_host = "127.0.0.1"
    server.dashboard_token = ""
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/api/status", headers={"Host": "evil.example"})
        with pytest_raises_http(403):
            urllib.request.urlopen(req, timeout=5)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

class pytest_raises_http:
    def __init__(self, status: int):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        assert isinstance(exc, urllib.error.HTTPError)
        assert exc.code == self.status
        return True

def test_dashboard_drilldown_and_log_endpoints_are_sanitized(monkeypatch):
    server = dash.ReusableThreadingHTTPServer(("127.0.0.1", 0), dash.DashboardHandler)
    server.dashboard_bind_host = "127.0.0.1"
    server.dashboard_token = ""
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setattr(dash, "explain_decision", lambda identifier: {"api_key": "sk-testsecretvalue123456789", "id": identifier})
    try:
        with urllib.request.urlopen(base + "/api/drilldown?id=x", timeout=5) as response:
            text = response.read().decode("utf-8")
            assert "[REDACTED_SECRET]" in text
            assert "sk-testsecret" not in text
        with urllib.request.urlopen(base + "/api/log?name=unknown&lines=not-int", timeout=5) as response:
            assert response.status == 404
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

def test_neurocore_html_contracts_are_present():
    assert "data-view=\"neurocore\"" in dash.HTML
    assert "view-neurocore" in dash.HTML
    assert "renderNeurocore" in dash.HTML
    assert "Sơ đồ hệ thần kinh" in dash.HTML
    assert "NeuroCore scoring windows" in dash.HTML
    assert "Readiness eligibility" in dash.HTML
    assert "Cost vector" in dash.HTML
    assert "promotion_ineligible" in dash.HTML
    assert "dashboardHeaders" in dash.HTML
    assert "X-Dashboard-Token" in dash.HTML
    assert "new URLSearchParams(location.search).get('token')" not in dash.HTML
    assert "?token=" not in dash.HTML
    assert "authQuery" not in dash.HTML
    assert "focusin" in dash.HTML
    assert "touchstart" in dash.HTML
    assert "aria-describedby" in dash.HTML
    assert "role','tabpanel" in dash.HTML

def test_dashboard_payload_budget_is_enforced():
    payload = {
        "dashboard_contract": {},
        "paper": {"latest_events": [{"x": "y" * 1000} for _ in range(50)]},
        "paper_report": {"recent_closes": [{"x": "z" * 1000} for _ in range(50)], "curve": [{"x": "c" * 1000} for _ in range(200)]},
        "phase_b_learning": {"recent_reviews": [{"x": "r" * 1000} for _ in range(20)], "recent_replays": [{"x": "p" * 1000} for _ in range(20)]},
        "ops": {"huge": "o" * 100000},
    }

    result = dash.enforce_dashboard_payload_budget(payload, budget_bytes=20_000)

    assert result["dashboard_contract"]["estimated_payload_bytes"] <= 20_000
    assert len(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True).encode("utf-8")) <= 20_000
    assert result["dashboard_contract"]["budget_trimmed"] is True

def test_dashboard_budget_accounts_for_added_contract_fields_at_default_budget():
    payload = {"dashboard_contract": {}, "overview": {"huge": "x" * 449_000}}

    result = dash.enforce_dashboard_payload_budget(payload)
    actual = len(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True).encode("utf-8"))

    assert actual <= dash.DASHBOARD_PAYLOAD_BUDGET_BYTES
    assert result["dashboard_contract"]["estimated_payload_bytes"] == actual

def test_dashboard_masks_live_string_modes():
    result = dash.sanitize_dashboard_payload(
        {
            "bias": {"live_mode": "live", "mode": "production", "nested_mode": "live"},
            "account_scope": "mainnet",
            "can_place_live_orders": True,
            "Can_Place_Live_Orders": True,
            "Live_Permission": True,
            "status": "LIVE",
            "ready_for_live": True,
            "readiness": "ready_for_live",
            "permission": True,
            "errors": ["LIVE"],
            "warnings": ["mainnet"],
            "nested": {"status": "mainnet", "readyForLive": True, "permission": True},
        }
    )

    assert result["bias"]["live_mode"] == "paper"
    assert result["bias"]["mode"] == "paper"
    assert result["bias"]["nested_mode"] == "paper"
    assert result["account_scope"] == "paper"
    assert result["can_place_live_orders"] is False
    assert result["Can_Place_Live_Orders"] is False
    assert result["Live_Permission"] is False
    assert result["status"] == "paper"
    assert result["ready_for_live"] is False
    assert result["readiness"] == "paper_only"
    assert result["permission"] is False
    assert result["errors"] == ["paper"]
    assert result["warnings"] == ["paper"]
    assert result["nested"]["status"] == "paper"
    assert result["nested"]["readyForLive"] is False
    assert result["nested"]["permission"] is False

def test_compact_live_readiness_cannot_surface_ready_for_live():
    result = dash.compact_live_readiness(
        {
            "passed": True,
            "readiness": "ready_for_live",
            "ready_for_live": True,
            "readyForLive": True,
            "live_readiness": {"nested": {"reason": "ready_for_live"}},
            "nested": {"readyForLive": True, "reason": "ready_for_live"},
            "reason": "ready_for_live",
            "errors": ["LIVE"],
            "permission": True,
            "canPlaceLiveOrders": True,
        }
    )
    serialized = json.dumps(result, sort_keys=True)

    assert result["passed"] is False
    assert result["mode"] == "paper"
    assert result["status"] == "paper_only"
    assert result["reason"] == "dashboard_hard_mask_paper_only"
    assert result["canPlaceLiveOrders"] is False
    assert result["permission"] is False
    assert result["errors"] == ["paper"]
    assert "readiness" not in result
    assert "ready_for_live" not in serialized
    assert "readyForLive" not in serialized

def test_dashboard_payload_budget_emergency_trims_kept_sections():
    payload = {
        "dashboard_contract": {},
        "overview": {"mode": "paper"},
        "paper": {"latest_events": [], "huge": "p" * 80_000},
        "paper_report": {"curve": [], "recent_closes": [], "huge": "r" * 80_000},
        "process": {"huge": "x" * 80_000},
        "neurocore": {"schema_version": "neurocore_dashboard.v1", "state": "critical", "top_blockers": [{"detail": "b" * 1000}], "huge": "n" * 100_000},
    }

    result = dash.enforce_dashboard_payload_budget(payload, budget_bytes=20_000)

    assert result["dashboard_contract"]["estimated_payload_bytes"] <= 20_000
    assert result["dashboard_contract"].get("budget_trimmed_emergency") is True
    assert result["neurocore"]["live_eligible"] is False
