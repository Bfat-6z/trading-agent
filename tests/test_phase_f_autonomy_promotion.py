import json
from pathlib import Path

import pytest

import alert_manager as am
import autonomous_paper_trading_brain as brain
import host_runtime_monitor as hrm
import portfolio_correlation_guard as pcg
import promotion_board as pb
import risk_of_ruin_model as ror


@pytest.fixture(autouse=True)
def host_runtime_ok(monkeypatch):
    monkeypatch.setattr(brain, "paper_opens_paused_by_runtime", lambda: {"paused": False, "reason": "ok", "replay_required": False, "promotion_window_valid": True})


def test_portfolio_correlation_guard_blocks_btc_beta_concentration(tmp_path: Path):
    positions = [{"symbol": "BTCUSDT", "side": "LONG", "notional": 200, "estimated_loss": 1}]

    result = pcg.evaluate_portfolio_risk(positions, equity=100, output_path=tmp_path / "portfolio.json")

    assert result["status"] == "critical"
    assert "btc_beta_concentration" in result["errors"]


def test_risk_of_ruin_worsens_with_bad_edge():
    result = ror.estimate_risk_of_ruin(win_rate=0.2, avg_win=1, avg_loss=2, risk_fraction=0.05, losing_streak=3)

    assert result["edge"] < 0
    assert result["status"] == "critical"


def test_promotion_board_fails_closed_until_requirements_met(tmp_path: Path):
    result = pb.evaluate_promotion({"paper_trades": 10, "shadow_closes": 10, "lifecycle_completeness": 1.0, "daily_exam_avg": 90, "trial_days": 1}, output_path=tmp_path / "promotion.json")

    assert result["passed"] is False
    assert result["state"] == "paper_learning"
    assert result["can_place_live_orders"] is False


def test_promotion_board_counts_only_validated_paper_closes_after_reset(tmp_path: Path):
    trades = tmp_path / "paper_trades.jsonl"
    trades.write_text(
        "\n".join(
            [
                '{"event":"paper_close","trade_id":"old","close_ts":"2026-06-23T23:59:00+00:00","qty":"1","net":"5"}',
                '{"event":"paper_close","trade_id":"new1","close_ts":"2026-06-24T04:26:00+00:00","qty":"1","net":"1"}',
                '{"event":"paper_close","trade_id":"new2","close_ts":"2026-06-24T04:27:00+00:00","qty":"1","net":"-0.5"}',
                '{"event":"paper_close","trade_id":"new2","close_ts":"2026-06-24T04:27:00+00:00","qty":"1","net":"-0.5"}',
                '{"event":"paper_open","trade_id":"open","open_ts":"2026-06-24T04:28:00+00:00","position":{"qty":"1"}}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    count = pb.validated_paper_closes_since_reset({"created_at": "2026-06-24T04:25:34+00:00"}, path=trades)

    assert count == 2


def test_promotion_board_excludes_quarantined_paper_closes(tmp_path: Path):
    trades = tmp_path / "paper_trades.jsonl"
    quarantine = tmp_path / "trade_lifecycle_quarantine.jsonl"
    trades.write_text(
        "\n".join(
            [
                '{"event":"paper_close","trade_id":"clean","close_ts":"2026-06-24T04:26:00+00:00","qty":"1","net":"1"}',
                '{"event":"paper_close","trade_id":"bad","close_ts":"2026-06-24T04:27:00+00:00","qty":"1","net":"-1"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    quarantine.write_text(
        '{"quarantine_id":"q_bad","trade_id":"bad","scope":"trade","status":"active","reason":"known_stale_market_snapshot_pre_guard"}\n',
        encoding="utf-8",
    )

    count = pb.validated_paper_closes_since_reset(
        {"created_at": "2026-06-24T04:25:34+00:00"},
        path=trades,
        quarantine_path=quarantine,
    )

    assert count == 1


def test_promotion_board_passes_live_review_candidate_without_live_orders(tmp_path: Path):
    result = pb.evaluate_promotion({"paper_trades": 300, "shadow_closes": 1000, "lifecycle_completeness": 0.995, "daily_exam_avg": 85, "trial_days": 14, "portfolio_risk_status": "ok"}, output_path=tmp_path / "promotion.json")

    assert result["passed"] is True
    assert result["state"] == "live_review_candidate"
    assert result["can_place_live_orders"] is False

def test_promotion_board_blocks_running_walk_forward_patch(tmp_path: Path):
    result = pb.evaluate_promotion(
        {
            "paper_trades": 300,
            "shadow_closes": 1000,
            "lifecycle_completeness": 0.995,
            "daily_exam_avg": 85,
            "trial_days": 14,
            "portfolio_risk_status": "ok",
            "walk_forward_required": True,
            "walk_forward_status": "running",
            "walk_forward_running": 1,
            "walk_forward_passed": 0,
            "walk_forward_failed": 0,
            "active_skill_patches": 1,
        },
        output_path=tmp_path / "promotion.json",
    )

    assert result["passed"] is False
    assert result["state"] == "paper_learning"
    assert "walk_forward_validation_running" in result["failures"]
    assert "walk_forward_not_all_patches_passed" in result["failures"]
    assert result["can_place_live_orders"] is False

def test_promotion_board_requires_active_patch_identity_match(tmp_path: Path):
    metrics = {
        "paper_trades": 300,
        "shadow_closes": 1000,
        "lifecycle_completeness": 0.995,
        "daily_exam_avg": 85,
        "trial_days": 14,
        "portfolio_risk_status": "ok",
        **pb.walk_forward_metrics(
            {
                "experiment_count": 1,
                "by_status": {"passed": 1},
                "rows": [{"patch_id": "old_patch", "status": "passed"}],
            },
            ["new_patch"],
        ),
    }

    result = pb.evaluate_promotion(metrics, output_path=tmp_path / "promotion.json")

    assert result["passed"] is False
    assert result["state"] == "paper_learning"
    assert result["metrics"]["walk_forward_status"] == "missing"
    assert result["metrics"]["walk_forward_missing_patch_ids"] == ["new_patch"]
    assert "walk_forward_missing" in result["failures"]
    assert result["can_place_live_orders"] is False


def test_alert_manager_redacts_and_dedupes(tmp_path: Path):
    latest = tmp_path / "alerts_latest.json"
    history = tmp_path / "alerts.jsonl"

    first = am.emit_alert("critical", "API_KEY=SECRET123456789012345678901234567890", "password=abc", history_path=history, latest_path=latest)
    second = am.emit_alert("critical", "API_KEY=SECRET123456789012345678901234567890", "password=abc", history_path=history, latest_path=latest)

    assert "SECRET" not in first["latest"]["title"]
    assert second["last_inserted"] is False


def test_host_runtime_reports_status(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(hrm, "ROOT", tmp_path)

    result = hrm.check_host_runtime(min_free_gb=0, output_path=tmp_path / "host.json")

    assert result["status"] in {"ok", "warn"}
    assert result["free_disk_gb"] >= 0


def test_autonomous_paper_brain_is_paper_only(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(brain, "BRAIN_LATEST", tmp_path / "brain.json")
    monkeypatch.setattr(brain, "BRAIN_HISTORY", tmp_path / "brain.jsonl")
    monkeypatch.setattr(brain, "run_preflight", lambda *args, **kwargs: {"allowed": True, "errors": []})
    monkeypatch.setattr(brain, "evaluate_candidate", lambda candidate: {"blocked": False, "action": "allow"})
    monkeypatch.setattr(brain, "evaluate_paper_order", lambda *args, **kwargs: {"can_open_paper": True, "errors": [], "risk_decision_id": "r1"})

    decision = brain.decide_paper_action(
        [{"symbol": "BTCUSDT", "side": "LONG", "setup_id": "good", "score": 9, "entry": 100, "sl": 99, "tp": 102, "leverage": 2}],
        [{"setup_id": "good", "trades": 60, "expectancy": 0.05, "profit_factor": 1.5, "win_rate": 0.55}],
        {"equity": "100", "cash": "100"},
    )

    assert decision["action"] == "paper_open_candidate"
    assert decision["can_place_live_orders"] is False

def test_autonomous_paper_brain_sizes_futures_margin_from_risk_budget(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(brain, "BRAIN_LATEST", tmp_path / "brain.json")
    monkeypatch.setattr(brain, "BRAIN_HISTORY", tmp_path / "brain.jsonl")
    monkeypatch.setattr(brain, "run_preflight", lambda *args, **kwargs: {"allowed": True, "errors": []})
    monkeypatch.setattr(brain, "evaluate_candidate", lambda candidate: {"blocked": False, "action": "allow"})

    decision = brain.decide_paper_action(
        [{"symbol": "BTCUSDT", "side": "LONG", "setup_id": "new_good", "score": 9, "entry": 100, "sl": 90, "tp": 120, "leverage": 2}],
        [{"setup_id": "new_good", "trades": 5, "expectancy": 0.05, "profit_factor": 1.5, "win_rate": 0.55}],
        {"equity": "100", "cash": "100"},
        exploration_allowed=True,
    )

    risk = decision["risk_decision"]
    assert decision["action"] == "paper_open_candidate"
    assert float(risk["paper_sizing"]["risk_budget_usdt"]) == 3.187
    assert float(risk["paper_sizing"]["requested_leverage"]) == 15.0
    assert float(risk["margin"]) == 2.124666
    assert float(risk["notional"]) == 31.86999
    assert risk["can_open_paper"] is True
    assert risk["paper_sizing"]["method"] == "risk_budget_to_isolated_margin"
    assert risk["paper_sizing"]["leverage_factors"]["mode"] == "adaptive_paper_leverage"

def test_autonomous_paper_brain_can_select_50x_for_high_conviction_normal_paper(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(brain, "BRAIN_LATEST", tmp_path / "brain.json")
    monkeypatch.setattr(brain, "BRAIN_HISTORY", tmp_path / "brain.jsonl")
    monkeypatch.setattr(brain, "PAPER_RISK_STATE", tmp_path / "risk.json")
    monkeypatch.setattr(brain, "run_preflight", lambda *args, **kwargs: {"allowed": True, "errors": []})
    monkeypatch.setattr(brain, "evaluate_candidate", lambda candidate: {"blocked": False, "action": "allow"})
    monkeypatch.setattr(
        brain,
        "rank_setups",
        lambda rows: {
            "rankings": [
                {
                    "setup_id": "aplus_pure",
                    "under_sampled": False,
                    "evidence_expectancy": 0.07,
                    "rank_score": 2.0,
                    "sample_confidence": 1.0,
                    "allocation_hint": "normal",
                    "risk_multiplier": 1.0,
                    "rank_reasons": ["high_expectancy", "stable_parameters"],
                }
            ]
        },
    )

    decision = brain.decide_paper_action(
        [{"symbol": "BTCUSDT", "side": "LONG", "setup_id": "aplus_pure", "score": 10, "entry": 100, "sl": 99, "tp": 102}],
        [],
        {"equity": "100", "cash": "100", "open_margin": "0"},
    )

    risk = decision["risk_decision"]
    assert decision["action"] == "paper_open_candidate"
    assert decision["allocation"]["tier"] == "normal_paper"
    assert decision["allocation"]["risk_fraction"] == 0.05
    assert float(risk["leverage"]) == 50.0
    assert float(risk["margin"]) == 10.0
    assert float(risk["notional"]) == 500.0
    assert float(risk["estimated_loss"]) == 5.0
    assert risk["paper_sizing"]["leverage_factors"]["high_conviction_boost"] == 21.0
    assert risk["paper_sizing"]["risk_policy_id"] == "paper_risk_policy_v1"
    assert risk["paper_sizing"]["initial_margin"] == 10.0
    assert risk["paper_sizing"]["maintenance_margin"] == 2.5
    assert risk["paper_sizing"]["risk_at_stop"] == 5.0
    assert risk["paper_sizing"]["fee_to_close_reserve"] == 0.25
    assert risk["paper_sizing"]["funding_reserve"] == 0.25
    assert risk["paper_sizing"]["gap_loss_estimate"] == 1.25
    assert risk["paper_sizing"]["liquidation_distance_fraction"] == 0.015
    assert risk["paper_sizing"]["errors"] == []
    assert decision["can_place_live_orders"] is False

def test_autonomous_paper_brain_blocks_50x_when_liquidation_distance_is_inside_stop(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(brain, "BRAIN_LATEST", tmp_path / "brain.json")
    monkeypatch.setattr(brain, "BRAIN_HISTORY", tmp_path / "brain.jsonl")
    monkeypatch.setattr(brain, "PAPER_RISK_STATE", tmp_path / "risk.json")
    monkeypatch.setattr(brain, "run_preflight", lambda *args, **kwargs: {"allowed": True, "errors": []})
    monkeypatch.setattr(brain, "evaluate_candidate", lambda candidate: {"blocked": False, "action": "allow"})
    monkeypatch.setattr(
        brain,
        "rank_setups",
        lambda rows: {
            "rankings": [
                {
                    "setup_id": "aplus_pure",
                    "under_sampled": False,
                    "evidence_expectancy": 0.07,
                    "rank_score": 2.0,
                    "sample_confidence": 1.0,
                    "allocation_hint": "normal",
                    "risk_multiplier": 1.0,
                }
            ]
        },
    )

    decision = brain.decide_paper_action(
        [{"symbol": "BTCUSDT", "side": "LONG", "setup_id": "aplus_pure", "score": 10, "entry": 100, "sl": 97, "tp": 106, "leverage": 50}],
        [],
        {"equity": "100", "cash": "100", "open_margin": "0"},
    )

    risk = decision["risk_decision"]
    assert decision["action"] == "skip"
    assert risk["can_open_paper"] is False
    assert "liquidation_distance_inside_stop_risk" in risk["errors"]
    assert risk["paper_sizing"]["requested_leverage"] == 50.0

def test_autonomous_paper_brain_daily_loss_breaker_blocks_new_position(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(brain, "BRAIN_LATEST", tmp_path / "brain.json")
    monkeypatch.setattr(brain, "BRAIN_HISTORY", tmp_path / "brain.jsonl")
    monkeypatch.setattr(brain, "PAPER_RISK_STATE", tmp_path / "risk.json")
    monkeypatch.setattr(brain, "run_preflight", lambda *args, **kwargs: {"allowed": True, "errors": []})
    monkeypatch.setattr(brain, "evaluate_candidate", lambda candidate: {"blocked": False, "action": "allow"})
    monkeypatch.setattr(
        brain,
        "rank_setups",
        lambda rows: {
            "rankings": [
                {
                    "setup_id": "aplus_pure",
                    "under_sampled": False,
                    "evidence_expectancy": 0.07,
                    "rank_score": 2.0,
                    "sample_confidence": 1.0,
                    "allocation_hint": "normal",
                    "risk_multiplier": 1.0,
                }
            ]
        },
    )

    decision = brain.decide_paper_action(
        [{"symbol": "BTCUSDT", "side": "LONG", "setup_id": "aplus_pure", "score": 10, "entry": 100, "sl": 99, "tp": 102}],
        [],
        {"equity": "100", "cash": "100", "starting_equity": "100", "daily_loss_usdt": "-6"},
    )

    risk = decision["risk_decision"]
    assert decision["action"] == "skip"
    assert risk["can_open_paper"] is False
    assert "daily_loss_breaker_active" in risk["errors"]
    assert risk["paper_sizing"]["account_breaker"]["daily_loss_limit_usdt"] == 5.0

def test_autonomous_paper_brain_floors_margin_cap_to_avoid_cap_reject():
    margin = brain.futures_margin_from_risk_budget(
        {"symbol": "BTCUSDT", "side": "LONG", "entry": 100, "sl": 99, "leverage": 2},
        {"max_loss_usdt": 10},
        {"equity": "98.28012261", "cash": "100"},
    )

    assert margin <= 98.28012261 * brain.MAX_PAPER_MARGIN_FRACTION
    assert margin == 44.226055

def test_autonomous_paper_brain_clamps_rounded_risk_budget_before_order_eval():
    account = {"equity": "82.284433469018500", "cash": "79.428892"}
    candidate = {"symbol": "HUSDT", "side": "LONG", "entry": "0.06279", "sl": "0.06122025", "tp": "0.0644382375", "leverage": 5}

    margin = brain.futures_margin_from_risk_budget(candidate, {"max_loss_usdt": 1.645689}, account)
    risk = brain.evaluate_paper_order(
        candidate["symbol"],
        candidate["side"],
        candidate["entry"],
        candidate["sl"],
        candidate["tp"],
        requested_margin=margin,
        requested_leverage=candidate["leverage"],
        setup_id="funding_squeeze",
        account=account,
        config={"mode": "paper_learning", "feature_flags": {"paper_trading": True, "live_orders": False}},
    )

    assert risk["can_open_paper"] is True
    assert "estimated_loss_above_risk_cap" not in risk["errors"]
    assert float(risk["estimated_loss"]) <= float(risk["max_loss"])


def test_autonomous_paper_brain_uses_meaningful_reduced_paper_size(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(brain, "BRAIN_LATEST", tmp_path / "brain.json")
    monkeypatch.setattr(brain, "BRAIN_HISTORY", tmp_path / "brain.jsonl")
    monkeypatch.setattr(brain, "PAPER_RISK_STATE", tmp_path / "risk.json")
    monkeypatch.setattr(brain, "run_preflight", lambda *args, **kwargs: {"allowed": True, "errors": []})
    monkeypatch.setattr(brain, "evaluate_candidate", lambda candidate: {"blocked": False, "action": "allow"})
    monkeypatch.setattr(
        brain,
        "rank_setups",
        lambda rows: {
            "rankings": [
                {
                    "setup_id": "funding_squeeze",
                    "under_sampled": False,
                    "evidence_expectancy": 0.04,
                    "rank_score": 0.15,
                    "allocation_hint": "reduced",
                    "risk_multiplier": 0.35,
                    "rank_reasons": ["counterfactual_parameter_instability"],
                }
            ]
        },
    )

    decision = brain.decide_paper_action(
        [{"symbol": "BELUSDT", "side": "LONG", "setup_id": "funding_squeeze", "score": 7.4, "entry": 0.12668, "sl": 0.123513, "tp": 0.13000535, "leverage": 5}],
        [],
        {"equity": "100", "cash": "100"},
        exploration_allowed=True,
    )

    risk = decision["risk_decision"]
    assert decision["allocation"]["tier"] == "reduced_paper"
    assert decision["allocation"]["risk_fraction"] == 0.0288
    assert decision["allocation"]["sizing_mode"] == "adaptive_paper"
    assert float(risk["paper_sizing"]["requested_leverage"]) == 13.0
    assert float(risk["margin"]) == pytest.approx(8.861538)
    assert float(risk["notional"]) == pytest.approx(115.199994)
    assert float(risk["estimated_loss"]) == pytest.approx(2.879999)


def test_autonomous_paper_brain_falls_back_when_top_candidate_allocation_blocked(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(brain, "BRAIN_LATEST", tmp_path / "brain.json")
    monkeypatch.setattr(brain, "BRAIN_HISTORY", tmp_path / "brain.jsonl")
    monkeypatch.setattr(brain, "PAPER_RISK_STATE", tmp_path / "risk.json")
    monkeypatch.setattr(brain, "run_preflight", lambda *args, **kwargs: {"allowed": True, "errors": []})
    monkeypatch.setattr(brain, "evaluate_candidate", lambda candidate: {"blocked": False, "action": "allow"})
    monkeypatch.setattr(
        brain,
        "rank_setups",
        lambda rows: {
            "rankings": [
                {
                    "setup_id": "exhaustion_fade",
                    "under_sampled": False,
                    "evidence_expectancy": -0.1,
                    "rank_score": -1.5,
                    "allocation_hint": "reduced",
                    "risk_multiplier": 0.35,
                    "rank_reasons": ["non_positive_evidence_expectancy"],
                },
                {
                    "setup_id": "funding_squeeze",
                    "under_sampled": False,
                    "evidence_expectancy": 0.04,
                    "rank_score": 0.15,
                    "allocation_hint": "reduced",
                    "risk_multiplier": 0.35,
                    "rank_reasons": ["counterfactual_parameter_instability"],
                },
            ]
        },
    )

    decision = brain.decide_paper_action(
        [
            {"candidate_id": "bad_top", "symbol": "BEATUSDT", "side": "SHORT", "setup_id": "exhaustion_fade", "score": 15, "entry": 1.0, "sl": 1.03, "tp": 0.97, "leverage": 5},
            {"candidate_id": "good_next", "symbol": "BELUSDT", "side": "LONG", "setup_id": "funding_squeeze", "score": 7.4, "entry": 0.12668, "sl": 0.123513, "tp": 0.13000535, "leverage": 5},
        ],
        [],
        {"equity": "100", "cash": "100"},
        exploration_allowed=True,
    )

    assert decision["action"] == "paper_open_candidate"
    assert decision["candidate"]["candidate_id"] == "good_next"
    assert decision["candidate_attempts"][0]["candidate_id"] == "bad_top"
    assert decision["candidate_attempts"][0]["action"] == "skip"
    assert decision["candidate_attempts"][1]["candidate_id"] == "good_next"
    assert float(decision["risk_decision"]["notional"]) == pytest.approx(115.199994)


def test_autonomous_paper_brain_skips_matching_open_position(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(brain, "BRAIN_LATEST", tmp_path / "brain.json")
    monkeypatch.setattr(brain, "BRAIN_HISTORY", tmp_path / "brain.jsonl")
    monkeypatch.setattr(brain, "PAPER_RISK_STATE", tmp_path / "risk.json")
    monkeypatch.setattr(brain, "run_preflight", lambda *args, **kwargs: {"allowed": True, "errors": []})
    monkeypatch.setattr(brain, "evaluate_candidate", lambda candidate: {"blocked": False, "action": "allow"})
    monkeypatch.setattr(
        brain,
        "rank_setups",
        lambda rows: {
            "rankings": [
                {"setup_id": "funding_squeeze", "under_sampled": False, "evidence_expectancy": 0.04, "rank_score": 0.15, "allocation_hint": "reduced", "risk_multiplier": 0.35}
            ]
        },
    )

    decision = brain.decide_paper_action(
        [
            {"candidate_id": "already_open", "symbol": "BELUSDT", "side": "LONG", "setup_id": "funding_squeeze", "score": 9, "entry": 0.12668, "sl": 0.123513, "tp": 0.13000535, "leverage": 5},
            {"candidate_id": "new_symbol", "symbol": "AGLDUSDT", "side": "LONG", "setup_id": "funding_squeeze", "score": 8, "entry": 0.1918, "sl": 0.187005, "tp": 0.19683475, "leverage": 5},
        ],
        [],
        {"equity": "100", "cash": "100", "open_positions": [{"symbol": "BELUSDT", "side": "LONG", "setup_id": "funding_squeeze"}]},
        exploration_allowed=True,
    )

    assert decision["action"] == "paper_open_candidate"
    assert decision["candidate"]["candidate_id"] == "new_symbol"
    assert decision["candidate_attempts"][0]["errors"] == ["matching_position_already_open"]

def test_autonomous_paper_brain_writes_structured_risk_state_when_all_candidates_skipped(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(brain, "BRAIN_LATEST", tmp_path / "brain.json")
    monkeypatch.setattr(brain, "BRAIN_HISTORY", tmp_path / "brain.jsonl")
    monkeypatch.setattr(brain, "PAPER_RISK_STATE", tmp_path / "risk.json")
    monkeypatch.setattr(brain, "rank_setups", lambda rows: {"rankings": [{"setup_id": "funding_squeeze", "under_sampled": False, "evidence_expectancy": 0.04, "rank_score": 0.15, "allocation_hint": "reduced"}]})

    decision = brain.decide_paper_action(
        [{"candidate_id": "already_open", "symbol": "BELUSDT", "side": "LONG", "setup_id": "funding_squeeze", "score": 9, "entry": 0.12668, "sl": 0.123513, "tp": 0.13000535}],
        [],
        {"equity": "100", "cash": "100", "open_positions": [{"symbol": "BELUSDT", "side": "LONG", "setup_id": "funding_squeeze"}]},
        exploration_allowed=True,
    )

    risk_state = json.loads((tmp_path / "risk.json").read_text(encoding="utf-8"))
    assert decision["action"] == "skip"
    assert risk_state["can_open_paper"] is False
    assert risk_state["can_place_live_orders"] is False
    assert risk_state["reason"] == "no_tradeable_candidate"
    assert risk_state["candidate_attempts"][0]["errors"] == ["matching_position_already_open"]
