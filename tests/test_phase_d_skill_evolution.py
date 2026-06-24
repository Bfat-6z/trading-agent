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
    review = sfa.propose_skill_patch(
        {"setup_id": "x", "patch_type": "new_setup"},
        {"expectancy": 0.1, "sample_size": 30},
        pending_path=tmp_path / "pending.jsonl",
        review_path=tmp_path / "reviews.jsonl",
        latest_path=tmp_path / "latest.json",
    )

    assert review["ok"] is False
    assert "missing_invalidation" in review["errors"]
    assert "missing_rollback_criteria" in review["errors"]
    assert "missing_evidence_ids" in review["errors"]


def test_skill_forge_rejects_negative_expectancy(tmp_path: Path):
    review = sfa.propose_skill_patch(
        {"setup_id": "x", "patch_type": "regime_filter", "invalidation": "breaks structure", "rollback_criteria": "paper expectancy stays negative"},
        {"expectancy": -0.1, "sample_size": 50, "evidence_ids": ["r1"]},
        pending_path=tmp_path / "pending.jsonl",
        review_path=tmp_path / "reviews.jsonl",
        latest_path=tmp_path / "latest.json",
    )

    assert review["ok"] is False
    assert "negative_expectancy" in review["errors"]


def test_valid_skill_patch_starts_paper_shadow_only(tmp_path: Path):
    review = sfa.propose_skill_patch(
        {"setup_id": "x", "patch_type": "sl_tp_template", "invalidation": "loses reclaim", "rollback_criteria": "20 future paper closes expectancy <= 0"},
        {"expectancy": 0.05, "sample_size": 25, "evidence_ids": ["review_1", "shadow_1"]},
        pending_path=tmp_path / "pending.jsonl",
        review_path=tmp_path / "reviews.jsonl",
        latest_path=tmp_path / "latest.json",
    )

    assert review["ok"] is True
    assert review["status"] == "paper_shadow_only"
    assert review["lifecycle"] == ["proposed", "schema_valid", "evidence_checked"]


def test_skill_forge_builds_patch_candidate_from_bad_review_cluster():
    reviews = []
    for idx in range(35):
        reviews.append(
            {
                "review_id": f"r{idx}",
                "classification": "bad_loss" if idx < 20 else "good_win",
                "source_trade": {"setup_id": "exhaustion_fade", "net": "-0.1" if idx < 20 else "0.02"},
            }
        )

    candidates = sfa.build_review_patch_candidates(reviews, min_sample=30)

    assert candidates
    assert candidates[0]["patch"]["setup_id"] == "exhaustion_fade"
    assert candidates[0]["patch"]["patch_type"] == "min_score_adjustment_by_setup"
    assert candidates[0]["evidence"]["sample_size"] == 35
    assert candidates[0]["evidence"]["post_trade_review_ids"]


def test_skill_forge_run_once_proposes_from_reviews(tmp_path: Path):
    reviews_path = tmp_path / "reviews_source.jsonl"
    for idx in range(35):
        sfa.append_jsonl_once(
            reviews_path,
            {
                "review_id": f"r{idx}",
                "classification": "bad_loss" if idx < 20 else "good_win",
                "source_trade": {"setup_id": "exhaustion_fade", "net": "-0.1" if idx < 20 else "0.02"},
            },
            "review_id",
        )

    result = sfa.run_once(
        reviews_path=reviews_path,
        pending_path=tmp_path / "pending.jsonl",
        review_path=tmp_path / "reviews.jsonl",
        latest_path=tmp_path / "latest.json",
        applied_path=tmp_path / "applied.jsonl",
        integration_output_path=tmp_path / "integration.json",
        min_sample=30,
        apply=False,
    )

    assert result["candidate_count"] == 1
    assert result["accepted_count"] == 1
    assert result["can_place_live_orders"] is False


def test_skill_forge_applies_patch_as_paper_only_lifecycle(monkeypatch, tmp_path: Path):
    pending = tmp_path / "pending.jsonl"
    applied = tmp_path / "applied.jsonl"
    output = tmp_path / "integration.json"
    latest = tmp_path / "latest.json"
    library = {"skills": {"x": {"setup_id": "x", "metadata": {}}}, "history": []}
    saved = {}
    monkeypatch.setattr(sfa, "load_library", lambda: library)
    monkeypatch.setattr(sfa, "save_library", lambda payload: saved.setdefault("library", payload) or payload)
    sfa.propose_skill_patch(
        {"setup_id": "x", "patch_type": "setup_retirement", "invalidation": "setup keeps losing", "rollback_criteria": "future window recovers", "patch_id": "p1"},
        {"expectancy": 0.02, "sample_size": 50, "evidence_ids": ["review_1"]},
        pending_path=pending,
        review_path=tmp_path / "reviews.jsonl",
        latest_path=latest,
    )

    result = sfa.apply_paper_shadow_patches(pending_path=pending, output_path=output, applied_path=applied, latest_path=latest)

    assert result["applied_count"] == 1
    assert result["can_place_live_orders"] is False
    skill = saved["library"]["skills"]["x"]
    assert skill["metadata"]["paper_only_retired"] is True
    assert skill["metadata"]["paper_shadow_patches"][0]["status"] == "paper_only_applied"
    assert sfa.read_jsonl(applied)[0]["lifecycle"][-1] == "paper_only_applied"


def test_skill_forge_apply_revalidates_pending_patch(monkeypatch, tmp_path: Path):
    pending = tmp_path / "pending.jsonl"
    output = tmp_path / "integration.json"
    library = {"skills": {"x": {"setup_id": "x", "metadata": {}}}, "history": []}
    monkeypatch.setattr(sfa, "load_library", lambda: library)
    monkeypatch.setattr(sfa, "save_library", lambda payload: payload)
    sfa.append_jsonl_once(pending, {"patch_id": "old", "setup_id": "x", "patch_type": "sl_tp_template", "invalidation": "old", "status": "paper_shadow_only", "evidence": {"sample_size": 30}}, "patch_id")

    result = sfa.apply_paper_shadow_patches(pending_path=pending, output_path=output, applied_path=tmp_path / "applied.jsonl", latest_path=tmp_path / "latest.json")

    assert result["applied_count"] == 0
    assert result["skipped"][0]["reason"] == "failed_apply_gate"
    assert "missing_rollback_criteria" in result["skipped"][0]["errors"]


def test_allocation_blocks_paper_only_retired_setup(tmp_path: Path):
    rankings = [{"setup_id": "retired", "under_sampled": False, "expectancy": 0.05, "rank_score": 1.0, "paper_only_retired": True}]

    result = cap.allocate_capital("retired", rankings, {"equity": "100"}, exploration_allowed=True, output_path=tmp_path / "alloc.json")

    assert result["allowed"] is False
    assert "setup_paper_only_retired" in result["errors"]
