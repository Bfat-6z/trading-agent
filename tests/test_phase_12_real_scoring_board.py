from pathlib import Path

import promotion_board as pb
import real_scoring_board as rsb
import setup_ranker

def trade(
    idx: int,
    net: float,
    *,
    setup_id: str = "edge",
    regime: str = "risk_on",
    symbol: str = "BTCUSDT",
    side: str = "LONG",
    source: str = "paper",
    fee: float = 0.01,
    funding: float = 0.0,
    slippage: float = 0.001,
    mae: float | None = -0.1,
    mfe: float | None = 0.2,
    setup_contract_hash: str = "contract_v1",
    capability: dict | None = None,
    source_trust: float = 1.0,
    capital_event_id: str = "epoch1",
) -> dict:
    ts = f"2026-06-{idx:02d}T00:00:00+00:00"
    return {
        "event_seq": idx,
        "trade_id": f"t{idx}",
        "setup_id": setup_id,
        "setup_contract_hash": setup_contract_hash,
        "market_regime": regime,
        "symbol": symbol,
        "side": side,
        "source": source,
        "source_trust": source_trust,
        "gross": net + fee + funding + slippage,
        "net": net,
        "fee": fee,
        "funding_payment": funding,
        "slippage": slippage,
        "mae": mae,
        "mfe": mfe,
        "entry": 100,
        "close_ts": ts,
        "outcome_known_at": ts,
        "capital_event_id": capital_event_id,
        "decision_data_capability_mask": capability or {"required_present": True},
    }

def test_real_scoring_golden_trade_series_exact_payload():
    rows = [trade(i, 1.0) for i in range(1, 31)]

    result = rsb.score_all(rows, as_of="2026-07-01T00:00:00+00:00", report_cutoff="2026-07-01T00:00:00+00:00")

    assert result["overall"]["trades"] == 30
    assert result["overall"]["expectancy_after_costs"] == 1.0
    assert result["overall"]["profit_factor_after_costs"] == 999.0
    assert result["overall"]["max_drawdown"] == 0.0
    assert result["overall"]["expectancy_lower_bound_95"] == 1.0
    assert result["overall"]["win_rate_diagnostic_only"] is True
    assert result["metric_manifest"]["digest"] == result["metric_manifest_digest"]
    assert result["snapshot_hash"].startswith("score_")
    assert result["can_place_live_orders"] is False

def test_real_scoring_high_winrate_negative_expectancy_fails():
    rows = [trade(i, 1.0) for i in range(1, 10)] + [trade(10, -20.0)]

    result = rsb.score_all(rows, as_of="2026-07-01T00:00:00+00:00", report_cutoff="2026-07-01T00:00:00+00:00")

    assert result["overall"]["win_rate"] == 0.9
    assert result["overall"]["expectancy_after_costs"] < 0
    assert "expectancy_not_positive_after_costs" in result["hard_errors"]
    assert result["passed"] is False

def test_real_scoring_tiny_sample_fails_uncertainty_gate():
    result = rsb.score_all([trade(1, 10.0), trade(2, 10.0)], as_of="2026-07-01T00:00:00+00:00", report_cutoff="2026-07-01T00:00:00+00:00")

    assert result["overall"]["profit_factor_after_costs"] == 999.0
    assert "effective_n_below_gate" in result["hard_errors"]
    assert result["passed"] is False

def test_real_scoring_good_global_bad_setup_bucket_fails():
    rows = [trade(i, 1.0, setup_id="good") for i in range(1, 26)]
    rows.extend(trade(i, -2.0, setup_id="bad") for i in range(26, 31))

    result = rsb.score_all(rows, as_of="2026-07-01T00:00:00+00:00", report_cutoff="2026-07-01T00:00:00+00:00")

    assert result["overall"]["expectancy_after_costs"] > 0
    assert result["by_setup"]["bad"]["expectancy_after_costs"] < 0
    assert "setup_bucket_failed:bad" in result["hard_errors"]

def test_real_scoring_fee_funding_slippage_omission_fails():
    row = trade(1, 1.0)
    row.pop("fee")

    result = rsb.score_all([row], as_of="2026-07-01T00:00:00+00:00", report_cutoff="2026-07-01T00:00:00+00:00")

    assert "execution_cost_completeness_missing" in result["hard_errors"]

def test_real_scoring_shadow_disagreement_blocks_readiness():
    paper = [trade(i, 1.0) for i in range(1, 25)]
    shadow = [{**trade(i, 1.0), "trade_id": f"t{i}", "entry": 104 if i == 1 else 100} for i in range(1, 25)]

    result = rsb.score_all(paper, shadow_rows=shadow, as_of="2026-07-01T00:00:00+00:00", report_cutoff="2026-07-01T00:00:00+00:00")

    assert result["paper_shadow_concordance"]["passed"] is False
    assert "shadow_fill_error_above_tolerance" in result["hard_errors"]

def test_real_scoring_blind_capability_profit_fails():
    rows = [trade(i, 1.0, capability={"required_missing": True}) for i in range(1, 25)]

    result = rsb.score_all(rows, as_of="2026-07-01T00:00:00+00:00", report_cutoff="2026-07-01T00:00:00+00:00")

    assert "blind_capability_trades_present" in result["hard_errors"]
    assert result["overall"]["blind_trade_count"] == 24

def test_real_scoring_stress_breach_blocks_readiness():
    rows = [trade(i, 1.0) for i in range(1, 25)]

    result = rsb.score_all(rows, exposures={"portfolio_beta": 3, "cluster_concentration": 0.9}, as_of="2026-07-01T00:00:00+00:00", report_cutoff="2026-07-01T00:00:00+00:00")

    assert result["stress_score"]["passed"] is False
    assert "stress_loss_breaches_gate" in result["hard_errors"]

def test_real_scoring_late_outcome_creates_correction_snapshot():
    previous = {"snapshot_id": "score_old", "included_event_seq_max": 10}
    row = trade(5, 1.0)

    result = rsb.score_all([row], previous_snapshot=previous, as_of="2026-07-02T00:00:00+00:00", report_cutoff="2026-07-02T00:00:00+00:00")

    assert result["correction_of_snapshot"] == "score_old"
    assert result["correction_reason"] == "late_or_corrected_outcome_in_prior_seq_range"

def test_real_scoring_spend_adjusted_negative_blocks_profit_wording():
    rows = [trade(i, 1.0) for i in range(1, 25)]

    result = rsb.score_all(rows, operating_costs={"llm": 1000}, as_of="2026-07-01T00:00:00+00:00", report_cutoff="2026-07-01T00:00:00+00:00")

    assert result["spend_adjusted_expectancy"] < 0
    assert "spend_adjusted_expectancy_negative" in result["hard_errors"]

def test_real_scoring_capital_event_splits_windows():
    rows = [trade(i, 1.0, capital_event_id="before_deposit") for i in range(1, 10)]
    rows.extend(trade(i, 2.0, capital_event_id="after_deposit") for i in range(10, 25))

    result = rsb.score_all(rows, as_of="2026-07-01T00:00:00+00:00", report_cutoff="2026-07-01T00:00:00+00:00")

    assert set(result["by_capital_event"]) == {"before_deposit", "after_deposit"}

def test_real_scoring_candidate_census_and_universe_gaps_present():
    candidates = [{"candidates": [{"state": "missed"}, {"state": "selected"}, {"state": "expired"}]}]

    result = rsb.score_all([trade(i, 1.0) for i in range(1, 25)], candidate_rows=candidates, as_of="2026-07-01T00:00:00+00:00", report_cutoff="2026-07-01T00:00:00+00:00")

    assert result["candidate_census"]["seen"] == 3
    assert result["candidate_census"]["missed_candidate_rate"] > 0

def test_real_scoring_run_once_scores_only_closed_paper_rows(tmp_path: Path):
    paper_path = tmp_path / "paper_trades.jsonl"
    shadow_path = tmp_path / "shadow.jsonl"
    candidate_path = tmp_path / "candidates.jsonl"
    latest_path = tmp_path / "latest.json"
    history_path = tmp_path / "history.jsonl"
    costs_path = tmp_path / "costs.json"
    rsb.append_jsonl(paper_path, {"event": "paper_open", "trade_id": "open1", "ts": "2026-06-01T00:00:00+00:00", "fee": 0.01, "funding_payment": 0, "slippage": 0})
    rsb.append_jsonl(paper_path, {**trade(1, 1.0), "event": "paper_close", "qty": "1"})
    rsb.write_json_atomic(costs_path, {"costs": {"llm": 0.1, "compute": 0.1}})

    result = rsb.run_once(paper_path, shadow_path, candidate_path, latest_path, history_path, costs_path)

    assert result["overall"]["trades"] == 1
    assert result["scored_closed_rows"] == 1
    assert result["operating_costs"] == {"llm": 0.1, "compute": 0.1}

def test_real_scoring_no_false_correction_without_event_seq():
    previous = {"snapshot_id": "score_old", "included_event_seq_max": 0}
    row = trade(1, 1.0)
    row.pop("event_seq")

    result = rsb.score_all([row], previous_snapshot=previous, as_of="2026-07-02T00:00:00+00:00", report_cutoff="2026-07-02T00:00:00+00:00")

    assert "correction_of_snapshot" not in result
    assert result["included_event_seq_max"] is None

def test_real_scoring_shadow_nested_signal_keys_match_without_id_parity():
    paper = [{**trade(i, 1.0), "risk_decision_id": f"r{i}", "candidate_id": f"c{i}"} for i in range(1, 25)]
    shadow = [
        {
            "event": "shadow_close",
            "shadow_id": f"s{i}",
            "entry": 100,
            "close_ts": f"2026-06-{i:02d}T00:00:00+00:00",
            "fees": 0.01,
            "funding_payment": 0,
            "slippage": 0.001,
            "net": 1,
            "signal": {"risk_decision_id": f"r{i}", "candidate_id": f"c{i}", "setup_id": "edge", "symbol": "BTCUSDT", "side": "LONG", "regime": "risk_on"},
        }
        for i in range(1, 25)
    ]

    result = rsb.score_all(paper, shadow_rows=shadow, as_of="2026-07-01T00:00:00+00:00", report_cutoff="2026-07-01T00:00:00+00:00")

    assert result["paper_shadow_concordance"]["matched"] == 24
    assert result["paper_shadow_concordance"]["passed"] is True
    assert "shadow_unmatched_rate_above_tolerance" not in result["hard_errors"]

def test_real_scoring_unmatched_shadow_corpus_blocks_readiness():
    shadow = [{"event": "shadow_close", "shadow_id": "shadow_only", "entry": 100, "close_ts": "2026-06-01T00:00:00+00:00", "net": 1, "fees": 0.01, "funding_payment": 0, "slippage": 0}]

    result = rsb.score_all([trade(i, 1.0) for i in range(1, 25)], shadow_rows=shadow, as_of="2026-07-01T00:00:00+00:00", report_cutoff="2026-07-01T00:00:00+00:00")

    assert result["paper_shadow_concordance"]["matched"] == 0
    assert result["paper_shadow_concordance"]["passed"] is False
    assert "shadow_unmatched_rate_above_tolerance" in result["hard_errors"]

def test_real_scoring_no_shadow_corpus_does_not_fail_concordance():
    result = rsb.score_all([trade(i, 1.0) for i in range(1, 25)], shadow_rows=[], as_of="2026-07-01T00:00:00+00:00", report_cutoff="2026-07-01T00:00:00+00:00")

    assert result["paper_shadow_concordance"]["mode"] == "no_shadow_corpus"
    assert result["paper_shadow_concordance"]["passed"] is True
    assert "shadow_unmatched_rate_above_tolerance" not in result["hard_errors"]

def test_real_scoring_run_once_fails_closed_when_operating_cost_file_missing(tmp_path: Path):
    paper_path = tmp_path / "paper_trades.jsonl"
    rsb.append_jsonl(paper_path, {**trade(1, 1.0), "event": "paper_close", "qty": "1"})

    result = rsb.run_once(paper_path, tmp_path / "shadow.jsonl", tmp_path / "candidates.jsonl", tmp_path / "latest.json", tmp_path / "history.jsonl", tmp_path / "missing_costs.json")

    assert "operating_costs_missing" in result["hard_errors"]

def test_promotion_board_blocks_failed_real_scoring(tmp_path: Path, monkeypatch):
    memory = tmp_path / "agent_memory"
    memory.mkdir()
    monkeypatch.setattr(pb, "MEMORY_DIR", memory)
    monkeypatch.setattr(pb, "REAL_SCORING_LATEST", memory / "real_scoring_board_latest.json")
    rsb.write_json_atomic(pb.REAL_SCORING_LATEST, {"passed": False, "snapshot_id": "score_bad", "hard_errors": ["expectancy_lcb_not_positive"], "metric_manifest_digest": "m", "metric_manifest": {"digest": "m"}})

    result = pb.evaluate_from_state(output_path=tmp_path / "promotion.json")

    assert "real_scoring_hard_gate_failed" in result["failures"]

def test_promotion_board_requires_real_scoring_from_state(tmp_path: Path, monkeypatch):
    memory = tmp_path / "agent_memory"
    memory.mkdir()
    monkeypatch.setattr(pb, "MEMORY_DIR", memory)
    monkeypatch.setattr(pb, "REAL_SCORING_LATEST", memory / "real_scoring_board_latest.json")
    monkeypatch.setattr(pb, "DAILY_EXAM_HISTORY", memory / "daily_exam_history.jsonl")

    result = pb.evaluate_from_state(output_path=tmp_path / "promotion.json")

    assert "real_scoring_missing" in result["failures"]

def test_promotion_board_blocks_stale_real_scoring_snapshot(tmp_path: Path, monkeypatch):
    state = tmp_path
    memory = tmp_path / "agent_memory"
    memory.mkdir()
    monkeypatch.setattr(pb, "STATE_DIR", state)
    monkeypatch.setattr(pb, "MEMORY_DIR", memory)
    monkeypatch.setattr(pb, "PAPER_TRADES_PATH", memory / "paper_trades.jsonl")
    monkeypatch.setattr(pb, "REAL_SCORING_LATEST", memory / "real_scoring_board_latest.json")
    monkeypatch.setattr(pb, "DAILY_EXAM_HISTORY", memory / "daily_exam_history.jsonl")
    rsb.write_json_atomic(state / "paper_account.json", {"created_at": "2026-06-24T00:00:00+00:00", "closed_trades": 1})
    rsb.append_jsonl(pb.PAPER_TRADES_PATH, {"event": "paper_close", "trade_id": "t1", "close_ts": "2026-06-24T01:00:00+00:00", "qty": "1", "net": "1"})
    rsb.write_json_atomic(
        pb.REAL_SCORING_LATEST,
        {"passed": True, "snapshot_id": "score_old", "as_of": "2026-01-01T00:00:00+00:00", "report_cutoff": "2026-01-01T00:00:00+00:00", "hard_errors": [], "overall": {"trades": 1}, "metric_manifest_digest": "m", "metric_manifest": {"digest": "m"}},
    )

    result = pb.evaluate_from_state(output_path=tmp_path / "promotion.json")

    assert "real_scoring_stale" in result["failures"]
    assert "real_scoring_before_account_reset" in result["failures"]

def test_setup_ranker_uses_real_scoring_for_allocation_gate(tmp_path: Path):
    library = {"skills": {"edge": {"stats": {"trades": 50, "expectancy": 0.2, "profit_factor": 2.0, "win_rate": 0.6}}}}
    scoring = {
        "snapshot_id": "score_bad",
        "passed": False,
        "metric_manifest_digest": "m",
        "hard_errors": ["setup_bucket_failed:edge"],
        "by_setup": {
            "edge": {
                "trades": 50,
                "expectancy_after_costs": -0.05,
                "expectancy_lower_bound_95": -0.1,
                "profit_factor_after_costs": 0.8,
                "win_rate": 0.6,
                "max_drawdown": 1,
                "effective_sample": {"effective_n": 50},
                "cost_completeness": True,
            }
        },
    }

    rows = setup_ranker.build_setup_evidence_rows(library, reviews=[], replays=[], shadow_rows=[], real_scoring=scoring)
    ranked = setup_ranker.rank_setups(rows, output_path=tmp_path / "rankings.json")["rankings"][0]

    assert ranked["evidence_expectancy"] == -0.05
    assert ranked["allocation_hint"] == "reduced"
    assert "real_scoring_failed" in ranked["rank_reasons"]
    assert "real_scoring_lcb_non_positive" in ranked["rank_reasons"]

def test_setup_ranker_reduces_global_real_scoring_failure_even_if_setup_stats_look_good(tmp_path: Path):
    rows = [
        {
            "setup_id": "edge",
            "trades": 50,
            "expectancy_after_costs": 0.05,
            "profit_factor_after_costs": 1.4,
            "win_rate": 0.6,
            "real_scoring_snapshot_id": "score_bad",
            "real_scoring_passed": False,
            "real_scoring_hard_errors": ["stress_loss_breaches_gate"],
            "effective_sample": {"effective_n": 50},
            "expectancy_lower_bound_95": 0.01,
            "cost_completeness": True,
        }
    ]

    ranked = setup_ranker.rank_setups(rows, output_path=tmp_path / "rankings.json")["rankings"][0]

    assert ranked["allocation_hint"] == "reduced"
    assert "real_scoring_failed" in ranked["rank_reasons"]

def test_setup_ranker_applies_global_real_scoring_failure_to_missing_setup_bucket(tmp_path: Path):
    library = {"skills": {"new_setup": {"stats": {"trades": 50, "expectancy": 0.2, "profit_factor": 2.0, "win_rate": 0.7}}}}
    scoring = {
        "snapshot_id": "score_global_bad",
        "passed": False,
        "metric_manifest_digest": "m",
        "hard_errors": ["stress_loss_breaches_gate"],
        "by_setup": {},
    }

    rows = setup_ranker.build_setup_evidence_rows(library, reviews=[], replays=[], shadow_rows=[], real_scoring=scoring)
    ranked = setup_ranker.rank_setups(rows, output_path=tmp_path / "rankings.json")["rankings"][0]

    assert ranked["real_scoring_snapshot_id"] == "score_global_bad"
    assert ranked["allocation_hint"] == "reduced"
    assert "real_scoring_failed" in ranked["rank_reasons"]

def test_setup_ranker_hard_real_scoring_failure_dominates_low_effective_n(tmp_path: Path):
    rows = [
        {
            "setup_id": "edge",
            "trades": 50,
            "expectancy_after_costs": 0.05,
            "profit_factor_after_costs": 1.4,
            "win_rate": 0.6,
            "real_scoring_snapshot_id": "score_bad",
            "real_scoring_passed": False,
            "real_scoring_hard_errors": ["stress_loss_breaches_gate"],
            "effective_sample": {"effective_n": 3},
            "expectancy_lower_bound_95": 0.01,
            "cost_completeness": True,
        }
    ]

    ranked = setup_ranker.rank_setups(rows, output_path=tmp_path / "rankings.json")["rankings"][0]

    assert ranked["allocation_hint"] == "reduced"
    assert ranked["risk_multiplier"] == 0.35
    assert "real_scoring_failed" in ranked["rank_reasons"]

def test_promotion_board_uses_rolling_daily_exam_average(tmp_path: Path, monkeypatch):
    memory = tmp_path / "agent_memory"
    memory.mkdir()
    monkeypatch.setattr(pb, "MEMORY_DIR", memory)
    monkeypatch.setattr(pb, "DAILY_EXAM_HISTORY", memory / "daily_exam_history.jsonl")
    monkeypatch.setattr(pb, "REAL_SCORING_LATEST", memory / "real_scoring_board_latest.json")
    rsb.write_json_atomic(pb.REAL_SCORING_LATEST, {"passed": True, "snapshot_id": "score_ok", "hard_errors": [], "metric_manifest_digest": "m", "metric_manifest": {"digest": "m"}})
    for score in [100, 50, 50, 100]:
        rsb.append_jsonl(pb.DAILY_EXAM_HISTORY, {"quality_score": score})

    result = pb.evaluate_from_state(output_path=tmp_path / "promotion.json")

    assert result["metrics"]["daily_exam_avg"] == 75.0
    assert result["metrics"]["daily_exam_rolling_window"] == 4
