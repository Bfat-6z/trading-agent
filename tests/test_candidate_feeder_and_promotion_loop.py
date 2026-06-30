from pathlib import Path

import autonomous_paper_trading_loop as paper_loop
import autonomous_paper_trading_brain as brain
import agent_process_supervisor as aps
import paper_candidate_feeder as feeder
import promotion_evaluator_loop as promo_loop

def test_candidate_feeder_builds_extreme_reversal_candidates():
    market = {"ts": "2026-06-21T00:00:00+00:00", "hot": [{"symbol": "ABCUSDT", "price": 10, "high": 11, "low": 6, "change_pct": 25, "range_pos": 0.9, "quote_volume": 100_000_000, "funding_pct": 0.01}]}

    candidates = feeder.build_candidates(market)

    assert candidates[0]["side"] == "SHORT"
    assert candidates[0]["setup_id"] == "exhaustion_fade"
    assert candidates[0]["tp"] < candidates[0]["entry"] < candidates[0]["sl"]
    assert candidates[0]["leverage"] == 5
    stop_distance = (candidates[0]["sl"] - candidates[0]["entry"]) / candidates[0]["entry"]
    assert stop_distance <= 0.0351
    assert candidates[0]["can_place_live_orders"] is False

def test_candidate_feeder_prefers_ranked_funding_squeeze_over_exhaustion():
    market = {
        "ts": "2026-06-21T00:00:00+00:00",
        "hot": [
            {
                "symbol": "HUSDT",
                "price": 0.06238,
                "high": 0.12,
                "low": 0.06,
                "change_pct": -43.857,
                "range_pos": 0.025,
                "quote_volume": 89_052_095,
                "funding_pct": -0.238931,
            }
        ],
    }
    rankings = {
        "top_setup_id": "funding_squeeze",
        "rankings": [
            {"setup_id": "funding_squeeze", "allocation_hint": "normal", "evidence_expectancy": 0.45, "rank_score": 4.7},
            {"setup_id": "exhaustion_fade", "allocation_hint": "reduced", "evidence_expectancy": -0.2, "rank_reasons": ["non_positive_evidence_expectancy"]},
        ],
    }

    candidates = feeder.build_candidates(market, setup_rankings=rankings)

    assert candidates[0]["setup_id"] == "funding_squeeze"
    assert candidates[0]["side"] == "LONG"
    assert candidates[0]["setup_routing"]["setup_bonus"] > 0
    assert candidates[0]["can_place_live_orders"] is False

def test_paper_brain_prefers_tradeable_setup_over_higher_raw_score():
    candidates = [
        {"symbol": "AAAUSDT", "side": "LONG", "setup_id": "exhaustion_fade", "score": 9.5},
        {"symbol": "BBBUSDT", "side": "LONG", "setup_id": "funding_squeeze", "score": 7.2},
    ]
    rankings = [
        {"setup_id": "exhaustion_fade", "allocation_hint": "reduced", "evidence_expectancy": -0.2, "rank_reasons": ["non_positive_evidence_expectancy"], "rank_score": -4.0, "paper_only_min_score_adjustment": 1.0},
        {"setup_id": "funding_squeeze", "allocation_hint": "normal", "evidence_expectancy": 0.3, "rank_reasons": ["positive_rank"], "rank_score": 4.0},
    ]

    chosen = brain.choose_candidate(candidates, rankings)

    assert chosen["setup_id"] == "funding_squeeze"

def test_candidate_feeder_run_once_writes_candidates(monkeypatch, tmp_path: Path):
    memory = tmp_path / "agent_memory"
    memory.mkdir()
    monkeypatch.setattr(feeder, "MARKET_LATEST", tmp_path / "market.json")
    monkeypatch.setattr(feeder, "REGISTRY_PATH", tmp_path / "instrument_registry.json")
    monkeypatch.setattr(feeder, "REGISTRY_QUALITY_PATH", memory / "universe_quality_latest.json")
    monkeypatch.setattr(feeder, "CANDIDATES_PATH", memory / "paper_candidates_latest.json")
    monkeypatch.setattr(feeder, "LATEST_PATH", memory / "paper_candidate_feeder_latest.json")
    monkeypatch.setattr(feeder, "HISTORY_PATH", memory / "paper_candidate_feeder_history.jsonl")
    monkeypatch.setattr(feeder, "HEARTBEAT_PATH", tmp_path / "paper_candidate_feeder_heartbeat.json")
    feeder.write_json_atomic(feeder.MARKET_LATEST, {"ts": "now", "hot": [{"symbol": "ABCUSDT", "price": 10, "high": 11, "low": 6, "change_pct": 25, "range_pos": 0.9, "quote_volume": 100_000_000, "funding_pct": 0.01}]})
    monkeypatch.setattr(feeder, "enqueue_job", lambda *args, **kwargs: {"ok": True, "job_id": "j1"})

    result = feeder.run_once()

    assert result["candidate_count"] == 1
    assert result["registry_update"]["instrument_count"] == 1
    assert feeder.CANDIDATES_PATH.exists()

def test_candidate_feeder_bootstraps_paper_instrument_registry(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(feeder, "REGISTRY_QUALITY_PATH", tmp_path / "quality.json")
    market = {"hot": [{"symbol": "ABCUSDT", "price": 0.123}]}

    result = feeder.bootstrap_paper_instrument_registry(market, path=tmp_path / "registry.json")

    row = result["registry"]["instruments"]["ABCUSDT"]
    assert row["status"] == "shadow_only"
    assert row["allowed_effect"] == "shadow_only"
    assert row["min_notional"] == "0.01"

def test_paper_loop_ignores_candidate_marked_exploration_and_untrusted_candidate(monkeypatch, tmp_path: Path):
    memory = tmp_path / "agent_memory"
    memory.mkdir()
    monkeypatch.setattr(paper_loop, "MEMORY_DIR", memory)
    monkeypatch.setattr(paper_loop, "LATEST_PATH", memory / "loop.json")
    monkeypatch.setattr(paper_loop, "HISTORY_PATH", memory / "loop.jsonl")
    monkeypatch.setattr(paper_loop, "HEARTBEAT_PATH", tmp_path / "loop_heartbeat.json")
    monkeypatch.setattr(paper_loop, "kill_switch_active", lambda: False)
    monkeypatch.setattr(paper_loop, "evaluate_live_permission", lambda request: {"allowed": True})
    monkeypatch.setattr(paper_loop, "evaluate_circuit_breakers", lambda metrics: {"allowed": True})
    monkeypatch.setattr(paper_loop, "load_account", lambda: {"equity": "100", "cash": "100"})
    monkeypatch.setattr(paper_loop, "load_runtime_config", lambda: {"feature_flags": {"paper_exploration": False}})
    monkeypatch.setattr(paper_loop, "load_queue_candidate_batch", lambda worker_id: ({"source": "test", "candidates": [{"symbol": "ABCUSDT", "side": "SHORT", "setup_id": "exhaustion_fade", "entry": 10, "sl": 11, "tp": 9, "score": 8, "exploration_allowed": True}]}, None))
    monkeypatch.setattr(paper_loop, "decide_paper_action", lambda candidates, setup_stats, account, exploration_allowed=False: {"action": "paper_open_candidate", "can_place_live_orders": False, "exploration_allowed": exploration_allowed})

    result = paper_loop.run_once()

    assert result["exploration_allowed"] is False
    assert result["decision"]["action"] == "skip"
    assert result["decision"]["reason"] == "no_trusted_candidates"
    assert "untrusted_candidate_producer" in result["decision"]["rejected_candidates"][0]["errors"]

def test_paper_loop_ignores_batch_stats_without_losing_library_evidence(monkeypatch, tmp_path: Path):
    memory = tmp_path / "agent_memory"
    memory.mkdir()
    monkeypatch.setattr(paper_loop, "MEMORY_DIR", memory)
    monkeypatch.setattr(paper_loop, "LATEST_PATH", memory / "loop.json")
    monkeypatch.setattr(paper_loop, "HISTORY_PATH", memory / "loop.jsonl")
    monkeypatch.setattr(paper_loop, "HEARTBEAT_PATH", tmp_path / "loop_heartbeat.json")
    monkeypatch.setattr(paper_loop, "kill_switch_active", lambda: False)
    monkeypatch.setattr(paper_loop, "evaluate_live_permission", lambda request: {"allowed": True})
    monkeypatch.setattr(paper_loop, "evaluate_circuit_breakers", lambda metrics: {"allowed": True})
    monkeypatch.setattr(paper_loop, "load_account", lambda: {"equity": "100", "cash": "100"})
    monkeypatch.setattr(paper_loop, "load_runtime_config", lambda: {"feature_flags": {"paper_exploration": True}})
    monkeypatch.setattr(
        paper_loop,
        "load_queue_candidate_batch",
        lambda worker_id: (
            {
                "candidates": [{"symbol": "ABCUSDT", "side": "SHORT", "setup_id": "exhaustion_fade", "entry": 10, "sl": 11, "tp": 9, "score": 8, "source": "paper_candidate_feeder", "producer_id": "paper_candidate_feeder", "allowed_effect": "feature_input"}],
                "setup_stats": [{"setup_id": "exhaustion_fade", "expectancy": 0.1}],
            },
            None,
        ),
    )
    monkeypatch.setattr(
        paper_loop,
        "setup_stats_from_library",
        lambda: [{"setup_id": "exhaustion_fade", "evidence_expectancy": -0.2, "metadata": {"paper_only_min_score_adjustment": 1.0}}],
    )
    captured = {}
    def fake_decide(candidates, setup_stats, account, exploration_allowed=False):
        captured["setup_stats"] = setup_stats
        return {"action": "skip", "can_place_live_orders": False}
    monkeypatch.setattr(paper_loop, "decide_paper_action", fake_decide)

    paper_loop.run_once()

    row = captured["setup_stats"][0]
    assert "expectancy" not in row
    assert row["evidence_expectancy"] == -0.2
    assert row["metadata"]["paper_only_min_score_adjustment"] == 1.0

def test_promotion_evaluator_loop_writes_latest(monkeypatch, tmp_path: Path):
    memory = tmp_path / "agent_memory"
    memory.mkdir()
    monkeypatch.setattr(promo_loop, "LATEST_PATH", memory / "promotion_loop.json")
    monkeypatch.setattr(promo_loop, "HISTORY_PATH", memory / "promotion_loop.jsonl")
    monkeypatch.setattr(promo_loop, "HEARTBEAT_PATH", tmp_path / "promotion_loop_heartbeat.json")
    monkeypatch.setattr(promo_loop, "evaluate_from_state", lambda: {"state": "paper_learning", "passed": False, "can_place_live_orders": False})

    result = promo_loop.run_once()

    assert result["promotion"]["state"] == "paper_learning"
    assert result["can_place_live_orders"] is False

def test_supervisor_includes_memory_and_skill_forge_agents():
    names = {spec.name for spec in aps.specs()}

    assert "memory_consolidation_agent" in names
    assert "skill_forge_agent" in names
    assert "whale_flow_observer" in names
