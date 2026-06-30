from pathlib import Path

import agent_work_queue as awq
import autonomous_paper_trading_brain as brain
import autonomous_paper_trading_loop as loop
import inner_critic
import dont_do_memory as ddm
import llm_output_quality_gate as lqg
import paper_execution_simulator as pes
import paper_portfolio_manager as ppm
import skill_forge_agent as sfa
import instrument_registry as registry
import microstructure_observer_loop as micro_loop
import alert_manager as alerts
import backup_restore as br
import daily_exam_agent as dea
import llm_council
import model_usage_ledger as mul
import security_import_guard as sig
import preflight_guard as pfg

def test_claim_next_of_types_ignores_unrelated_jobs(tmp_path: Path):
    db = tmp_path / "jobs.sqlite"
    awq.enqueue_job("daily_exam_task", {"x": 1}, priority=99, db_path=db)
    awq.enqueue_job("setup_review", {"candidate": {"symbol": "BTCUSDT", "side": "LONG", "setup_id": "s"}}, priority=1, db_path=db)

    job = awq.claim_next_of_types("worker", ["setup_review"], db_path=db)

    assert job is not None
    assert job["job_type"] == "setup_review"

def test_autonomous_paper_loop_once_is_paper_only(monkeypatch, tmp_path: Path):
    memory = tmp_path / "agent_memory"
    memory.mkdir()
    monkeypatch.setattr(loop, "MEMORY_DIR", memory)
    monkeypatch.setattr(loop, "LATEST_PATH", memory / "autonomous_paper_trading_loop_latest.json")
    monkeypatch.setattr(loop, "HISTORY_PATH", memory / "autonomous_paper_trading_loop_history.jsonl")
    monkeypatch.setattr(loop, "HEARTBEAT_PATH", tmp_path / "autonomous_paper_trading_loop_heartbeat.json")
    monkeypatch.setattr(loop, "CANDIDATES_PATH", memory / "paper_candidates_latest.json")
    monkeypatch.setattr(loop, "kill_switch_active", lambda: False)
    monkeypatch.setattr(loop, "evaluate_live_permission", lambda request: {"allowed": True})
    monkeypatch.setattr(loop, "evaluate_circuit_breakers", lambda metrics: {"allowed": True, "action": "allow"})
    monkeypatch.setattr(loop, "load_account", lambda: {"equity": "100", "cash": "100"})
    monkeypatch.setattr(loop, "load_queue_candidate_batch", lambda worker_id: ({}, None))
    monkeypatch.setattr(loop, "decide_paper_action", lambda candidates, setup_stats, account, exploration_allowed=False: {"action": "skip", "can_place_live_orders": False})

    result = loop.run_once()

    assert result["can_place_live_orders"] is False
    assert result["decision"]["can_place_live_orders"] is False

def test_paper_brain_blocks_candidate_below_skill_patch_min_score():
    candidate = {"symbol": "BTCUSDT", "side": "LONG", "setup_id": "s1", "score": 8.0}
    rankings = [{"setup_id": "s1", "paper_only_min_score_adjustment": 1.0}]

    assert brain.paper_only_patch_errors(candidate, rankings) == ["skill_patch_min_score_block"]

def test_paper_brain_does_not_add_invalid_margin_when_allocation_blocks(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(brain, "BRAIN_LATEST", tmp_path / "brain.json")
    monkeypatch.setattr(brain, "BRAIN_HISTORY", tmp_path / "brain.jsonl")
    monkeypatch.setattr(brain, "PAPER_RISK_STATE", tmp_path / "risk.json")
    monkeypatch.setattr(brain, "paper_opens_paused_by_runtime", lambda: {"paused": False, "reason": "ok", "replay_required": False, "promotion_window_valid": True})
    monkeypatch.setattr(brain, "run_preflight", lambda *args, **kwargs: {"allowed": True, "errors": []})
    monkeypatch.setattr(brain, "evaluate_candidate", lambda candidate: {"blocked": False})
    monkeypatch.setattr(brain, "rank_setups", lambda rows: {"rankings": [{"setup_id": "s1", "evidence_expectancy": -0.1, "expectancy": -0.1, "allocation_hint": "reduced", "risk_multiplier": 0.35}]})

    result = brain.decide_paper_action(
        [{"symbol": "BTCUSDT", "side": "LONG", "setup_id": "s1", "entry": 100, "sl": 99, "tp": 102, "score": 9}],
        [{"setup_id": "s1"}],
        {"equity": "100", "cash": "100"},
    )

    assert result["action"] == "skip"
    assert "non_positive_expectancy" in result["errors"]
    assert "invalid_margin" not in result["errors"]
    assert result["risk_decision"]["reason"] == "allocation_blocked"
    assert loop.HEARTBEAT_PATH.exists()

def test_inner_critic_uses_dont_do_shadow_only(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(ddm, "DONT_DO_PATH", tmp_path / "dont_do.json")
    monkeypatch.setattr(inner_critic, "evaluate_dont_do_candidate", lambda signal: {"action": "shadow_only", "blocked": True, "matches": [{"rule_id": "r1"}]})
    monkeypatch.setattr(inner_critic, "safe_append_event", lambda *args, **kwargs: None)
    signal = {"symbol": "BTCUSDT", "side": "LONG", "score": 9, "setup_id": "momentum_continuation"}
    snapshot = {"ts": inner_critic.utc_now()}
    library = {"skills": {"momentum_continuation": {"enabled": True, "stats": {}}}}
    monkeypatch.setattr(inner_critic, "match_setup", lambda *args, **kwargs: [{"setup_id": "momentum_continuation", "confidence": 0.8}])

    verdict = inner_critic.evaluate_signal(signal, bias={"min_signal_score": 6}, snapshot=snapshot, market_model={}, library=library, hypotheses_result={"hypotheses": [{"setup_id": "momentum_continuation", "symbols": ["BTCUSDT"], "prediction": {"side": "LONG"}, "hypothesis_id": "h1"}]}, news_context={})

    assert verdict["verdict"] == "tighten"
    assert "dont_do_memory_shadow_only" in verdict["reasons"]

def test_llm_reasoning_quality_gate_forces_no_live():
    result = lqg.sanitize_output({"summary": "ok", "risk_proposal": {"can_place_live_orders": True, "can_loosen_risk": True}}, kind="llm_reasoning")

    assert result["ok"] is False
    assert result["sanitized"]["can_place_live_orders"] is False
    assert "unsafe_risk_or_live_permission" in result["errors"]

def test_skill_forge_applies_paper_shadow_patch_metadata(monkeypatch, tmp_path: Path):
    pending = tmp_path / "pending.jsonl"
    output = tmp_path / "integration.json"
    applied = tmp_path / "applied.jsonl"
    latest = tmp_path / "latest.json"
    library = {"skills": {"s1": {"setup_id": "s1", "metadata": {}}}, "history": []}
    saved = {}
    monkeypatch.setattr(sfa, "load_library", lambda: library)
    monkeypatch.setattr(sfa, "save_library", lambda payload: saved.setdefault("library", payload) or payload)
    sfa.append_jsonl_once(
        pending,
        {
            "patch_id": "p1",
            "setup_id": "s1",
            "patch_type": "regime_filter",
            "invalidation": "bad spread",
            "rollback_criteria": "future paper expectancy <= 0",
            "status": "paper_shadow_only",
            "lifecycle": ["proposed", "schema_valid", "evidence_checked"],
            "evidence_ids": ["review_1"],
            "evidence": {"sample_size": 30, "evidence_ids": ["review_1"]},
        },
        "patch_id",
    )

    result = sfa.apply_paper_shadow_patches(pending_path=pending, output_path=output, applied_path=applied, latest_path=latest)

    assert result["applied_count"] == 1
    patch = saved["library"]["skills"]["s1"]["metadata"]["paper_shadow_patches"][0]
    assert patch["live_enabled"] is False
    assert patch["status"] == "paper_only_applied"

def test_paper_portfolio_open_and_close_lifecycle(monkeypatch, tmp_path: Path):
    account_path = tmp_path / "paper_account.json"
    monkeypatch.setattr(ppm, "POSITION_HISTORY_PATH", tmp_path / "positions.jsonl")
    account = ppm.default_account()
    risk = ppm.evaluate_paper_order("BTCUSDT", "LONG", "100", "99", "102", requested_margin="5", requested_leverage="2", account=account, config={"mode": "paper_learning", "live_execution_enabled": False, "feature_flags": {"paper_trading": True, "live_orders": False}})

    opened = ppm.open_paper_position(risk, account=account, path=account_path)
    closed = ppm.close_paper_position(opened["position"]["position_id"], "101", fee="0.01", account=opened["account"], path=account_path)

    assert opened["ok"] is True
    assert closed["ok"] is True
    assert closed["account"]["open_positions"] == []
    assert float(closed["account"]["equity"]) > 100

def test_paper_execution_partial_fill_tracks_remaining_qty(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(pes, "PAPER_ORDERS", tmp_path / "orders.jsonl")

    result = pes.simulate_entry_order("BTCUSDT", "LONG", "market", "2", "100", {"ts": "x", "open": 100, "high": 101, "low": 99, "fill_fraction": "0.25"})

    assert result["status"] == "partial"
    assert result["filled_qty"] == "0.5"
    assert result["remaining_qty"] == "1.5"

def test_instrument_registry_refreshes_exchange_info_with_leverage_brackets(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(registry, "QUALITY_PATH", tmp_path / "quality.json")
    exchange_info = {"symbols": [{"symbol": "btcusdt", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING", "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.1"}, {"filterType": "LOT_SIZE", "stepSize": "0.001"}, {"filterType": "MIN_NOTIONAL", "notional": "5"}]}]}
    leverage = [{"symbol": "BTCUSDT", "brackets": [{"initialLeverage": 50}]}]

    payload = registry.refresh_registry_from_exchange_info(exchange_info, leverage_payload=leverage, path=tmp_path / "registry.json")

    btc = payload["instruments"]["BTCUSDT"]
    assert btc["max_leverage"] == "50"
    assert registry.can_trade_paper("BTCUSDT", payload)["can_trade_paper"] is True

def test_microstructure_loop_evaluates_local_sources(monkeypatch, tmp_path: Path):
    memory = tmp_path / "agent_memory"
    memory.mkdir()
    monkeypatch.setattr(micro_loop, "MEMORY_DIR", memory)
    monkeypatch.setattr(micro_loop, "LATEST_PATH", memory / "micro_latest.json")
    monkeypatch.setattr(micro_loop, "HISTORY_PATH", memory / "micro_history.jsonl")
    monkeypatch.setattr(micro_loop, "HEARTBEAT_PATH", tmp_path / "micro_heartbeat.json")
    monkeypatch.setattr(micro_loop, "DERIVATIVES_SOURCE", tmp_path / "derivatives_source_latest.json")
    monkeypatch.setattr(micro_loop, "ORDERBOOK_SOURCE", tmp_path / "orderbook_source_latest.json")
    monkeypatch.setattr(micro_loop, "LIQUIDATIONS_SOURCE", tmp_path / "liquidations_source_latest.json")
    micro_loop.write_json_atomic(micro_loop.DERIVATIVES_SOURCE, {"symbol": "BTCUSDT", "funding_rate": "0.0001", "oi_now": 110, "oi_prev": 100})
    micro_loop.write_json_atomic(micro_loop.ORDERBOOK_SOURCE, {"symbol": "BTCUSDT", "bids": [[100, 10]], "asks": [[100.01, 10]], "max_spread_bps": 8})
    micro_loop.write_json_atomic(micro_loop.LIQUIDATIONS_SOURCE, {"symbol": "BTCUSDT", "events": [{"ts": "x", "side": "LONG", "notional": 2_000_000}]})

    result = micro_loop.run_once()

    assert result["status"] == "ok"
    assert result["result_count"] == 3
    assert result["can_place_live_orders"] is False

def test_microstructure_loop_reads_evaluated_latest_snapshots(monkeypatch, tmp_path: Path):
    memory = tmp_path / "agent_memory"
    memory.mkdir()
    monkeypatch.setattr(micro_loop, "MEMORY_DIR", memory)
    monkeypatch.setattr(micro_loop, "LATEST_PATH", memory / "micro_latest.json")
    monkeypatch.setattr(micro_loop, "HISTORY_PATH", memory / "micro_history.jsonl")
    monkeypatch.setattr(micro_loop, "HEARTBEAT_PATH", tmp_path / "micro_heartbeat.json")
    monkeypatch.setattr(micro_loop, "DERIVATIVES_SOURCE", tmp_path / "derivatives_latest.json")
    monkeypatch.setattr(micro_loop, "ORDERBOOK_SOURCE", tmp_path / "orderbook_microstructure_latest.json")
    monkeypatch.setattr(micro_loop, "LIQUIDATIONS_SOURCE", tmp_path / "liquidations_latest.json")
    micro_loop.write_json_atomic(micro_loop.DERIVATIVES_SOURCE, {"symbol": "BTCUSDT", "status": "ok", "funding_rate": 0.0001, "open_interest_delta": 0.1})
    micro_loop.write_json_atomic(micro_loop.ORDERBOOK_SOURCE, {"symbol": "BTCUSDT", "paper_entry_allowed": True, "spread_bps": 1.0})
    micro_loop.write_json_atomic(micro_loop.LIQUIDATIONS_SOURCE, {"symbol": "BTCUSDT", "burst": True, "total_notional": 2_000_000})

    result = micro_loop.run_once()

    assert result["status"] == "ok"
    assert result["result_count"] == 3
    assert result["results"]["orderbook"]["paper_entry_allowed"] is True

def test_preflight_reads_actual_market_and_news_latest_paths(monkeypatch, tmp_path: Path):
    state = tmp_path / "state"
    memory = state / "agent_memory"
    memory.mkdir(parents=True)
    monkeypatch.setattr(pfg, "STATE_DIR", state)
    monkeypatch.setattr(pfg, "MEMORY_DIR", memory)
    pfg.write_json_atomic(state / "market_updates_latest.json", {"ts": pfg.utc_now(), "source": "market_observer", "source_ids": ["market_observer"], "hot": [{"symbol": "BTCUSDT"}]})
    pfg.write_json_atomic(memory / "news_latest.json", {"ts": pfg.utc_now(), "macro_risk_score": 0.2})
    pfg.write_json_atomic(memory / "trade_lifecycle_latest.json", {"ts": pfg.utc_now(), "learning_allowed": True})

    result = pfg.run_preflight(
        {"action": "paper_decision", "requires_fresh_market": True, "requires_lifecycle_clean": True},
        config={"mode": "paper_learning", "live_execution_enabled": False, "feature_flags": {"paper_trading": True, "live_orders": False}},
        output_path=state / "preflight_latest.json",
    )

    assert result["allowed"] is True
    assert "missing_market_observer" not in result["warnings"]
    assert "missing_news_observer" not in result["warnings"]
    assert result["freshness"]["market_observer"]["exists"] is True
    assert result["freshness"]["news_observer"]["exists"] is True

def test_alert_manager_queues_local_and_webhook(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(alerts, "ALERTS_HISTORY", tmp_path / "alerts.jsonl")
    monkeypatch.setattr(alerts, "ALERTS_LATEST", tmp_path / "alerts.json")
    monkeypatch.setattr(alerts, "ALERT_OUTBOX", tmp_path / "outbox.jsonl")
    monkeypatch.setattr(alerts, "WEBHOOK_OUTBOX", tmp_path / "webhook.jsonl")

    result = alerts.emit_and_queue_alert("warn", "quota low", "token=SECRET123456789012345678901234567890", channels=["local", "webhook"])

    assert len(result["queued_channels"]) == 2
    assert "SECRET" not in (tmp_path / "webhook.jsonl").read_text(encoding="utf-8")

def test_backup_restore_migrates_json_state(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(br, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(br, "BACKUP_MANIFESTS", tmp_path / "manifests")
    source = tmp_path / "state.json"
    br.write_json_atomic(source, {"value": 1})

    result = br.migrate_json_state([source], target_schema_version=99, output_path=tmp_path / "migration.json")

    assert result["ok"] is True
    assert br.sha256_file(source)
    assert br.write_json_atomic is not None

def test_daily_exam_prioritizes_self_model_gaps(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(dea, "SELF_MODEL", tmp_path / "self_model.json")
    dea.write_json(dea.SELF_MODEL, {"known_gaps": ["trade_lifecycle_not_clean"]})
    inputs = dea.load_inputs(max_log_lines=50)

    assert dea.choose_exam_type(inputs, "2026-06-21") == "risk_gate_review"

def test_daily_exam_prioritizes_ranked_test_memory_gaps(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(dea, "SELF_MODEL", tmp_path / "self_model.json")
    monkeypatch.setattr(dea, "TEST_RESULT_MEMORY", tmp_path / "test_result_memory.json")
    dea.write_json(dea.SELF_MODEL, {"known_gaps": []})
    dea.write_json(
        dea.TEST_RESULT_MEMORY,
        {
            "priority_curriculum": [
                {
                    "gap": "counterfactual_coverage_low",
                    "priority_score": 9,
                    "occurrences": 3,
                    "task": "raise replay coverage",
                    "action": "run replay",
                    "source": "counterfactual",
                }
            ],
        },
    )
    inputs = dea.load_inputs(max_log_lines=50)

    assert dea.choose_exam_type(inputs, "2026-06-21") == "setup_defense"

def test_daily_exam_scores_objective_performance_improvement():
    inputs = {
        "paper": {"closes": 30, "net": 6.0, "win_rate": 0.55},
        "shadow": {
            "fresh_window": {
                "overall": {"closed": 120, "expectancy": 0.02, "profit_factor": 1.3},
                "data_quality": {"confidence": "medium"},
            }
        },
        "post_trade": {"review_quality": {"cost_coverage_pct": 0.9, "r_multiple_coverage_pct": 0.85}},
        "counterfactual": {"updated_at": dea.utc_now(), "coverage_pct": 0.82, "replay_count": 100, "complete_count": 82},
        "walk_forward": {
            "updated_at": dea.utc_now(),
            "by_status": {"passed": 1},
            "rows": [{"status": "passed", "min_test_trades": 20, "test_metrics": {"trades": 24, "expectancy_after_fees": 0.03, "profit_factor": 1.2}, "errors": [], "can_place_live_orders": False}],
            "can_place_live_orders": False,
        },
        "promotion": {"failures": []},
    }

    result = dea.score_performance_improvement(inputs)

    assert result["score"] > 0.8
    assert result["shadow_source"] == "fresh_window"
    assert result["walk_forward_passed"] == 1

def test_daily_exam_rejects_stale_low_quality_or_unsafe_performance_proof():
    stale_ts = "2026-01-01T00:00:00+00:00"
    inputs = {
        "paper": {"closes": 30, "net": 6.0, "win_rate": 0.55},
        "shadow": {
            "fresh_window": {
                "overall": {"closed": 120, "expectancy": 0.05, "profit_factor": 2.0},
                "data_quality": {"confidence": "low", "api_error_count": 20},
            }
        },
        "post_trade": {"review_quality": {"cost_coverage_pct": 0.9, "r_multiple_coverage_pct": 0.85}},
        "counterfactual": {"updated_at": stale_ts, "coverage_pct": 1.0, "replay_count": 100, "complete_count": 100},
        "walk_forward": {
            "updated_at": stale_ts,
            "by_status": {"passed": 1},
            "rows": [{"status": "passed", "min_test_trades": 20, "test_metrics": {"trades": 3, "expectancy_after_fees": -0.01, "profit_factor": 0.8}, "errors": [], "can_place_live_orders": True}],
            "can_place_live_orders": True,
        },
        "promotion": {"failures": [], "can_place_live_orders": True},
    }

    result = dea.score_performance_improvement(inputs)

    assert result["score"] < 0.5
    assert result["walk_forward_passed"] == 0
    assert result["walk_forward_unsafe_live_permission"] is True
    assert result["promotion_unsafe_live_permission"] is True

def test_daily_exam_quality_is_capped_when_performance_proof_is_weak():
    fresh_ts = dea.utc_now()
    inputs = {
        "market": {"ts": fresh_ts},
        "news": {"ts": fresh_ts},
        "cognitive": {"ts": fresh_ts, "reasoning_trace": {"thought_quality_score": 0.9}},
        "self_improvement": {"ts": fresh_ts, "overall_learning_score": 0.9, "guardrail_proposal": {"can_loosen": False, "can_trade_live": False}},
        "bias": {"min_signal_score": 8, "risk_posture": "defensive"},
        "live_readiness": {"mode": "paper"},
        "shadow": {"overall": {"closed": 500, "win_rate": 0.6, "expectancy": -0.02, "profit_factor": 0.8}},
        "paper": {"closes": 5, "net": -1.0, "win_rate": 0.2},
        "setups": [{"trades": 50}],
        "post_trade": {"review_quality": {"cost_coverage_pct": 0.0, "r_multiple_coverage_pct": 0.0}},
        "counterfactual": {"coverage_pct": 0.0},
        "walk_forward": {"by_status": {"running": 1}},
        "promotion": {"failures": ["walk_forward_validation_running"]},
        "self_model": {},
        "previous_exam": {},
    }

    rubric = dea.quality_rubric(inputs)

    assert rubric["scores"]["performance_improvement"]["score"] < 0.35
    assert rubric["quality_score"] <= 65

def test_llm_council_run_role_uses_router_and_sanitizer(tmp_path: Path):
    def fake_call(system: str, user: str, model: str) -> str:
        return '{"summary":"ok","data_ids":["d1"],"recommendation":"paper test only","risk_proposal":{"can_place_live_orders":true}}'

    result = llm_council.run_role("risk_critic", {"x": 1}, ["d1"], llm_call=fake_call, history_path=tmp_path / "council.jsonl")

    assert result["accepted"] is False
    assert "unsafe_risk_or_live_permission" in result["errors"]
    assert result["payload"]["can_place_live_orders"] is False

def test_model_usage_ledger_records_local_cost_summary(tmp_path: Path):
    history = tmp_path / "usage.jsonl"
    latest = tmp_path / "usage.json"

    row = mul.record_model_usage("daily_exam", "gpt-5.5", "9router", prompt="hello", response="world", history_path=history, latest_path=latest)

    assert row["input_tokens_est"] >= 1
    assert mul.summarize_model_usage(history)["call_count"] == 1

def test_security_import_guard_flags_forbidden_live_import(tmp_path: Path):
    module = tmp_path / "paper_agent.py"
    module.write_text("import binance.client\n", encoding="utf-8")

    result = sig.scan_import_guard([module], output_path=tmp_path / "guard.json")

    assert result["ok"] is False
    assert result["violations"][0]["forbidden_imports"] == ["binance.client"]
