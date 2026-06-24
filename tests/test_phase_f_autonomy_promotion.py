from pathlib import Path

import alert_manager as am
import autonomous_paper_trading_brain as brain
import host_runtime_monitor as hrm
import portfolio_correlation_guard as pcg
import promotion_board as pb
import risk_of_ruin_model as ror


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
    assert float(risk["paper_sizing"]["risk_budget_usdt"]) == 1.5
    assert float(risk["margin"]) == 7.5
    assert float(risk["notional"]) == 15.0
    assert risk["can_open_paper"] is True
    assert risk["paper_sizing"]["method"] == "risk_budget_to_isolated_margin"

def test_autonomous_paper_brain_floors_margin_cap_to_avoid_cap_reject():
    margin = brain.futures_margin_from_risk_budget(
        {"symbol": "BTCUSDT", "side": "LONG", "entry": 100, "sl": 99, "leverage": 2},
        {"max_loss_usdt": 10},
        {"equity": "98.28012261", "cash": "100"},
    )

    assert margin <= 98.28012261 * brain.MAX_PAPER_MARGIN_FRACTION
    assert margin == 24.57003
