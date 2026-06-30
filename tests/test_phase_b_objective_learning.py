import json
from pathlib import Path

import aplus_setup_ontology as aplus
import counterfactual_replay_agent as cf
import data_source_registry as dsr
import derivatives_observer as dob
import learning_dashboard_data as ldd
import liquidation_observer as lob
import market_data_lake as mdl
import market_feature_store as mfs
import orderbook_observer as obo
import paper_exploration_policy as pep
import paper_execution_simulator as pes
import post_trade_learning_agent as ptl


def candles():
    return [
        {"ts": "2026-06-21T00:00:00+00:00", "open": 100, "high": 101, "low": 99.5, "close": 100.5, "volume": 1000},
        {"ts": "2026-06-21T00:01:00+00:00", "open": 100.5, "high": 102, "low": 100, "close": 101.5, "volume": 1500},
        {"ts": "2026-06-21T00:02:00+00:00", "open": 101.5, "high": 103, "low": 101, "close": 102.5, "volume": 1800},
        {"ts": "2026-06-21T00:03:00+00:00", "open": 102.5, "high": 103.2, "low": 101.8, "close": 102.8, "volume": 1200},
    ]


def test_rate_limited_source_is_degraded(tmp_path: Path, monkeypatch):
    path = tmp_path / "sources.json"
    monkeypatch.setattr(dsr, "DATA_SOURCE_EVENTS", tmp_path / "source_events.jsonl")

    decision = dsr.mark_source_event("binance_usdm_klines", "rate_limited", path=path)

    assert decision["usable"] is False
    assert "source_rate_limited" in decision["errors"]


def test_market_data_lake_replay_manifest_uses_pinned_cache(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(mdl, "MARKET_CACHE_DIR", tmp_path / "market_cache")
    monkeypatch.setattr(mdl, "REPLAY_MANIFEST_DIR", tmp_path / "replay_manifests")

    cached = mdl.store_candles("BTCUSDT", "1m", candles(), source_id="local_state")
    manifest = mdl.create_replay_manifest("t1", cached["cache_id"], ["local_state"], {"fee": "0.0005"})

    assert mdl.load_candles(cached["cache_id"])["cache_id"] == cached["cache_id"]
    assert manifest["candle_cache_id"] == cached["cache_id"]
    assert (tmp_path / "replay_manifests" / f"{manifest['manifest_id']}.json").exists()


def test_feature_store_is_deterministic_and_degrades_without_derivatives(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(mfs, "FEATURE_STORE_DIR", tmp_path / "feature_store")
    monkeypatch.setattr(mfs, "REGIME_LATEST", tmp_path / "regime_latest.json")

    first = mfs.compute_market_features("BTCUSDT", "1m", candles())
    second = mfs.compute_market_features("BTCUSDT", "1m", candles())

    assert first["feature_id"] == second["feature_id"]
    assert first["feature_confidence"] < 0.85
    assert "derivatives" in first["missing_features"]
    assert first["regime"]["regime_version"] == "regime_v1"


def test_aplus_requires_invalidation_and_not_high_volume_alone():
    candidate = {
        "setup_id": "volume_only",
        "r_multiple": 0,
        "hard_requirements": {"explicit_invalidation": False, "execution_realistic": True},
        "dimensions": {"liquidity_volume_quality": 1.0},
    }

    score = aplus.score_setup(candidate)

    assert score["can_assign_aplus"] is False
    assert "missing_explicit_invalidation" in score["errors"]
    assert "high_volume_alone_not_aplus" in score["errors"]


def test_paper_stop_can_fill_worse_than_trigger():
    result = pes.simulate_exit(
        "LONG",
        entry="100",
        qty="1",
        sl="99",
        tp="103",
        leverage="2",
        candles=[{"ts": "2026-06-21T00:01:00+00:00", "open": 98, "high": 98.5, "low": 97, "close": 98}],
    )

    assert result["reason"] == "sl"
    assert float(result["exit"]) < 99


def test_limit_order_may_remain_unfilled(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(pes, "PAPER_ORDERS", tmp_path / "paper_orders.jsonl")

    result = pes.simulate_entry_order("BTCUSDT", "LONG", "limit", "1", "99", {"ts": "x", "open": 100, "high": 101, "low": 100, "close": 101})

    assert result["status"] == "open"
    assert result["reason"] == "limit_unfilled"


def test_funding_payment_changes_equity(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(pes, "PAPER_POSITIONS", tmp_path / "paper_positions.json")

    account = pes.apply_funding_payment({"equity": "100"}, notional="1000", funding_rate="0.001", side="LONG")

    assert float(account["equity"]) == 99.0
    assert float(account["last_funding_payment"]) == -1.0


def test_post_trade_learning_marks_bad_win_when_process_bad():
    trade = {"trade_id": "t1", "side": "LONG", "entry": "100", "exit": "101", "sl": "99", "net": "0.5", "close_ts": "2026-06-21T00:03:00+00:00"}

    review = ptl.review_closed_trade(trade, candles(), setup_score={"score": 0.2}, append=False)

    assert review["classification"] == "bad_win"


def test_post_trade_learning_detects_stop_too_tight():
    trade = {"trade_id": "t2", "side": "LONG", "entry": "100", "exit": "99", "sl": "99", "tp": "102", "net": "-1", "reason": "sl", "close_ts": "2026-06-21T00:01:00+00:00"}

    review = ptl.review_closed_trade(trade, candles(), setup_score={"score": 0.8}, append=False)

    assert review["classification"] == "stop_too_tight"
    assert review["flags"]["stop_too_tight"] is True


def test_post_trade_learning_records_costs_failure_and_counterfactual(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ptl, "COUNTERFACTUAL_REPLAYS_JSONL", tmp_path / "counterfactual_replays.jsonl")
    ptl.COUNTERFACTUAL_REPLAYS_JSONL.write_text(
        json.dumps(
            {
                "signal_id": "t3",
                "replay_id": "cf_1",
                "status": "complete",
                "conclusion": "parameter_improvement_candidate",
                "best_variant": {"variant": "sl1_tp0.5", "net": 0.2},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    trade = {
        "trade_id": "t3",
        "side": "LONG",
        "entry": "100",
        "exit": "99",
        "sl": "99",
        "tp": "102",
        "gross": "-1",
        "net": "-1.08",
        "entry_fee": "0.03",
        "exit_fee": "0.03",
        "fee": "0.06",
        "funding_payment": "-0.02",
        "margin": "5",
        "slippage": "0.01",
        "reason": "manual",
        "setup_id": "exhaustion_fade",
        "close_ts": "2026-06-21T00:02:00+00:00",
    }

    review = ptl.review_closed_trade(trade, candles(), setup_score={"score": 0.7}, append=False)

    assert review["costs"]["fees"] == 0.06
    assert review["costs"]["funding_payment"] == -0.02
    assert review["counterfactual"]["replay_id"] == "cf_1"
    assert review["primary_failure_reason"] == "counterfactual_parameter_improvement"
    assert review["setup_validity_score"] == 0.7
    assert review["flags"]["fee_drag_high"] is True
    assert review["flags"]["funding_drag"] is True

def test_post_trade_learning_classifies_microstructure_and_context_failures(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ptl, "COUNTERFACTUAL_REPLAYS_JSONL", tmp_path / "counterfactual_replays.jsonl")
    base = {
        "side": "LONG",
        "entry": "100",
        "exit": "99",
        "sl": "99",
        "tp": "102",
        "gross": "-1",
        "net": "-1",
        "margin": "5",
        "reason": "manual",
        "setup_id": "exhaustion_fade",
        "close_ts": "2026-06-21T00:02:00+00:00",
    }

    spread_review = ptl.review_closed_trade({**base, "trade_id": "spread_1", "spread_bps": 45}, candles(), setup_score={"score": 0.7}, append=False)
    thin_review = ptl.review_closed_trade({**base, "trade_id": "thin_1", "quote_volume": 100_000}, candles(), setup_score={"score": 0.7}, append=False)
    crowded_review = ptl.review_closed_trade({**base, "trade_id": "crowded_1", "funding_pct": 0.24, "open_interest_delta": 0.22}, candles(), setup_score={"score": 0.7}, append=False)
    regime_review = ptl.review_closed_trade({**base, "trade_id": "regime_1", "market_regime": "risk_off", "setup_expected_regime": "risk_on"}, candles(), setup_score={"score": 0.7}, append=False)

    assert spread_review["classification"] == "spread_slippage_issue"
    assert spread_review["flags"]["spread_slippage_issue"] is True
    assert thin_review["classification"] == "thin_liquidity"
    assert thin_review["flags"]["thin_liquidity"] is True
    assert crowded_review["classification"] == "crowded_trade"
    assert crowded_review["flags"]["crowded_trade"] is True
    assert regime_review["classification"] == "regime_mismatch"
    assert regime_review["flags"]["regime_mismatch"] is True

def test_post_trade_learning_uses_counterfactual_for_entry_timing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ptl, "COUNTERFACTUAL_REPLAYS_JSONL", tmp_path / "counterfactual_replays.jsonl")
    ptl.COUNTERFACTUAL_REPLAYS_JSONL.write_text(
        json.dumps(
            {
                "signal_id": "timing_1",
                "replay_id": "cf_timing",
                "status": "complete",
                "conclusion": "parameter_improvement_candidate",
                "best_variant": {"variant": "entry_plus_1", "net": 0.4},
                "base_variant": {"variant": "sl1_tp1", "net": -0.2},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    trade = {"trade_id": "timing_1", "side": "LONG", "entry": "100", "exit": "99", "sl": "99", "tp": "102", "net": "-1", "reason": "manual", "close_ts": "2026-06-21T00:02:00+00:00"}

    review = ptl.review_closed_trade(trade, candles(), setup_score={"score": 0.7}, append=False)

    assert review["classification"] == "early_entry"
    assert review["primary_failure_reason"] == "early_entry"
    assert review["flags"]["early_entry"] is True

    ptl.COUNTERFACTUAL_REPLAYS_JSONL.write_text(
        json.dumps(
            {
                "signal_id": "timing_2",
                "replay_id": "cf_timing_late",
                "status": "complete",
                "conclusion": "parameter_improvement_candidate",
                "best_variant": {"variant": "entry_minus_1", "net": 0.3},
                "base_variant": {"variant": "sl1_tp1", "net": -0.2},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    late_trade = {**trade, "trade_id": "timing_2"}
    late_review = ptl.review_closed_trade(late_trade, candles(), setup_score={"score": 0.7}, append=False)

    assert late_review["classification"] == "late_entry"
    assert late_review["flags"]["late_entry"] is True

def test_post_trade_learning_reads_news_context_from_trade_row(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ptl, "COUNTERFACTUAL_REPLAYS_JSONL", tmp_path / "counterfactual_replays.jsonl")
    trade = {"trade_id": "news_1", "side": "LONG", "entry": "100", "exit": "99", "sl": "99", "tp": "102", "net": "-1", "news_conflict": True, "market": {"primary_regime": "risk_off"}, "close_ts": "2026-06-21T00:02:00+00:00"}

    review = ptl.review_closed_trade(trade, candles(), setup_score={"score": 0.7}, append=False)

    assert review["classification"] == "news_conflict"
    assert review["market_regime"] == "risk_off"

def test_post_trade_learning_summary_includes_failure_and_quality_scores():
    summary = ptl.summarize_reviews(
        [
            {"classification": "bad_loss", "primary_failure_reason": "adverse_move_to_stop", "process_quality_score": 0.6, "outcome_quality_score": 0.1, "setup_validity_score": 0.7, "mae": -0.01, "mfe": 0.02, "r_multiple": -1, "costs": {"fees": 0.1}, "counterfactual": {"replay_id": "cf1"}},
            {"classification": "good_win", "primary_failure_reason": "no_failure_profit", "process_quality_score": 0.8, "outcome_quality_score": 0.9, "setup_validity_score": 0.8},
        ]
    )

    assert summary["by_primary_failure_reason"]["adverse_move_to_stop"] == 1
    assert summary["avg_process_quality_score"] == 0.7
    assert summary["avg_outcome_quality_score"] == 0.5
    assert summary["review_quality"]["mfe_mae_coverage_pct"] == 0.5
    assert summary["review_quality"]["counterfactual_attach_pct"] == 0.5

def test_counterfactual_blocked_winner_is_evidence_only(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cf, "REPLAYS_JSONL", tmp_path / "counterfactual_replays.jsonl")
    monkeypatch.setattr(cf, "LATEST_JSON", tmp_path / "counterfactual_latest.json")
    monkeypatch.setattr(pes, "PAPER_ORDERS", tmp_path / "paper_orders.jsonl")

    signal = {"signal_id": "s1", "symbol": "BTCUSDT", "side": "LONG", "entry": "100", "sl": "99", "tp": "101", "qty": "1", "leverage": "2", "blocked": True, "source_available_at_max": "2026-06-21T00:03:00+00:00"}
    result = cf.replay_signal(signal, candles(), append=True)

    assert result["status"] == "complete"
    assert result["conclusion"] == "risk_gate_false_positive_candidate"
    assert result["gate_change_allowed"] is False


def test_counterfactual_mark_only_snapshot_is_unresolved(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cf, "REPLAYS_JSONL", tmp_path / "counterfactual_replays.jsonl")
    monkeypatch.setattr(cf, "LATEST_JSON", tmp_path / "counterfactual_latest.json")

    signal = {
        "signal_id": "paper_pos_1",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": "100",
        "sl": "99",
        "tp": "101",
        "qty": "1",
        "data_quality": "mark_only_snapshot",
        "open_ts": "2026-06-21T00:00:00+00:00",
    }

    replay_candles, source = cf.candles_for_signal(signal)
    result = cf.replay_signal(signal, replay_candles, append=True, candle_source=source)

    assert result["status"] == "unresolved"
    assert result["reason"] == "insufficient_candle_coverage"
    assert result["candle_source"]["source"] == "mark_only_snapshot"
    assert cf.write_latest_summary()["unresolved_count"] == 1

def test_counterfactual_run_once_appends_unresolved_for_paper_close_without_candles(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cf, "PAPER_TRADES_JSONL", tmp_path / "paper_trades.jsonl")
    monkeypatch.setattr(cf, "REPLAYS_JSONL", tmp_path / "counterfactual_replays.jsonl")
    monkeypatch.setattr(cf, "LATEST_JSON", tmp_path / "counterfactual_latest.json")
    monkeypatch.setattr(cf, "HEARTBEAT_PATH", tmp_path / "counterfactual_heartbeat.json")
    monkeypatch.setattr(cf, "PAPER_BRAIN_HISTORY_JSONL", tmp_path / "paper_brain_history.jsonl")
    monkeypatch.setattr(cf, "PAPER_CANDIDATE_HISTORY_JSONL", tmp_path / "paper_candidate_history.jsonl")

    row = {
        "event": "paper_close",
        "trade_id": "paper_pos_2",
        "symbol": "ETHUSDT",
        "side": "SHORT",
        "entry": "100",
        "exit": "101",
        "qty": "1",
        "sl": "101",
        "tp": "98",
        "net": "-1",
        "data_quality": "mark_only_snapshot",
        "open_ts": "2026-06-21T00:00:00+00:00",
        "close_ts": "2026-06-21T00:02:00+00:00",
    }
    cf.PAPER_TRADES_JSONL.write_text(json.dumps(row) + "\n", encoding="utf-8")

    result = cf.run_once(limit=5)
    rows = cf.read_jsonl(cf.REPLAYS_JSONL)

    assert result["eligible_scanned"] == 1
    assert result["new_replays"] == 1
    assert rows[0]["status"] == "unresolved"
    assert result["summary"]["unresolved_count"] == 1

def test_counterfactual_embedded_candles_complete_and_entry_plus_one(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cf, "REPLAYS_JSONL", tmp_path / "counterfactual_replays.jsonl")
    monkeypatch.setattr(cf, "LATEST_JSON", tmp_path / "counterfactual_latest.json")
    monkeypatch.setattr(pes, "PAPER_ORDERS", tmp_path / "paper_orders.jsonl")

    signal = {
        "signal_id": "paper_pos_3",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": "100",
        "sl": "99",
        "tp": "101",
        "qty": "1",
        "leverage": "2",
        "candles": candles(),
    }
    replay_candles, source = cf.candles_for_signal(signal)
    result = cf.replay_signal(signal, replay_candles, append=True, candle_source=source)

    assert result["status"] == "complete"
    assert result["candle_source"]["source"] == "embedded"
    assert "entry_plus_1" in {row["variant"] for row in result["variants"]}

def test_counterfactual_replay_does_not_write_paper_orders(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cf, "REPLAYS_JSONL", tmp_path / "counterfactual_replays.jsonl")
    monkeypatch.setattr(cf, "LATEST_JSON", tmp_path / "counterfactual_latest.json")
    paper_orders = tmp_path / "paper_orders.jsonl"
    monkeypatch.setattr(pes, "PAPER_ORDERS", paper_orders)

    signal = {"signal_id": "pure_1", "symbol": "BTCUSDT", "side": "LONG", "entry": "100", "sl": "99", "tp": "101", "qty": "1", "leverage": "2"}
    result = cf.replay_signal(signal, candles(), append=True)

    assert result["status"] == "complete"
    assert not paper_orders.exists()

def test_counterfactual_unresolved_can_retry_when_cache_appears(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cf, "PAPER_TRADES_JSONL", tmp_path / "paper_trades.jsonl")
    monkeypatch.setattr(cf, "PAPER_BRAIN_HISTORY_JSONL", tmp_path / "paper_brain_history.jsonl")
    monkeypatch.setattr(cf, "REPLAYS_JSONL", tmp_path / "counterfactual_replays.jsonl")
    monkeypatch.setattr(cf, "LATEST_JSON", tmp_path / "counterfactual_latest.json")
    monkeypatch.setattr(cf, "HEARTBEAT_PATH", tmp_path / "counterfactual_heartbeat.json")
    monkeypatch.setattr(cf, "PAPER_CANDIDATE_HISTORY_JSONL", tmp_path / "paper_candidate_history.jsonl")
    monkeypatch.setattr(mdl, "MARKET_CACHE_DIR", tmp_path / "market_cache")
    monkeypatch.setattr(pes, "PAPER_ORDERS", tmp_path / "paper_orders.jsonl")

    row = {
        "event": "paper_close",
        "trade_id": "retry_1",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": "100",
        "exit": "101",
        "qty": "1",
        "sl": "99",
        "tp": "101",
        "net": "1",
        "data_quality": "mark_only_snapshot",
        "open_ts": "2026-06-21T00:00:00+00:00",
        "close_ts": "2026-06-21T00:03:00+00:00",
    }
    cf.PAPER_TRADES_JSONL.write_text(json.dumps(row) + "\n", encoding="utf-8")
    first = cf.run_once(limit=5)
    cached = mdl.store_candles("BTCUSDT", "1m", candles(), source_id="test")
    row["data_quality"] = "mark_sequence"
    row["candle_cache_id"] = cached["cache_id"]
    cf.PAPER_TRADES_JSONL.write_text(json.dumps(row) + "\n", encoding="utf-8")
    second = cf.run_once(limit=5)

    rows = cf.read_jsonl(cf.REPLAYS_JSONL)
    assert first["summary"]["unresolved_count"] == 1
    assert second["summary"]["complete_count"] == 1
    assert any(row["status"] == "complete" for row in rows)

def test_counterfactual_invalid_signal_is_unresolved_not_crash(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cf, "REPLAYS_JSONL", tmp_path / "counterfactual_replays.jsonl")
    monkeypatch.setattr(cf, "LATEST_JSON", tmp_path / "counterfactual_latest.json")

    result = cf.replay_signal({"signal_id": "bad_1", "side": "LONG", "entry": "100", "sl": "99", "tp": "101", "qty": "1"}, candles(), append=True)

    assert result["status"] == "unresolved"
    assert result["reason"] == "invalid_replay_signal"
    assert "missing_symbol" in result["errors"]

def test_counterfactual_run_once_ingests_blocked_brain_decision(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cf, "PAPER_TRADES_JSONL", tmp_path / "paper_trades.jsonl")
    monkeypatch.setattr(cf, "PAPER_BRAIN_HISTORY_JSONL", tmp_path / "paper_brain_history.jsonl")
    monkeypatch.setattr(cf, "REPLAYS_JSONL", tmp_path / "counterfactual_replays.jsonl")
    monkeypatch.setattr(cf, "LATEST_JSON", tmp_path / "counterfactual_latest.json")
    monkeypatch.setattr(cf, "HEARTBEAT_PATH", tmp_path / "counterfactual_heartbeat.json")
    monkeypatch.setattr(cf, "PAPER_CANDIDATE_HISTORY_JSONL", tmp_path / "paper_candidate_history.jsonl")

    decision = {
        "action": "skip",
        "decided_at": "2026-06-21T00:00:00+00:00",
        "candidate": {"candidate_id": "cand_1", "symbol": "BTCUSDT", "side": "LONG", "entry": "100", "sl": "99", "tp": "101", "score": 7},
        "errors": ["score_below_memory_minimum"],
    }
    cf.PAPER_BRAIN_HISTORY_JSONL.write_text(json.dumps(decision) + "\n", encoding="utf-8")

    result = cf.run_once(limit=5)
    rows = cf.read_jsonl(cf.REPLAYS_JSONL)

    assert result["eligible_scanned"] == 1
    assert rows[0]["signal_id"] == "cand_1"
    assert rows[0]["status"] == "unresolved"

def test_counterfactual_summary_reports_complete_and_unresolved_coverage():
    summary = cf.summarize_replays(
        [
            {"status": "complete", "conclusion": "no_change"},
            {"status": "unresolved", "reason": "insufficient_candle_coverage"},
        ]
    )

    assert summary["replay_count"] == 2
    assert summary["complete_count"] == 1
    assert summary["unresolved_count"] == 1
    assert summary["coverage_pct"] == 0.5

def test_counterfactual_phase10_preserves_original_base_signal(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cf, "REPLAYS_JSONL", tmp_path / "counterfactual_replays.jsonl")
    monkeypatch.setattr(cf, "LATEST_JSON", tmp_path / "counterfactual_latest.json")
    monkeypatch.setattr(pes, "PAPER_ORDERS", tmp_path / "paper_orders.jsonl")

    signal = {"signal_id": "base_1", "symbol": "BTCUSDT", "side": "LONG", "entry": "100", "sl": "99", "tp": "103", "qty": "1", "leverage": "2"}
    result = cf.replay_signal(signal, candles(), append=True)
    variants = {row["variant"] for row in result["variants"]}

    assert result["status"] == "complete"
    assert result["base_signal"]["tp"] == "103"
    assert result["base_variant"]["variant"] == "base_original"
    assert result["base_variant"]["entry"] == 100.0
    assert result["base_variant"]["sl"] == 99.0
    assert result["base_variant"]["tp"] == 103.0
    assert "sl1_tp1" in variants
    assert "higher_leverage" in variants
    assert "time_exit" in variants
    assert "trailing_1r" in variants
    assert "no_trade" in variants

def test_counterfactual_phase10_candidate_census_counts_denominator(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cf, "PAPER_TRADES_JSONL", tmp_path / "paper_trades.jsonl")
    monkeypatch.setattr(cf, "PAPER_BRAIN_HISTORY_JSONL", tmp_path / "paper_brain_history.jsonl")
    monkeypatch.setattr(cf, "PAPER_CANDIDATE_HISTORY_JSONL", tmp_path / "paper_candidate_history.jsonl")
    monkeypatch.setattr(cf, "REPLAYS_JSONL", tmp_path / "counterfactual_replays.jsonl")
    monkeypatch.setattr(cf, "LATEST_JSON", tmp_path / "counterfactual_latest.json")
    monkeypatch.setattr(cf, "HEARTBEAT_PATH", tmp_path / "counterfactual_heartbeat.json")
    monkeypatch.setattr(pes, "PAPER_ORDERS", tmp_path / "paper_orders.jsonl")
    cf.PAPER_CANDIDATE_HISTORY_JSONL.write_text(
        json.dumps(
            {
                "updated_at": "2026-06-21T00:00:00+00:00",
                "market_ts": "2026-06-21T00:00:00+00:00",
                "candidates": [
                    {"candidate_id": "census_complete", "symbol": "BTCUSDT", "side": "LONG", "entry": "100", "sl": "99", "tp": "101", "source_available_at_max": "2026-06-21T00:03:00+00:00", "candles": candles()},
                    {"candidate_id": "census_missing", "symbol": "ETHUSDT", "side": "SHORT", "entry": "100", "sl": "101", "tp": "98"},
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = cf.run_once(limit=10)
    rows = cf.read_jsonl(cf.REPLAYS_JSONL)

    assert result["eligible_scanned"] == 2
    assert {row["signal_id"] for row in rows} == {"census_complete", "census_missing"}
    assert result["summary"]["eligible_count"] == 2
    assert result["summary"]["latest_complete_count"] == 1
    assert result["summary"]["latest_unresolved_count"] == 1
    assert result["summary"]["coverage_pct"] == 0.5

def test_counterfactual_phase10_empty_candidate_scan_stays_in_denominator(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cf, "PAPER_TRADES_JSONL", tmp_path / "paper_trades.jsonl")
    monkeypatch.setattr(cf, "PAPER_BRAIN_HISTORY_JSONL", tmp_path / "paper_brain_history.jsonl")
    monkeypatch.setattr(cf, "PAPER_CANDIDATE_HISTORY_JSONL", tmp_path / "paper_candidate_history.jsonl")
    monkeypatch.setattr(cf, "REPLAYS_JSONL", tmp_path / "counterfactual_replays.jsonl")
    monkeypatch.setattr(cf, "LATEST_JSON", tmp_path / "counterfactual_latest.json")
    monkeypatch.setattr(cf, "HEARTBEAT_PATH", tmp_path / "counterfactual_heartbeat.json")
    cf.PAPER_CANDIDATE_HISTORY_JSONL.write_text(
        json.dumps({"updated_at": "2026-06-21T00:00:00+00:00", "market_ts": "2026-06-21T00:00:00+00:00", "reason": "rate_limited_universe_slice", "candidates": []}) + "\n",
        encoding="utf-8",
    )

    result = cf.run_once(limit=10)
    rows = cf.read_jsonl(cf.REPLAYS_JSONL)

    assert result["eligible_scanned"] == 1
    assert result["summary"]["eligible_count"] == 1
    assert result["summary"]["coverage_pct"] == 0.0
    assert rows[0]["status"] == "unresolved"
    assert rows[0]["eligible_reason"] == "rate_limited_universe_slice"

def test_counterfactual_phase10_backfilled_shadow_is_marked_not_online(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cf, "REPLAYS_JSONL", tmp_path / "counterfactual_replays.jsonl")
    monkeypatch.setattr(cf, "LATEST_JSON", tmp_path / "counterfactual_latest.json")
    monkeypatch.setattr(pes, "PAPER_ORDERS", tmp_path / "paper_orders.jsonl")
    signal = {
        "signal_id": "shadow_backfill_1",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": "100",
        "sl": "99",
        "tp": "101",
        "qty": "1",
        "backfilled": True,
        "first_computed_at": "2026-06-21T00:10:00+00:00",
        "source_available_at_max": "2026-06-21T00:10:00+00:00",
    }

    result = cf.replay_signal(signal, candles(), append=True)

    assert result["status"] == "complete"
    assert result["shadow_online"] is False
    assert result["first_computed_at"] == "2026-06-21T00:10:00+00:00"
    summary = cf.write_latest_summary(eligible_count=1, eligible_ids={"shadow_backfill_1"})
    assert summary["coverage_pct"] == 1.0
    assert summary["readiness_coverage_pct"] == 0.0
    assert summary["backfill_latest_count"] == 1

def test_counterfactual_phase10_runtime_candidate_without_cutoff_fails_closed(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cf, "REPLAYS_JSONL", tmp_path / "counterfactual_replays.jsonl")
    monkeypatch.setattr(cf, "LATEST_JSON", tmp_path / "counterfactual_latest.json")
    signal = {
        "signal_id": "missing_cutoff_1",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": "100",
        "sl": "99",
        "tp": "101",
        "qty": "1",
        "blocked": True,
        "eligible_source": "paper_candidate_census",
    }

    result = cf.replay_signal(signal, candles(), append=True)

    assert result["status"] == "unresolved"
    assert result["reason"] == "missing_replay_cutoff"

def test_counterfactual_phase10_paper_close_missing_cutoff_fails_closed(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cf, "REPLAYS_JSONL", tmp_path / "counterfactual_replays.jsonl")
    monkeypatch.setattr(cf, "LATEST_JSON", tmp_path / "counterfactual_latest.json")
    signal = {
        "signal_id": "paper_missing_cutoff_1",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": "100",
        "sl": "99",
        "tp": "101",
        "qty": "1",
        "eligible_source": "paper_close",
    }

    result = cf.replay_signal(signal, candles(), append=True)

    assert result["status"] == "unresolved"
    assert result["reason"] == "missing_replay_cutoff"

def test_counterfactual_phase10_late_ingested_or_finalized_data_is_blocked(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cf, "REPLAYS_JSONL", tmp_path / "counterfactual_replays.jsonl")
    monkeypatch.setattr(cf, "LATEST_JSON", tmp_path / "counterfactual_latest.json")
    signal = {
        "signal_id": "late_ingest_1",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": "100",
        "sl": "99",
        "tp": "101",
        "qty": "1",
        "trial_seq_cutoff": "2026-06-21T00:03:00+00:00",
    }
    replay_candles = [{**row, "known_at": row["ts"], "available_at": row["ts"], "ingested_at": row["ts"], "finalized_at": row["ts"]} for row in candles()]
    replay_candles[1]["ingested_at"] = "2026-06-21T00:04:00+00:00"
    replay_candles[2]["finalized_at"] = "2026-06-21T00:05:00+00:00"

    result = cf.replay_signal(signal, replay_candles, append=True)

    assert result["status"] == "unresolved"
    assert result["reason"] == "future_data_violation"
    assert any("future_ingested_at" in item or "future_finalized_at" in item for item in result["errors"])

def test_counterfactual_phase10_complete_signal_replays_when_source_signature_changes(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cf, "PAPER_TRADES_JSONL", tmp_path / "paper_trades.jsonl")
    monkeypatch.setattr(cf, "PAPER_BRAIN_HISTORY_JSONL", tmp_path / "paper_brain_history.jsonl")
    monkeypatch.setattr(cf, "PAPER_CANDIDATE_HISTORY_JSONL", tmp_path / "paper_candidate_history.jsonl")
    monkeypatch.setattr(cf, "REPLAYS_JSONL", tmp_path / "counterfactual_replays.jsonl")
    monkeypatch.setattr(cf, "LATEST_JSON", tmp_path / "counterfactual_latest.json")
    monkeypatch.setattr(cf, "HEARTBEAT_PATH", tmp_path / "counterfactual_heartbeat.json")
    monkeypatch.setattr(pes, "PAPER_ORDERS", tmp_path / "paper_orders.jsonl")
    row = {
        "event": "paper_close",
        "trade_id": "signature_1",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": "100",
        "exit": "101",
        "qty": "1",
        "sl": "99",
        "tp": "101",
        "net": "1",
        "source_available_at_max": "2026-06-21T00:03:00+00:00",
        "open_ts": "2026-06-21T00:00:00+00:00",
        "close_ts": "2026-06-21T00:03:00+00:00",
        "candles": candles(),
    }
    cf.PAPER_TRADES_JSONL.write_text(json.dumps(row) + "\n", encoding="utf-8")
    first = cf.run_once(limit=5)
    late = dict(row)
    late["source_available_at_max"] = "2026-06-21T00:01:00+00:00"
    late["trial_seq_cutoff"] = "2026-06-21T00:01:00+00:00"
    cf.PAPER_TRADES_JSONL.write_text(json.dumps(late) + "\n", encoding="utf-8")
    second = cf.run_once(limit=5)
    rows = cf.read_jsonl(cf.REPLAYS_JSONL)

    assert first["summary"]["coverage_pct"] == 1.0
    assert second["summary"]["coverage_pct"] == 0.0
    assert rows[-1]["status"] == "unresolved"
    assert rows[-1]["reason"] == "future_data_violation"
    assert rows[-1]["is_correction_event"] is True

def test_counterfactual_phase10_latest_summary_is_not_raw_order_dependent():
    older = {
        "signal_id": "shuffle_1",
        "replay_id": "old",
        "status": "unresolved",
        "reason": "insufficient_candle_coverage",
        "created_at": "2026-06-21T00:00:00+00:00",
        "created_at_ns": 1,
    }
    newer = {
        "signal_id": "shuffle_1",
        "replay_id": "new",
        "status": "complete",
        "conclusion": "no_change",
        "created_at": "2026-06-21T00:01:00+00:00",
        "created_at_ns": 2,
    }

    summary = cf.summarize_replays([newer, older], eligible_count=1, eligible_ids={"shuffle_1"})

    assert summary["coverage_pct"] == 1.0
    assert summary["latest_complete_count"] == 1
    assert summary["latest_unresolved_count"] == 0

def test_counterfactual_phase10_future_data_violation_is_unresolved(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cf, "REPLAYS_JSONL", tmp_path / "counterfactual_replays.jsonl")
    monkeypatch.setattr(cf, "LATEST_JSON", tmp_path / "counterfactual_latest.json")
    signal = {
        "signal_id": "future_1",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": "100",
        "sl": "99",
        "tp": "101",
        "qty": "1",
        "trial_seq_cutoff": "2026-06-21T00:01:30+00:00",
    }
    future_candles = [
        {**row, "known_at": "2026-06-21T00:01:00+00:00", "available_at": "2026-06-21T00:01:00+00:00"}
        for row in candles()
    ]
    future_candles[-1]["available_at"] = "2026-06-21T00:05:00+00:00"

    result = cf.replay_signal(signal, future_candles, append=True)

    assert result["status"] == "unresolved"
    assert result["reason"] == "future_data_violation"
    assert any("future_" in item for item in result["errors"])

def test_counterfactual_phase10_unresolved_to_complete_correction_updates_latest(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cf, "PAPER_TRADES_JSONL", tmp_path / "paper_trades.jsonl")
    monkeypatch.setattr(cf, "PAPER_BRAIN_HISTORY_JSONL", tmp_path / "paper_brain_history.jsonl")
    monkeypatch.setattr(cf, "PAPER_CANDIDATE_HISTORY_JSONL", tmp_path / "paper_candidate_history.jsonl")
    monkeypatch.setattr(cf, "REPLAYS_JSONL", tmp_path / "counterfactual_replays.jsonl")
    monkeypatch.setattr(cf, "LATEST_JSON", tmp_path / "counterfactual_latest.json")
    monkeypatch.setattr(cf, "HEARTBEAT_PATH", tmp_path / "counterfactual_heartbeat.json")
    monkeypatch.setattr(mdl, "MARKET_CACHE_DIR", tmp_path / "market_cache")
    monkeypatch.setattr(pes, "PAPER_ORDERS", tmp_path / "paper_orders.jsonl")

    row = {
        "event": "paper_close",
        "trade_id": "correction_1",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": "100",
        "exit": "101",
        "qty": "1",
        "sl": "99",
        "tp": "101",
        "net": "1",
        "data_quality": "mark_only_snapshot",
        "open_ts": "2026-06-21T00:00:00+00:00",
        "close_ts": "2026-06-21T00:03:00+00:00",
    }
    cf.PAPER_TRADES_JSONL.write_text(json.dumps(row) + "\n", encoding="utf-8")
    first = cf.run_once(limit=5)
    cached = mdl.store_candles("BTCUSDT", "1m", candles(), source_id="test")
    row["data_quality"] = "mark_sequence"
    row["candle_cache_id"] = cached["cache_id"]
    cf.PAPER_TRADES_JSONL.write_text(json.dumps(row) + "\n", encoding="utf-8")
    second = cf.run_once(limit=5)

    assert first["summary"]["coverage_pct"] == 0.0
    assert second["summary"]["coverage_pct"] == 1.0
    assert second["summary"]["correction_count"] == 1
    assert second["summary"]["signal_count"] == 1
    rows = cf.read_jsonl(cf.REPLAYS_JSONL)
    complete = [row for row in rows if row["status"] == "complete"][0]
    assert complete["is_correction_event"] is True
    assert complete["previous_status"] == "unresolved"
    assert complete["supersedes_replay_id"]

def test_derivatives_missing_data_degrades_confidence(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dob, "DERIVATIVES_LATEST", tmp_path / "derivatives_latest.json")

    result = dob.evaluate_derivatives("BTCUSDT")

    assert result["status"] == "degraded_missing_derivatives"
    assert result["confidence"] < 0.55


def test_orderbook_spread_spike_blocks_paper_entry(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(obo, "ORDERBOOK_LATEST", tmp_path / "orderbook.json")

    result = obo.evaluate_orderbook("BTCUSDT", bids=[[100, 10]], asks=[[101, 10]], max_spread_bps=5)

    assert result["paper_entry_allowed"] is False
    assert "spread_spike" in result["warnings"]


def test_liquidation_burst_has_replayable_event_ids(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(lob, "LIQUIDATIONS_LATEST", tmp_path / "liquidations.json")
    monkeypatch.setattr(lob, "LIQUIDATION_EVENTS", tmp_path / "liquidation_events.jsonl")

    result = lob.aggregate_liquidations("BTCUSDT", [{"ts": "2026-06-21T00:00:00+00:00", "side": "LONG", "notional": 2_000_000}], burst_threshold_notional=1_000_000)

    assert result["burst"] is True
    assert result["event_ids"][0].startswith("liq_")


def test_paper_exploration_is_tiny_and_paper_only(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(pep, "EXPLORATION_LATEST", tmp_path / "exploration.json")

    result = pep.evaluate_exploration_request({"symbol": "BTCUSDT", "confidence": 0.5, "margin": 20}, {"equity": "100"}, output_path=tmp_path / "exploration.json")

    assert result["allowed"] is True
    assert result["approved_margin"] == 2.0
    assert "margin_reduced_to_exploration_cap" in result["warnings"]
    assert result["gate_change_allowed"] is False


def test_learning_dashboard_payload_contains_phase_b_sections(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ldd, "STATE_DIR", tmp_path)
    monkeypatch.setattr(ldd, "MEMORY_DIR", tmp_path / "agent_memory")
    (tmp_path / "agent_memory").mkdir()

    payload = ldd.load_phase_b_learning()

    assert set(["lifecycle", "post_trade", "counterfactual", "microstructure", "exploration"]).issubset(payload.keys())
