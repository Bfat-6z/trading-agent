import sqlite3
from pathlib import Path

import agent_work_queue as awq
import event_store as es


def add_candidate(db: Path, candidate_id: str, priority: int = 50, sequence: int = 0) -> str:
    result = es.append_event_envelope(
        "candidate.generated",
        {"candidate_id": candidate_id, "symbol": "BTCUSDT", "side": "LONG"},
        "paper_candidate_feeder",
        "paper_candidate_feeder",
        candidate_id,
        db_path=db,
        priority=priority,
        sequence=sequence,
    )
    assert result["ok"] is True
    return result["event_id"]


def test_consumer_reads_only_unacked_events(tmp_path: Path):
    db = tmp_path / "bus.db"
    add_candidate(db, "c1", sequence=1)
    sub = es.create_subscription("consumer1", ["candidate.generated"], db_path=db)

    first = es.read_events(sub["subscription_id"], limit=1, db_path=db, lease_seconds=60)
    second = es.read_events(sub["subscription_id"], limit=1, db_path=db, lease_seconds=60)

    assert first["count"] == 1
    assert second["count"] == 0


def test_priority_events_claim_before_low_priority(tmp_path: Path):
    db = tmp_path / "bus.db"
    low = add_candidate(db, "low", priority=10, sequence=1)
    high = es.append_event_envelope(
        "candidate.selected",
        {"candidate_id": "high", "symbol": "BTCUSDT", "side": "LONG"},
        "autonomous_paper_trading_brain",
        "autonomous_paper_trading_brain",
        "high",
        db_path=db,
        priority=99,
        sequence=2,
    )["event_id"]
    sub = es.create_subscription("consumer1", ["candidate.generated", "candidate.selected"], db_path=db)

    batch = es.read_events(sub["subscription_id"], limit=2, db_path=db)

    assert [event["event_id"] for event in batch["events"]] == [high, low]


def test_replay_from_cursor_is_deterministic(tmp_path: Path):
    db = tmp_path / "bus.db"
    first = add_candidate(db, "c1", sequence=1)
    second = add_candidate(db, "c2", sequence=2)

    replay1 = es.replay_events(db_path=db, event_types=["candidate.generated"], after_seq=0)
    replay2 = es.replay_events(db_path=db, event_types=["candidate.generated"], after_seq=0)

    assert [event["event_id"] for event in replay1["events"]] == [first, second]
    assert [event["event_id"] for event in replay2["events"]] == [first, second]


def test_failed_event_retries_then_dlqs(tmp_path: Path):
    db = tmp_path / "bus.db"
    event_id = add_candidate(db, "bad", sequence=1)
    sub = es.create_subscription("consumer1", ["candidate.generated"], db_path=db)
    first = es.read_events(sub["subscription_id"], limit=1, db_path=db)
    first_bus = first["events"][0]["bus"]
    retry = es.fail_event(sub["subscription_id"], event_id, first_bus["lease_token"], first_bus["attempt_id"], "boom", db_path=db, max_retries=2)
    second = es.read_events(sub["subscription_id"], limit=1, db_path=db)
    second_bus = second["events"][0]["bus"]
    dlq = es.fail_event(sub["subscription_id"], event_id, second_bus["lease_token"], second_bus["attempt_id"], "boom", db_path=db, max_retries=2)
    after = es.read_events(sub["subscription_id"], limit=1, db_path=db)

    assert retry["state"] == "retry"
    assert dlq["state"] == "dlq"
    assert after["count"] == 0
    assert es.bus_health(db)["dlq_count"] == 1


def test_stale_ack_after_lease_expiry_is_rejected(tmp_path: Path):
    db = tmp_path / "bus.db"
    event_id = add_candidate(db, "c1", sequence=1)
    sub = es.create_subscription("consumer1", ["candidate.generated"], db_path=db)
    batch = es.read_events(sub["subscription_id"], limit=1, db_path=db, lease_seconds=1)
    bus = batch["events"][0]["bus"]
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE event_attempts SET lease_expires_at='2026-01-01T00:00:00+00:00' WHERE attempt_id=?", (bus["attempt_id"],))

    result = es.ack_events(sub["subscription_id"], [event_id], bus["lease_token"], bus["attempt_id"], expected_seq=bus["seq"], db_path=db)

    assert result["ok"] is False
    assert any(error.startswith("attempt_not_leased") for error in result["errors"])


def test_filtered_subscription_cannot_skip_unacked_same_type(tmp_path: Path):
    db = tmp_path / "bus.db"
    first = add_candidate(db, "c1", priority=10, sequence=1)
    second = add_candidate(db, "c2", priority=99, sequence=2)
    sub = es.create_subscription("consumer1", ["candidate.generated"], db_path=db)
    batch = es.read_events(sub["subscription_id"], limit=2, db_path=db)
    later = next(event for event in batch["events"] if event["event_id"] == second)
    bus = later["bus"]

    result = es.ack_events(sub["subscription_id"], [second], bus["lease_token"], bus["attempt_id"], expected_seq=bus["seq"], db_path=db)

    assert first != second
    assert result["ok"] is False
    assert any(error.startswith("ack_would_skip_unacked_event") for error in result["errors"])


def test_job_lifecycle_emits_bus_events(tmp_path: Path):
    jobs = tmp_path / "jobs.sqlite"
    bus = tmp_path / "bus.db"
    enq = awq.enqueue_job("market_scan", {"symbol": "BTCUSDT"}, db_path=jobs, event_db_path=bus)
    claimed = awq.claim_next("worker1", db_path=jobs, event_db_path=bus)
    awq.complete_job(enq["job_id"], ok=True, db_path=jobs, event_db_path=bus)

    replay = es.replay_events(db_path=bus, event_types=["job.lifecycle"])

    assert claimed["job_id"] == enq["job_id"]
    assert [event["payload"]["status"] for event in replay["events"]] == ["queued", "running", "done"]
    assert replay["events"][0]["producer_id"] == "agent_work_queue"


def test_replay_manifest_missing_data_is_non_replayable(tmp_path: Path):
    result = es.replay_with_manifest({"manifest_id": "m1"}, db_path=tmp_path / "bus.db")

    assert result["ok"] is False
    assert result["non_replayable_reason"]
    assert any(error.startswith("missing_manifest") for error in result["errors"])


def test_bus_health_history_and_backpressure(tmp_path: Path):
    db = tmp_path / "bus.db"
    add_candidate(db, "c1", sequence=1)
    sub = es.create_subscription("consumer1", ["candidate.generated"], db_path=db)
    es.read_events(sub["subscription_id"], limit=1, db_path=db)

    health = es.write_bus_health(db, latest_path=tmp_path / "latest.json", history_path=tmp_path / "history.jsonl")
    pressure = es.evaluate_backpressure(health, {"max_unacked": 0, "max_dlq": 0})

    assert health["unacked_count"] == 1
    assert pressure["pause_low_priority_producers"] is True
    assert (tmp_path / "latest.json").exists()
    assert (tmp_path / "history.jsonl").exists()


def test_live_backup_restores_to_matching_bus_health(tmp_path: Path):
    db = tmp_path / "bus.db"
    add_candidate(db, "c1", sequence=1)
    backup = tmp_path / "backup" / "bus.db"
    manifest = es.create_live_backup(db, backup, tmp_path / "backup" / "manifest.json", owner="owner", checker="checker", restore_approver="approver")
    restored = es.validate_restore_replay(db, backup)

    assert manifest["sha256"]
    assert manifest["owner"] == "owner"
    assert restored["ok"] is True


def test_erasure_receipt_preserves_metadata_not_payload(tmp_path: Path):
    row = es.append_erasure_receipt("payload_1", "user_delete", "key_1", receipt_path=tmp_path / "erasure.jsonl")

    assert row["payload_recoverable"] is False
    assert row["metadata_hash"].startswith("sha256:")
    assert (tmp_path / "erasure.jsonl").exists()


def test_cutover_and_dual_write_contracts_are_explicit():
    assert es.dual_write_shadow_counts(3, 3)["ok"] is True
    failed = es.cutover_checklist_status({"snapshot_backup": True})

    assert failed["ok"] is False
    assert "drain_unacked" in failed["missing"]
