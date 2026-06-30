from pathlib import Path

import experiment_registry as exp

def build_job(**overrides):
    payload = {
        "hypothesis": "tight stop improves expectancy",
        "setup_id": "funding_squeeze",
        "variant": {"sl_mult": 1.0, "tp_mult": 1.0},
        "data_window": {"start": "2026-06-21T00:00:00+00:00", "end": "2026-06-21T01:00:00+00:00"},
        "config": {"fees": "binance_usdm_v1"},
        "setup_contract_hash": "setup_hash_v1",
        "actor_id": "tester",
    }
    payload.update(overrides)
    return exp.build_experiment_job(**payload)

def test_experiment_swarm_dedupes_variant_but_allows_new_window(tmp_path: Path):
    db = tmp_path / "experiments.sqlite"
    queue = tmp_path / "jobs.sqlite"
    latest = tmp_path / "latest.json"
    job = build_job()

    first = exp.enqueue_experiment_job(job, db_path=db, queue_db_path=queue, latest_path=latest)
    duplicate = exp.enqueue_experiment_job(job, db_path=db, queue_db_path=queue, latest_path=latest)
    new_window = exp.enqueue_experiment_job(
        build_job(data_window={"start": "2026-06-21T01:00:00+00:00", "end": "2026-06-21T02:00:00+00:00"}),
        db_path=db,
        queue_db_path=queue,
        latest_path=latest,
    )

    assert first["ok"] is True and first["inserted"] is True
    assert duplicate["ok"] is True and duplicate["inserted"] is False
    assert new_window["ok"] is True and new_window["inserted"] is True
    summary = exp.write_swarm_latest(db, latest)
    assert summary["experiment_count"] == 2
    assert summary["can_place_live_orders"] is False

def test_experiment_worker_failure_retries_then_dlqs(tmp_path: Path):
    db = tmp_path / "experiments.sqlite"
    job = build_job()
    job["max_retries"] = 1
    exp.enqueue_experiment_job(job, db_path=db, queue_db_path=tmp_path / "jobs.sqlite")

    claimed = exp.claim_experiment_job("worker1", db_path=db)
    first_fail = exp.complete_experiment_job(claimed["experiment_id"], ok=False, error="boom", db_path=db)
    claimed_again = exp.claim_experiment_job("worker1", db_path=db)
    second_fail = exp.complete_experiment_job(claimed_again["experiment_id"], ok=False, error="boom", db_path=db)

    assert first_fail["status"] == "queued"
    assert second_fail["status"] == "dlq"

def test_family_correction_blocks_best_variant_until_alpha_passes(tmp_path: Path):
    db = tmp_path / "experiments.sqlite"
    queue = tmp_path / "jobs.sqlite"
    j1 = build_job(variant={"sl_mult": 1.0})
    j2 = build_job(variant={"sl_mult": 0.5})
    exp.enqueue_experiment_job(j1, db_path=db, queue_db_path=queue)
    exp.enqueue_experiment_job(j2, db_path=db, queue_db_path=queue)

    weak = exp.record_experiment_result(j1, {"expectancy_after_fees": 0.1, "p_value": 0.04}, db_path=db)
    strong = exp.record_experiment_result(j2, {"expectancy_after_fees": 0.1, "p_value": 0.01}, db_path=db)

    assert weak["status"] == "failed"
    assert weak["corrected_alpha"] == 0.025
    assert strong["status"] == "passed"

def test_experiment_rejects_unknown_setup_contract_hash(tmp_path: Path):
    db = tmp_path / "experiments.sqlite"
    result = exp.enqueue_experiment_job(build_job(setup_contract_hash=None), db_path=db, queue_db_path=tmp_path / "jobs.sqlite")

    assert result["ok"] is False
    assert "unknown_setup_contract_hash" in result["errors"]

def test_actor_family_quota_rejects_swarm_spam(tmp_path: Path):
    db = tmp_path / "experiments.sqlite"
    queue = tmp_path / "jobs.sqlite"
    first = exp.enqueue_experiment_job(build_job(variant={"sl_mult": 1.0}), db_path=db, queue_db_path=queue, max_jobs_per_actor_family=1)
    second = exp.enqueue_experiment_job(build_job(variant={"sl_mult": 0.5}), db_path=db, queue_db_path=queue, max_jobs_per_actor_family=1)

    assert first["ok"] is True
    assert second["ok"] is False
    assert "actor_family_quota_exceeded" in second["errors"]

def test_abandoned_hypothesis_remains_in_registry(tmp_path: Path):
    row = exp.record_hypothesis("spread filter helps only in high vol", "exhaustion_fade", status="abandoned", reason="insufficient_candles", db_path=tmp_path / "experiments.sqlite")

    assert row["status"] == "abandoned"
    assert row["reason"] == "insufficient_candles"
    assert row["can_place_live_orders"] is False
