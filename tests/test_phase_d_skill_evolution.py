from pathlib import Path

import backtest_harness as bh
import capital_allocation_policy as cap
import experiment_registry as exp
import promotion_board as pb
import setup_ranker
import skill_forge_agent as sfa
import walk_forward_validator as wfv

def review_row(setup_id: str, ts: str, net: float, review_id: str) -> dict:
    return {
        "review_id": review_id,
        "reviewed_at": ts,
        "decision_ts": ts,
        "source_trade": {"setup_id": setup_id, "close_ts": ts, "net": str(net)},
        "costs": {"net": str(net)},
    }


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

def test_walk_forward_running_until_future_sample():
    patch = {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-24T10:00:00+00:00"}
    reviews = [
        review_row("fade", "2026-06-24T09:00:00+00:00", -0.2, "r1"),
        review_row("fade", "2026-06-24T10:00:00+00:00", 5.0, "boundary"),
        review_row("fade", "2026-06-24T10:05:00+00:00", 0.2, "r2"),
    ]

    result = wfv.evaluate_patch_walk_forward(patch, reviews, min_test_trades=2)

    assert result["status"] == "running"
    assert result["test_metrics"]["trades"] == 1
    assert "insufficient_future_trades" in result["errors"]
    assert result["can_place_live_orders"] is False

def test_walk_forward_passes_with_future_positive_expectancy():
    patch = {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-24T10:00:00+00:00"}
    reviews = [
        review_row("fade", "2026-06-24T09:00:00+00:00", -0.2, "r1"),
        review_row("fade", "2026-06-24T10:05:00+00:00", 0.2, "r2"),
        review_row("fade", "2026-06-24T10:10:00+00:00", 0.4, "r3"),
    ]

    result = wfv.evaluate_patch_walk_forward(patch, reviews, min_test_trades=2)

    assert result["status"] == "passed"
    assert result["test_metrics"]["trades"] == 2
    assert result["test_metrics"]["expectancy_after_fees"] == 0.3

def test_walk_forward_fails_with_future_negative_expectancy():
    patch = {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-24T10:00:00+00:00"}
    reviews = [
        review_row("fade", "2026-06-24T10:05:00+00:00", -0.5, "r1"),
        review_row("fade", "2026-06-24T10:10:00+00:00", 0.1, "r2"),
    ]

    result = wfv.evaluate_patch_walk_forward(patch, reviews, min_test_trades=2)

    assert result["status"] == "failed"
    assert "future_expectancy_not_positive" in result["errors"]
    assert "future_profit_factor_too_low" in result["errors"]

def test_walk_forward_run_once_writes_latest_rows_without_live_permission(tmp_path: Path):
    applied = tmp_path / "applied.jsonl"
    pending = tmp_path / "pending.jsonl"
    reviews = tmp_path / "reviews.jsonl"
    history = tmp_path / "experiments.jsonl"
    latest = tmp_path / "experiments_latest.json"
    walk_latest = tmp_path / "walk_forward_latest.json"
    wfv.append_jsonl(applied, {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-24T10:00:00+00:00", "status": "paper_only_applied"})
    wfv.append_jsonl(reviews, review_row("fade", "2026-06-24T10:05:00+00:00", 0.2, "r1"))
    wfv.append_jsonl(reviews, review_row("fade", "2026-06-24T10:10:00+00:00", 0.2, "r2"))

    result = wfv.run_once(
        applied_path=applied,
        pending_path=pending,
        reviews_path=reviews,
        history_path=history,
        latest_path=latest,
        walk_forward_path=walk_latest,
        min_test_trades=2,
    )

    assert result["experiment_count"] == 1
    assert result["by_status"]["passed"] == 1
    assert result["rows"][0]["patch_id"] == "p1"
    assert result["can_place_live_orders"] is False
    assert walk_latest.exists()

def test_walk_forward_latest_ignores_non_walk_forward_experiments(tmp_path: Path):
    history = tmp_path / "experiments.jsonl"
    latest = tmp_path / "experiments_latest.json"
    walk_latest = tmp_path / "walk_forward_latest.json"
    wfv.append_jsonl(history, {"experiment_id": "exp_old", "status": "passed", "setup_id": "other"})
    result = wfv.write_outputs(
        [
            {
                "experiment_id": "wf_p1",
                "patch_id": "p1",
                "setup_id": "fade",
                "status": "running",
                "errors": ["insufficient_future_trades"],
                "test_metrics": {"trades": 1},
                "can_place_live_orders": False,
            }
        ],
        history_path=history,
        latest_path=latest,
        walk_forward_path=walk_latest,
    )

    assert result["experiment_count"] == 1
    assert result["by_status"] == {"running": 1}
    assert result["rows"][0]["experiment_id"] == "wf_p1"

def test_walk_forward_window_spec_id_is_reproducible_and_boundaries_immutable():
    spec = wfv.build_walk_forward_window_spec(
        train_start="2026-06-01T00:00:00+00:00",
        train_end="2026-06-10T00:00:00+00:00",
        test_start="2026-06-11T00:00:00+00:00",
        test_end="2026-06-15T00:00:00+00:00",
        holdout_start="2026-06-16T00:00:00+00:00",
        holdout_end="2026-06-20T00:00:00+00:00",
        embargo_seconds=3600,
    )
    same = wfv.build_walk_forward_window_spec(
        train_start="2026-06-01T00:00:00+00:00",
        train_end="2026-06-10T00:00:00+00:00",
        test_start="2026-06-11T00:00:00+00:00",
        test_end="2026-06-15T00:00:00+00:00",
        holdout_start="2026-06-16T00:00:00+00:00",
        holdout_end="2026-06-20T00:00:00+00:00",
        embargo_seconds=3600,
    )
    tampered = {**spec, "test": {"start": "2026-06-12T00:00:00+00:00", "end": "2026-06-15T00:00:00+00:00"}}

    assert spec["window_id"] == same["window_id"]
    assert wfv.validate_walk_forward_window_spec(spec) == []
    assert "window_id_digest_mismatch" in wfv.validate_walk_forward_window_spec(tampered)

def test_walk_forward_rejects_review_with_feature_ts_after_decision_time():
    patch = {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-24T10:00:00+00:00"}
    bad = review_row("fade", "2026-06-24T10:05:00+00:00", 0.2, "r1")
    bad["decision_ts"] = "2026-06-24T10:00:00+00:00"
    bad["feature_ts"] = "2026-06-24T10:01:00+00:00"

    result = wfv.evaluate_patch_walk_forward(patch, [bad], min_test_trades=1)

    assert result["status"] == "failed"
    assert "feature_ts_after_decision" in result["errors"]

def test_walk_forward_rejects_feature_rows_missing_decision_time():
    patch = {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-24T10:00:00+00:00"}
    bad = review_row("fade", "2026-06-24T10:05:00+00:00", 0.2, "r1")
    bad.pop("decision_ts", None)
    bad["feature_ts"] = "2026-06-24T10:01:00+00:00"

    result = wfv.evaluate_patch_walk_forward(patch, [bad], min_test_trades=1)

    assert result["status"] == "failed"
    assert "missing_decision_time" in result["errors"]

def test_walk_forward_rejects_feature_end_after_decision_time():
    patch = {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-24T10:00:00+00:00"}
    bad = review_row("fade", "2026-06-24T10:05:00+00:00", 0.2, "r1")
    bad["decision_ts"] = "2026-06-24T10:00:00+00:00"
    bad["feature_end_at"] = "2026-06-24T10:01:00+00:00"

    result = wfv.evaluate_patch_walk_forward(patch, [bad], min_test_trades=1)

    assert result["status"] == "failed"
    assert "feature_end_at_after_decision" in result["errors"]

def test_walk_forward_splits_by_decision_time_not_close_time():
    patch = {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-24T10:00:00+00:00"}
    row = review_row("fade", "2026-06-24T10:05:00+00:00", 0.2, "r1")
    row["decision_ts"] = "2026-06-24T09:55:00+00:00"

    result = wfv.evaluate_patch_walk_forward(patch, [row], min_test_trades=1)

    assert result["status"] == "running"
    assert result["test_metrics"]["trades"] == 0
    assert "insufficient_future_trades" in result["errors"]

def test_walk_forward_purge_embargo_removes_overlapping_label_intervals():
    rows = [
        {"review_id": "safe", "label_start_at": "2026-06-01T00:00:00+00:00", "label_end_at": "2026-06-02T00:00:00+00:00"},
        {"review_id": "overlap", "label_start_at": "2026-06-09T23:30:00+00:00", "label_end_at": "2026-06-10T01:00:00+00:00"},
    ]

    kept, purged = wfv.purge_samples_for_window(rows, {"start": "2026-06-10T00:00:00+00:00", "end": "2026-06-12T00:00:00+00:00"}, embargo_seconds=3600)

    assert [row["review_id"] for row in kept] == ["safe"]
    assert purged == ["overlap"]

def test_walk_forward_purges_test_labels_overlapping_audit_holdout():
    spec = wfv.build_walk_forward_window_spec(
        train_start="2026-06-01T00:00:00+00:00",
        train_end="2026-06-09T00:00:00+00:00",
        test_start="2026-06-10T00:00:00+00:00",
        test_end="2026-06-11T00:00:00+00:00",
        holdout_start="2026-06-12T00:00:00+00:00",
        holdout_end="2026-06-14T00:00:00+00:00",
    )
    patch = {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-09T00:00:00+00:00", "walk_forward_window_spec": spec}
    row = review_row("fade", "2026-06-10T01:00:00+00:00", 0.2, "r1")
    row["label_start_at"] = "2026-06-10T01:00:00+00:00"
    row["label_end_at"] = "2026-06-12T01:00:00+00:00"

    result = wfv.evaluate_patch_walk_forward(patch, [row], min_test_trades=1)

    assert result["test_metrics"]["trades"] == 0
    assert result["partition_meta"]["purged_test_for_holdout"] == ["r1"]

def test_walk_forward_censors_or_fails_immature_labels_until_outcome_known():
    patch = {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-24T10:00:00+00:00"}
    row = review_row("fade", "2026-06-24T10:05:00+00:00", 0.2, "r1")
    row["outcome_known_at"] = "2999-01-01T00:00:00+00:00"

    result = wfv.evaluate_patch_walk_forward(patch, [row], min_test_trades=1)

    assert result["status"] == "failed"
    assert "immature_or_unresolved_labels" in result["errors"]
    assert result["test_metrics"]["trades"] == 0

def test_walk_forward_censors_future_label_end_without_outcome_known():
    patch = {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-24T10:00:00+00:00"}
    row = review_row("fade", "2026-06-24T10:05:00+00:00", 0.2, "r1")
    row["label_end_at"] = "2999-01-01T00:00:00+00:00"

    result = wfv.evaluate_patch_walk_forward(patch, [row], min_test_trades=1)

    assert result["status"] == "failed"
    assert "immature_or_unresolved_labels" in result["errors"]

def test_walk_forward_censors_future_review_without_explicit_label_fields():
    patch = {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-24T10:00:00+00:00"}
    row = review_row("fade", "2999-01-01T00:00:00+00:00", 0.2, "r1")

    result = wfv.evaluate_patch_walk_forward(patch, [row], min_test_trades=1)

    assert result["status"] == "failed"
    assert "immature_or_unresolved_labels" in result["errors"]

def test_walk_forward_censors_future_review_even_when_label_end_is_past():
    patch = {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-24T10:00:00+00:00"}
    row = review_row("fade", "2999-01-01T00:00:00+00:00", 0.2, "r1")
    row["label_end_at"] = "2026-06-24T10:10:00+00:00"

    result = wfv.evaluate_patch_walk_forward(patch, [row], min_test_trades=1)

    assert result["status"] == "failed"
    assert "immature_or_unresolved_labels" in result["errors"]

def test_walk_forward_rejects_current_active_universe_for_promotion_evidence():
    patch = {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-24T10:00:00+00:00"}
    row = review_row("fade", "2026-06-24T10:05:00+00:00", 0.2, "r1")
    row["universe_manifest"] = {"current_survivor_only": True}

    result = wfv.evaluate_patch_walk_forward(patch, [row], min_test_trades=1)

    assert result["status"] == "failed"
    assert "current_survivor_universe_diagnostic_only" in result["errors"]

def test_walk_forward_holdout_peek_consumes_budget_and_reuse_fails():
    patch = {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-24T10:00:00+00:00", "uses_audit_holdout": True, "audit_holdout_id": "h1"}
    row = review_row("fade", "2026-06-24T10:05:00+00:00", 0.2, "r1")

    result = wfv.evaluate_patch_walk_forward(patch, [row], min_test_trades=1, holdout_registry_rows=[{"audit_holdout_id": "h1", "sealed": True}])

    assert result["status"] == "failed"
    assert "audit_holdout_budget_exhausted" in result["errors"]

def test_walk_forward_run_once_consumes_audit_holdout_budget(tmp_path: Path, monkeypatch):
    memory = tmp_path / "agent_memory"
    memory.mkdir()
    monkeypatch.setattr(wfv, "HOLDOUT_REGISTRY", memory / "holdout.jsonl")
    monkeypatch.setattr(wfv, "WALK_FORWARD_HEARTBEAT", tmp_path / "walk_forward_heartbeat.json")
    monkeypatch.setattr(wfv, "WALK_FORWARD_PID", tmp_path / "walk_forward_validator.pid")
    applied = tmp_path / "applied.jsonl"
    pending = tmp_path / "pending.jsonl"
    reviews = tmp_path / "reviews.jsonl"
    patch = {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-24T10:00:00+00:00", "uses_audit_holdout": True, "audit_holdout_id": "h1"}
    wfv.append_jsonl(applied, patch)
    wfv.append_jsonl(reviews, review_row("fade", "2026-06-24T10:05:00+00:00", 0.2, "r1"))

    first = wfv.run_once(applied_path=applied, pending_path=pending, reviews_path=reviews, history_path=tmp_path / "history.jsonl", latest_path=tmp_path / "latest.json", walk_forward_path=tmp_path / "wf.json", min_test_trades=1)
    second = wfv.run_once(applied_path=applied, pending_path=pending, reviews_path=reviews, history_path=tmp_path / "history2.jsonl", latest_path=tmp_path / "latest2.json", walk_forward_path=tmp_path / "wf2.json", min_test_trades=1)

    assert first["by_status"]["passed"] == 1
    assert second["by_status"]["failed"] == 1
    assert "audit_holdout_budget_exhausted" in second["rows"][0]["errors"]

def test_walk_forward_spec_holdout_consumes_budget_even_without_flag(tmp_path: Path, monkeypatch):
    memory = tmp_path / "agent_memory"
    memory.mkdir()
    monkeypatch.setattr(wfv, "HOLDOUT_REGISTRY", memory / "holdout.jsonl")
    monkeypatch.setattr(wfv, "WALK_FORWARD_HEARTBEAT", tmp_path / "walk_forward_heartbeat.json")
    monkeypatch.setattr(wfv, "WALK_FORWARD_PID", tmp_path / "walk_forward_validator.pid")
    applied = tmp_path / "applied.jsonl"
    pending = tmp_path / "pending.jsonl"
    reviews = tmp_path / "reviews.jsonl"
    spec = wfv.build_walk_forward_window_spec(
        train_start="2026-06-01T00:00:00+00:00",
        train_end="2026-06-10T00:00:00+00:00",
        test_start="2026-06-11T00:00:00+00:00",
        test_end="2026-06-12T00:00:00+00:00",
        holdout_start="2026-06-13T00:00:00+00:00",
        holdout_end="2026-06-14T00:00:00+00:00",
    )
    wfv.append_jsonl(applied, {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-10T00:00:00+00:00", "walk_forward_window_spec": spec})
    wfv.append_jsonl(reviews, review_row("fade", "2026-06-11T00:05:00+00:00", 0.2, "r1"))

    wfv.run_once(applied_path=applied, pending_path=pending, reviews_path=reviews, history_path=tmp_path / "history.jsonl", latest_path=tmp_path / "latest.json", walk_forward_path=tmp_path / "wf.json", min_test_trades=1)
    second = wfv.run_once(applied_path=applied, pending_path=pending, reviews_path=reviews, history_path=tmp_path / "history2.jsonl", latest_path=tmp_path / "latest2.json", walk_forward_path=tmp_path / "wf2.json", min_test_trades=1)

    assert second["by_status"]["failed"] == 1
    assert "audit_holdout_budget_exhausted" in second["rows"][0]["errors"]

def test_walk_forward_two_same_holdout_patches_in_one_run_do_not_both_pass(tmp_path: Path, monkeypatch):
    memory = tmp_path / "agent_memory"
    memory.mkdir()
    monkeypatch.setattr(wfv, "HOLDOUT_REGISTRY", memory / "holdout.jsonl")
    monkeypatch.setattr(wfv, "WALK_FORWARD_HEARTBEAT", tmp_path / "walk_forward_heartbeat.json")
    monkeypatch.setattr(wfv, "WALK_FORWARD_PID", tmp_path / "walk_forward_validator.pid")
    applied = tmp_path / "applied.jsonl"
    pending = tmp_path / "pending.jsonl"
    reviews = tmp_path / "reviews.jsonl"
    for patch_id in ("p1", "p2"):
        wfv.append_jsonl(applied, {"patch_id": patch_id, "setup_id": "fade", "applied_at": "2026-06-24T10:00:00+00:00", "uses_audit_holdout": True, "audit_holdout_id": "h1"})
    wfv.append_jsonl(reviews, review_row("fade", "2026-06-24T10:05:00+00:00", 0.2, "r1"))

    result = wfv.run_once(applied_path=applied, pending_path=pending, reviews_path=reviews, history_path=tmp_path / "history.jsonl", latest_path=tmp_path / "latest.json", walk_forward_path=tmp_path / "wf.json", min_test_trades=1)

    assert result["by_status"]["passed"] == 1
    assert result["by_status"]["failed"] == 1
    assert "audit_holdout_budget_exhausted" in result["rows"][1]["errors"]

def test_walk_forward_contaminated_family_cannot_use_final_holdout():
    patch = {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-24T10:00:00+00:00", "hypothesis_source_partition": "holdout"}
    row = review_row("fade", "2026-06-24T10:05:00+00:00", 0.2, "r1")

    result = wfv.evaluate_patch_walk_forward(patch, [row], min_test_trades=1)

    assert result["status"] == "failed"
    assert "test_holdout_derived_hypothesis_contaminates_family" in result["errors"]

def test_walk_forward_requires_effect_size_and_confidence_not_just_positive_mean():
    patch = {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-24T10:00:00+00:00"}
    rows = [
        review_row("fade", "2026-06-24T10:05:00+00:00", 0.01, "r1"),
        review_row("fade", "2026-06-24T10:10:00+00:00", 0.01, "r2"),
    ]

    result = wfv.evaluate_patch_walk_forward(patch, rows, min_test_trades=2, min_effect_size=0.05)

    assert result["status"] == "failed"
    assert "effect_size_too_small" in result["errors"]
    assert result["test_metrics"]["confidence_interval"]["n"] == 2

def test_walk_forward_family_alpha_blocks_best_of_many_false_positive_variants():
    patch = {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-24T10:00:00+00:00", "family_variant_count": 10, "p_value": 0.02}
    row = review_row("fade", "2026-06-24T10:05:00+00:00", 0.2, "r1")

    result = wfv.evaluate_patch_walk_forward(patch, [row], min_test_trades=1)

    assert result["status"] == "failed"
    assert "multiple_test_penalty_failed" in result["errors"]
    assert result["family_correction"]["corrected_alpha"] == 0.005

def test_walk_forward_family_alpha_requires_p_value_for_many_variants():
    patch = {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-24T10:00:00+00:00", "family_variant_count": 100}
    row = review_row("fade", "2026-06-24T10:05:00+00:00", 0.2, "r1")

    result = wfv.evaluate_patch_walk_forward(patch, [row], min_test_trades=1)

    assert result["status"] == "failed"
    assert "missing_p_value_for_family_correction" in result["errors"]

def test_walk_forward_family_alpha_uses_family_registry_variant_count():
    patch = {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-24T10:00:00+00:00", "experiment_family_id": "fam1"}
    row = review_row("fade", "2026-06-24T10:05:00+00:00", 0.2, "r1")
    registry = [{"experiment_family_id": "fam1", "variant_hash": f"v{i}"} for i in range(3)]

    result = wfv.evaluate_patch_walk_forward(patch, [row], min_test_trades=1, family_registry_rows=registry)

    assert result["status"] == "failed"
    assert "missing_p_value_for_family_correction" in result["errors"]
    assert result["family_correction"]["family_variant_count"] == 3

def test_walk_forward_rejects_invalid_negative_p_value():
    patch = {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-24T10:00:00+00:00", "family_variant_count": 100, "p_value": -1}
    row = review_row("fade", "2026-06-24T10:05:00+00:00", 0.2, "r1")

    result = wfv.evaluate_patch_walk_forward(patch, [row], min_test_trades=1)

    assert result["status"] == "failed"
    assert "invalid_p_value" in result["errors"]

def test_walk_forward_group_validation_fails_single_symbol_or_single_cluster_edge():
    patch = {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-24T10:00:00+00:00", "requires_grouped_validation": True}
    row = review_row("fade", "2026-06-24T10:05:00+00:00", 0.2, "r1")
    row["symbol"] = "BTCUSDT"
    row["sector"] = "majors"
    row["beta_cluster"] = "btc_beta"

    result = wfv.evaluate_patch_walk_forward(patch, [row], min_test_trades=1, grouped_requirements={"min_unique_symbols": 2, "min_unique_beta_clusters": 2})

    assert result["status"] == "failed"
    assert "unique_symbols_below_minimum" in result["errors"]
    assert "unique_beta_clusters_below_minimum" in result["errors"]

def test_walk_forward_regime_labels_must_have_cutoff_at_or_before_decision_time():
    patch = {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-24T10:00:00+00:00"}
    row = review_row("fade", "2026-06-24T10:05:00+00:00", 0.2, "r1")
    row["decision_ts"] = "2026-06-24T10:00:00+00:00"
    row["regime_label_cutoff_proof"] = {"max_input_ts": "2026-06-24T10:02:00+00:00"}

    result = wfv.evaluate_patch_walk_forward(patch, [row], min_test_trades=1)

    assert result["status"] == "failed"
    assert "regime_max_input_ts_after_decision" in result["errors"]

def test_walk_forward_returns_inconclusive_when_required_regime_bucket_missing():
    patch = {
        "patch_id": "p1",
        "setup_id": "fade",
        "applied_at": "2026-06-24T10:00:00+00:00",
        "regime_distribution_manifest": {"required_buckets": ["risk_on", "risk_off"], "min_effective_n_per_bucket": 1},
    }
    row = review_row("fade", "2026-06-24T10:05:00+00:00", 0.2, "r1")
    row["market_regime"] = "risk_on"

    result = wfv.evaluate_patch_walk_forward(patch, [row], min_test_trades=1)

    assert result["status"] == "inconclusive"
    assert "required_regime_bucket_absent" in result["errors"]

def test_walk_forward_rejects_full_history_fitted_transform_digest():
    patch = {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-24T10:00:00+00:00"}
    row = review_row("fade", "2026-06-24T10:05:00+00:00", 0.2, "r1")
    row["transform_fit_partition"] = "full_history"

    result = wfv.evaluate_patch_walk_forward(patch, [row], min_test_trades=1)

    assert result["status"] == "failed"
    assert "transform_fit_outside_train" in result["errors"]

def test_decision_time_backtest_blocks_future_entry_request(tmp_path: Path):
    rows = [
        {"ts": "2026-06-21T00:00:00+00:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1},
        {"ts": "2026-06-21T00:01:00+00:00", "open": 100, "high": 101, "low": 99, "close": 101, "volume": 1},
        {"ts": "2026-06-21T00:02:00+00:00", "open": 101, "high": 102, "low": 100, "close": 102, "volume": 1},
    ]

    try:
        bh.run_decision_time_backtest("bad", rows, lambda visible: {"index": len(visible) + 5, "side": "LONG"}, output_dir=tmp_path)
    except ValueError as exc:
        assert str(exc) == "strategy_must_enter_next_candle"
    else:
        raise AssertionError("future entry request should fail")

def test_decision_time_backtest_requires_next_candle_entry(tmp_path: Path):
    rows = [
        {"ts": "2026-06-21T00:00:00+00:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1},
        {"ts": "2026-06-21T00:01:00+00:00", "open": 100, "high": 101, "low": 99, "close": 101, "volume": 1},
        {"ts": "2026-06-21T00:02:00+00:00", "open": 101, "high": 102, "low": 100, "close": 102, "volume": 1},
    ]

    try:
        bh.run_decision_time_backtest("same", rows, lambda visible: {"index": len(visible) - 1, "side": "LONG"}, output_dir=tmp_path)
    except ValueError as exc:
        assert str(exc) == "strategy_must_enter_next_candle"
    else:
        raise AssertionError("same-candle entry should fail")

def test_promotion_board_blocks_stale_walk_forward_latest(tmp_path: Path):
    metrics = pb.walk_forward_metrics({"updated_at": "2000-01-01T00:00:00+00:00", "stale_sla_seconds": 60, "by_status": {"passed": 1}, "rows": [{"patch_id": "p1", "status": "passed", "walk_forward_window_spec": {"window_id": "w1"}, "family_correction": {"corrected_alpha": 0.01}}]}, ["p1"])

    result = pb.evaluate_promotion({**metrics, "paper_trades": 999, "shadow_closes": 9999, "lifecycle_completeness": 1.0, "daily_exam_avg": 100, "trial_days": 99}, output_path=tmp_path / "promotion.json")

    assert "walk_forward_stale" in result["failures"]

def test_promotion_board_requires_candidate_manifest_wf_window_and_metric_digests(tmp_path: Path):
    metrics = pb.walk_forward_metrics(
        {
            "updated_at": "2999-01-01T00:00:00+00:00",
            "stale_sla_seconds": 60,
            "by_status": {"passed": 1},
            "rows": [{"patch_id": "p1", "status": "passed", "metric_manifest_digest": "a", "cited_metric_manifest_digest": "b"}],
        },
        ["p1"],
    )

    result = pb.evaluate_promotion({**metrics, "paper_trades": 999, "shadow_closes": 9999, "lifecycle_completeness": 1.0, "daily_exam_avg": 100, "trial_days": 99}, output_path=tmp_path / "promotion.json")

    assert "walk_forward_manifest_digest_mismatch" in result["failures"]

def test_promotion_board_requires_cited_metric_digest(tmp_path: Path):
    metrics = pb.walk_forward_metrics(
        {
            "updated_at": "2999-01-01T00:00:00+00:00",
            "stale_sla_seconds": 60,
            "by_status": {"passed": 1},
            "rows": [{"patch_id": "p1", "status": "passed", "walk_forward_window_spec": {"window_id": "w1"}, "family_correction": {"corrected_alpha": 0.01}, "metric_manifest_digest": "m", "code_config_digest": "c", "candidate_policy_digest": "p", "frozen_partition_digest": "f"}],
        },
        ["p1"],
    )

    result = pb.evaluate_promotion({**metrics, "paper_trades": 999, "shadow_closes": 9999, "lifecycle_completeness": 1.0, "daily_exam_avg": 100, "trial_days": 99}, output_path=tmp_path / "promotion.json")

    assert result["passed"] is False
    assert "walk_forward_manifest_digest_mismatch" in result["failures"]

def test_promotion_board_handles_null_walk_forward_spec_as_digest_failure(tmp_path: Path):
    metrics = pb.walk_forward_metrics(
        {
            "updated_at": "2999-01-01T00:00:00+00:00",
            "stale_sla_seconds": 60,
            "by_status": {"passed": 1},
            "rows": [{"patch_id": "p1", "status": "passed", "walk_forward_window_spec": None, "family_correction": {"corrected_alpha": 0.01}}],
        },
        ["p1"],
    )

    result = pb.evaluate_promotion({**metrics, "paper_trades": 999, "shadow_closes": 9999, "lifecycle_completeness": 1.0, "daily_exam_avg": 100, "trial_days": 99}, output_path=tmp_path / "promotion.json")

    assert result["passed"] is False
    assert "walk_forward_manifest_digest_mismatch" in result["failures"]

def test_walk_forward_run_once_writes_review_watermark_and_heartbeat(tmp_path: Path, monkeypatch):
    memory = tmp_path / "agent_memory"
    memory.mkdir()
    monkeypatch.setattr(wfv, "WALK_FORWARD_HEARTBEAT", tmp_path / "walk_forward_heartbeat.json")
    monkeypatch.setattr(wfv, "WALK_FORWARD_PID", tmp_path / "walk_forward_validator.pid")
    monkeypatch.setattr(wfv, "HOLDOUT_REGISTRY", memory / "holdout.jsonl")
    applied = tmp_path / "applied.jsonl"
    pending = tmp_path / "pending.jsonl"
    reviews = tmp_path / "reviews.jsonl"
    wfv.append_jsonl(applied, {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-24T10:00:00+00:00"})
    wfv.append_jsonl(reviews, review_row("fade", "2026-06-24T10:05:00+00:00", 0.2, "r1"))

    result = wfv.run_once(applied_path=applied, pending_path=pending, reviews_path=reviews, history_path=tmp_path / "history.jsonl", latest_path=tmp_path / "latest.json", walk_forward_path=tmp_path / "wf.json", min_test_trades=1)

    assert result["review_watermark"]
    assert wfv.WALK_FORWARD_HEARTBEAT.exists()
    assert wfv.WALK_FORWARD_PID.exists()

def test_walk_forward_result_propagates_manifest_digests_for_promotion():
    patch = {
        "patch_id": "p1",
        "setup_id": "fade",
        "applied_at": "2026-06-24T10:00:00+00:00",
        "metric_manifest_digest": "m",
        "code_config_digest": "c",
        "candidate_policy_digest": "p",
        "frozen_partition_digest": "f",
    }
    row = review_row("fade", "2026-06-24T10:05:00+00:00", 0.2, "r1")

    result = wfv.evaluate_patch_walk_forward(patch, [row], min_test_trades=1)

    assert result["metric_manifest_digest"] == "m"
    assert result["cited_metric_manifest_digest"] == "m"
    assert result["code_config_digest"] == "c"

def test_walk_forward_migration_patch_requires_rollback_and_compatibility_proof():
    patch = {"patch_id": "p1", "setup_id": "fade", "applied_at": "2026-06-24T10:00:00+00:00", "migration_backed": True}
    row = review_row("fade", "2026-06-24T10:05:00+00:00", 0.2, "r1")

    result = wfv.evaluate_patch_walk_forward(patch, [row], min_test_trades=1)

    assert result["status"] == "failed"
    assert "missing_rollback_rehearsal" in result["errors"]


def test_setup_ranker_penalizes_high_winrate_bad_expectancy(tmp_path: Path):
    rows = [
        {"setup_id": "pretty_wr_bad_exp", "trades": 100, "win_rate": 0.9, "expectancy": -0.02, "profit_factor": 0.8},
        {"setup_id": "lower_wr_good_exp", "trades": 80, "win_rate": 0.52, "expectancy": 0.05, "profit_factor": 1.4},
    ]

    result = setup_ranker.rank_setups(rows, output_path=tmp_path / "rankings.json")

    assert result["top_setup_id"] == "lower_wr_good_exp"


def test_setup_ranker_fuses_review_and_counterfactual_evidence(tmp_path: Path):
    library = {"skills": {"weak": {"setup_id": "weak", "stats": {"trades": 50, "win_rate": 0.6, "expectancy": 0.02}, "metadata": {}}}}
    reviews = [
        {"review_id": f"r{i}", "classification": "bad_loss", "source_trade": {"trade_id": f"t{i}", "setup_id": "weak", "net": "-0.2"}}
        for i in range(30)
    ]
    replays = [
        {"signal_id": f"t{i}", "status": "complete", "conclusion": "parameter_improvement_candidate"}
        for i in range(12)
    ]
    rows = setup_ranker.build_setup_evidence_rows(library, reviews=reviews, replays=replays, shadow_rows=[])

    result = setup_ranker.rank_setups(rows, output_path=tmp_path / "rankings.json")
    ranked = result["rankings"][0]

    assert ranked["setup_id"] == "weak"
    assert ranked["review_sample"] == 30
    assert ranked["bad_loss_rate"] == 1.0
    assert ranked["parameter_instability_rate"] == 1.0
    assert ranked["allocation_hint"] == "reduced"
    assert "bad_loss_cluster" in ranked["rank_reasons"]


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
    assert result["max_loss_usdt"] == 2.92
    assert result["can_trade_live"] is False


def test_allocation_uses_reduced_hint_multiplier(tmp_path: Path):
    rankings = [{"setup_id": "weak", "under_sampled": False, "evidence_expectancy": 0.02, "expectancy": 0.02, "rank_score": 0.4, "allocation_hint": "reduced", "risk_multiplier": 0.35, "rank_reasons": ["bad_loss_cluster"]}]

    result = cap.allocate_capital("weak", rankings, {"equity": "100"}, output_path=tmp_path / "alloc.json")

    assert result["allowed"] is True
    assert result["tier"] == "reduced_paper"
    assert result["max_loss_usdt"] == 2.69
    assert result["risk_fraction"] == 0.0269
    assert result["sizing_mode"] == "adaptive_paper"
    assert result["rank_reasons"] == ["bad_loss_cluster"]


def test_allocation_caps_strong_setup_at_five_percent_paper_risk(tmp_path: Path):
    rankings = [{"setup_id": "strong", "under_sampled": False, "evidence_expectancy": 0.2, "rank_score": 4.0, "allocation_hint": "normal", "risk_multiplier": 1.0, "sample_confidence": 1.0}]

    result = cap.allocate_capital("strong", rankings, {"equity": "100"}, output_path=tmp_path / "alloc.json")

    assert result["allowed"] is True
    assert result["tier"] == "normal_paper"
    assert result["risk_fraction"] == 0.05
    assert result["max_loss_usdt"] == 5.0
    assert result["can_trade_live"] is False


def test_allocation_reduces_size_when_exposure_is_high(tmp_path: Path):
    rankings = [{"setup_id": "strong", "under_sampled": False, "evidence_expectancy": 0.2, "rank_score": 4.0, "allocation_hint": "normal", "risk_multiplier": 1.0, "sample_confidence": 1.0}]

    result = cap.allocate_capital("strong", rankings, {"equity": "100", "open_margin": "46"}, output_path=tmp_path / "alloc.json")

    assert result["allowed"] is True
    assert result["risk_fraction"] < 0.05
    assert result["sizing_factors"]["exposure_penalty"] > 0


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

def test_skill_forge_main_once_writes_daemon_heartbeat(monkeypatch, tmp_path: Path):
    memory = tmp_path / "agent_memory"
    memory.mkdir()
    monkeypatch.setattr(sfa, "STATE_DIR", tmp_path)
    monkeypatch.setattr(sfa, "MEMORY_DIR", memory)
    monkeypatch.setattr(sfa, "PID_FILE", tmp_path / "skill_forge_agent.pid")
    monkeypatch.setattr(sfa, "HEARTBEAT_PATH", tmp_path / "skill_forge_agent_heartbeat.json")
    monkeypatch.setattr(sfa, "STOP_FILE", tmp_path / "STOP_SKILL_FORGE_AGENT")
    monkeypatch.setattr(sfa, "POST_TRADE_REVIEWS", memory / "post_trade_reviews.jsonl")
    monkeypatch.setattr(sfa, "PATCHES_PENDING", memory / "skill_patches_pending.jsonl")
    monkeypatch.setattr(sfa, "PATCHES_APPLIED", memory / "skill_patches_applied.jsonl")
    monkeypatch.setattr(sfa, "PATCHES_REVERTED", memory / "skill_patches_reverted.jsonl")
    monkeypatch.setattr(sfa, "PATCH_REVIEWS", memory / "skill_patch_reviews.jsonl")
    monkeypatch.setattr(sfa, "SKILL_FORGE_LATEST", memory / "skill_forge_latest.json")
    monkeypatch.setattr(sfa, "SKILL_FORGE_HISTORY", memory / "skill_forge_history.jsonl")
    monkeypatch.setattr(sfa, "SKILL_PATCH_INTEGRATION_LATEST", memory / "skill_patch_integration_latest.json")

    code = sfa.main(["--once", "--min-sample", "9999", "--interval-seconds", "1"])

    assert code == 0
    assert sfa.PID_FILE.exists()
    assert sfa.HEARTBEAT_PATH.exists()
    assert sfa.SKILL_FORGE_LATEST.exists()


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
