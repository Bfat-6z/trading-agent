from pathlib import Path

import capital_allocation_policy as cap
import experiment_registry as exp
import setup_ranker
import skill_forge_agent as sfa


def test_experiment_rejects_overlapping_train_test_windows(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(exp, "EXPERIMENTS_LATEST", tmp_path / "experiments_latest.json")

    row = exp.propose_experiment(
        "momentum edge persists",
        "momentum",
        {"start": "2026-06-01T00:00:00+00:00", "end": "2026-06-10T00:00:00+00:00"},
        {"start": "2026-06-09T00:00:00+00:00", "end": "2026-06-15T00:00:00+00:00"},
        path=tmp_path / "experiments.jsonl",
    )

    assert row["status"] == "rejected"
    assert "train_test_windows_overlap" in row["errors"]


def test_experiment_fails_train_only_edge():
    row = {"experiment_id": "e1", "success_metric": "expectancy_after_fees"}

    result = exp.evaluate_experiment(row, {"expectancy_after_fees": 0.1}, {"expectancy_after_fees": -0.01, "trades": 50})

    assert result["status"] == "failed"
    assert "test_metric_not_positive" in result["errors"]


def test_setup_ranker_penalizes_high_winrate_bad_expectancy(tmp_path: Path):
    rows = [
        {"setup_id": "pretty_wr_bad_exp", "trades": 100, "win_rate": 0.9, "expectancy": -0.02, "profit_factor": 0.8},
        {"setup_id": "lower_wr_good_exp", "trades": 80, "win_rate": 0.52, "expectancy": 0.05, "profit_factor": 1.4},
    ]

    result = setup_ranker.rank_setups(rows, output_path=tmp_path / "rankings.json")

    assert result["top_setup_id"] == "lower_wr_good_exp"


def test_allocation_blocks_undersampled_without_exploration(tmp_path: Path):
    rankings = [{"setup_id": "new_setup", "under_sampled": True, "expectancy": 0.02, "rank_score": 0.9}]

    result = cap.allocate_capital("new_setup", rankings, {"equity": "100"}, exploration_allowed=False, output_path=tmp_path / "alloc.json")

    assert result["allowed"] is False
    assert "setup_under_sampled" in result["errors"]


def test_allocation_allows_tiny_exploration_for_undersampled(tmp_path: Path):
    rankings = [{"setup_id": "new_setup", "under_sampled": True, "expectancy": 0.02, "rank_score": 0.9}]

    result = cap.allocate_capital("new_setup", rankings, {"equity": "100"}, exploration_allowed=True, output_path=tmp_path / "alloc.json")

    assert result["allowed"] is True
    assert result["tier"] == "exploration_paper"
    assert result["max_loss_usdt"] == 1.5
    assert result["can_trade_live"] is False


def test_skill_forge_rejects_patch_missing_invalidation(tmp_path: Path):
    review = sfa.propose_skill_patch({"setup_id": "x", "patch_type": "new_setup"}, {"expectancy": 0.1, "sample_size": 30}, pending_path=tmp_path / "pending.jsonl", review_path=tmp_path / "reviews.jsonl")

    assert review["ok"] is False
    assert "missing_invalidation" in review["errors"]


def test_skill_forge_rejects_negative_expectancy(tmp_path: Path):
    review = sfa.propose_skill_patch({"setup_id": "x", "patch_type": "regime_filter", "invalidation": "breaks structure"}, {"expectancy": -0.1, "sample_size": 50}, pending_path=tmp_path / "pending.jsonl", review_path=tmp_path / "reviews.jsonl")

    assert review["ok"] is False
    assert "negative_expectancy" in review["errors"]


def test_valid_skill_patch_starts_paper_shadow_only(tmp_path: Path):
    review = sfa.propose_skill_patch({"setup_id": "x", "patch_type": "sl_tp_template", "invalidation": "loses reclaim"}, {"expectancy": 0.05, "sample_size": 25}, pending_path=tmp_path / "pending.jsonl", review_path=tmp_path / "reviews.jsonl")

    assert review["ok"] is True
    assert review["status"] == "paper_shadow_only"
