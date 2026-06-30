from pathlib import Path

import agent_work_queue as awq
import atomic_state
import self_model


def patch_self_model_paths(monkeypatch, tmp_path: Path):
    memory = tmp_path / "agent_memory"
    memory.mkdir()
    monkeypatch.setattr(self_model, "STATE_DIR", tmp_path)
    monkeypatch.setattr(self_model, "MEMORY_DIR", memory)
    monkeypatch.setattr(self_model, "SELF_MODEL_LATEST", memory / "self_model_latest.json")
    monkeypatch.setattr(self_model, "HEARTBEAT_PATH", tmp_path / "self_model_heartbeat.json")
    return memory


def test_low_replay_coverage_queues_replay_task(monkeypatch, tmp_path: Path):
    memory = patch_self_model_paths(monkeypatch, tmp_path)
    atomic_state.write_json_atomic(
        memory / "test_result_memory_latest.json",
        {
            "known_gaps": ["counterfactual_coverage_low"],
            "priority_curriculum": [
                {
                    "priority": "high",
                    "gap": "counterfactual_coverage_low",
                    "priority_score": 9,
                    "occurrences": 3,
                    "task": "raise replay coverage",
                    "action": "run counterfactual replay",
                    "source": "counterfactual",
                }
            ],
        },
    )

    model = self_model.build_self_model()
    job = awq.claim_next_of_types("test-worker", ["replay_batch"], db_path=tmp_path / "agent_jobs.sqlite")

    assert model["can_place_live_orders"] is False
    assert model["homework_score"]["assigned"] >= 1
    assert model["work_queue"]["queued_count"] >= 1
    assert job is not None
    assert job["job_type"] == "replay_batch"
    assert job["payload"]["can_place_live_orders"] is False
    assert "counterfactual_coverage_low" in job["payload"]["evidence_ids"]


def test_builtin_curriculum_tasks_have_evidence_ids():
    plan = self_model.build_curriculum_tasks(
        [{"priority": "high", "task": "collect and review closed paper trades", "source": "self_model"}],
        [],
        "2026-06-29T00:00:00+00:00",
    )

    assert plan["planned"][0]["evidence_ids"]
    assert plan["planned"][0]["evidence_ids"][0].startswith("curriculum_")


def test_weak_setup_maps_to_setup_review_task():
    plan = self_model.build_curriculum_tasks(
        [{"priority": "medium", "gap": "weak_setup_skills", "task": "review weak setup", "setup_id": "fade"}],
        [],
        "2026-06-29T00:00:00+00:00",
    )

    assert plan["planned"][0]["job_type"] == "setup_review"
    assert plan["planned"][0]["can_loosen_risk"] is False


def test_repeated_failure_rises_in_priority():
    base = self_model.curriculum_priority({"priority": "medium", "gap": "counterfactual_coverage_low", "occurrences": 0})
    repeated = self_model.curriculum_priority({"priority": "medium", "gap": "counterfactual_coverage_low", "occurrences": 4})

    assert repeated > base


def test_self_model_task_is_traceable_to_completion(tmp_path: Path):
    task = self_model.build_curriculum_tasks(
        [{"priority": "high", "gap": "walk_forward_not_done", "task": "run experiment replay", "action": "experiment replay"}],
        [],
        "2026-06-29T00:00:00+00:00",
    )["planned"][0]
    report = self_model.enqueue_curriculum_tasks([task], db_path=tmp_path / "jobs.sqlite", history_path_arg=tmp_path / "curriculum.jsonl")
    claimed = awq.claim_next("worker", db_path=tmp_path / "jobs.sqlite")
    awq.complete_job(claimed["job_id"], ok=True, db_path=tmp_path / "jobs.sqlite")

    assert report["queued_count"] == 1
    assert claimed["job_id"] == task["curriculum_task_id"]
    assert awq.queue_summary(tmp_path / "jobs.sqlite")["by_status"]["done"] == 1
    assert atomic_state.read_jsonl(tmp_path / "curriculum.jsonl")[0]["curriculum_signature"] == task["curriculum_signature"]


def test_active_duplicate_queue_job_throttles_same_signature(tmp_path: Path):
    item = {"priority": "high", "gap": "counterfactual_coverage_low", "task": "run counterfactual replay"}
    first = self_model.build_curriculum_tasks([item], [], "2026-06-29T00:00:00+00:00")["planned"][0]
    self_model.enqueue_curriculum_tasks([first], db_path=tmp_path / "jobs.sqlite", history_path_arg=tmp_path / "curriculum.jsonl")

    second = self_model.build_curriculum_tasks([item], [], "2026-06-29T07:00:00+00:00", active_signatures=self_model.active_queue_signatures(tmp_path / "jobs.sqlite"))

    assert second["planned"] == []
    assert second["throttled"][0]["anti_loop"]["reason"] == "curriculum_duplicate_active_job"


def test_failed_sqlite_job_counts_toward_retry_breaker(tmp_path: Path):
    item = {"priority": "high", "task": "collect and review closed paper trades", "source": "self_model"}
    task = self_model.build_curriculum_tasks([item], [], "2026-06-29T00:00:00+00:00")["planned"][0]
    self_model.enqueue_curriculum_tasks([task], db_path=tmp_path / "jobs.sqlite", history_path_arg=tmp_path / "curriculum.jsonl")
    awq.complete_job(task["curriculum_task_id"], ok=False, db_path=tmp_path / "jobs.sqlite")
    history_path = tmp_path / "manual_curriculum.jsonl"
    for ts in ("2026-06-29T00:00:00+00:00", "2026-06-29T07:00:00+00:00", "2026-06-29T14:00:00+00:00"):
        atomic_state.append_jsonl(history_path, {**task, "queued_at": ts, "status": "queued"})
    rows = self_model.refreshed_history_rows(atomic_state.read_jsonl(history_path), tmp_path / "jobs.sqlite")

    plan = self_model.build_curriculum_tasks([item], rows, "2026-06-29T07:00:00+00:00")

    assert plan["planned"] == []
    assert plan["throttled"][0]["anti_loop"]["reason"] == "self_generated_retry_circuit_breaker"


def test_repeated_self_generated_task_loop_is_throttled():
    item = {"priority": "high", "task": "collect and review closed paper trades", "source": "self_model"}
    signature = self_model.curriculum_signature(item)
    history = [
        {"curriculum_signature": signature, "queued_at": "2026-06-29T00:00:00+00:00", "status": "failed", "source_partition": "self_generated"},
        {"curriculum_signature": signature, "queued_at": "2026-06-29T00:00:01+00:00", "status": "failed", "source_partition": "self_generated"},
        {"curriculum_signature": signature, "queued_at": "2026-06-29T00:00:02+00:00", "status": "failed", "source_partition": "self_generated"},
    ]

    plan = self_model.build_curriculum_tasks([item], history, "2026-06-29T07:00:00+00:00")

    assert plan["planned"] == []
    assert plan["throttled"][0]["anti_loop"]["reason"] == "self_generated_retry_circuit_breaker"


def test_curriculum_cooldown_throttles_recent_duplicate():
    item = {"priority": "high", "gap": "counterfactual_coverage_low", "task": "run counterfactual replay"}
    signature = self_model.curriculum_signature(item)
    history = [{"curriculum_signature": signature, "queued_at": "2026-06-29T00:00:00+00:00", "status": "queued", "source_partition": "evidence_backed"}]

    plan = self_model.build_curriculum_tasks([item], history, "2026-06-29T01:00:00+00:00")

    assert plan["planned"] == []
    assert plan["throttled"][0]["anti_loop"]["reason"] == "curriculum_cooldown_active"
