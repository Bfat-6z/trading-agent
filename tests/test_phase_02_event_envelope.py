import sqlite3
from pathlib import Path

import agent_data_contracts as contracts
import event_store as es


def paper_close_payload(trade_id: str = "t1") -> dict:
    return {"trade_id": trade_id, "symbol": "BTCUSDT", "side": "LONG", "entry": "100", "exit": "101"}


def test_missing_envelope_schema_version_rejects_event():
    envelope = es.build_event_envelope("paper.close", paper_close_payload(), "paper_execution_lifecycle_loop", "paper_execution_lifecycle_loop", "t1")
    envelope.pop("schema_version")

    result = contracts.validate_event_envelope(envelope)

    assert result.ok is False
    assert "missing_envelope:schema_version" in result.errors
    assert "invalid_envelope_schema_version" in result.errors


def test_same_idempotency_key_dedupes_and_conflict_rejects(tmp_path: Path):
    db = tmp_path / "events.db"
    first = es.append_event_envelope(
        "paper.close",
        paper_close_payload("t1"),
        "paper_execution_lifecycle_loop",
        "paper_execution_lifecycle_loop",
        "t1",
        db_path=db,
        idempotency_key="paper:t1",
    )
    duplicate = es.append_event_envelope(
        "paper.close",
        paper_close_payload("t1"),
        "paper_execution_lifecycle_loop",
        "paper_execution_lifecycle_loop",
        "t1",
        db_path=db,
        idempotency_key="paper:t1",
    )
    conflict = es.append_event_envelope(
        "paper.close",
        paper_close_payload("t2"),
        "paper_execution_lifecycle_loop",
        "paper_execution_lifecycle_loop",
        "t2",
        db_path=db,
        idempotency_key="paper:t1",
    )

    assert first["inserted"] is True
    assert duplicate["deduped"] is True
    assert duplicate["event_id"] == first["event_id"]
    assert conflict["ok"] is False
    assert "idempotency_payload_conflict" in conflict["errors"]


def test_provenance_missing_rejects_high_value_news_event(tmp_path: Path):
    result = es.append_event_envelope(
        "news.snapshot.captured",
        {"snapshot_id": "n1", "source_id": "news"},
        "news_observer",
        "news_observer",
        "n1",
        db_path=tmp_path / "events.db",
    )

    assert result["ok"] is False
    assert "missing_provenance_id" in result["errors"]


def test_time_order_is_utc_normalized(tmp_path: Path):
    envelope = es.build_event_envelope(
        "candidate.generated",
        {"candidate_id": "c1", "symbol": "BTCUSDT", "side": "LONG"},
        "paper_candidate_feeder",
        "paper_candidate_feeder",
        "c1",
        occurred_at="2026-06-21T07:00:00+07:00",
    )
    result = es.append_enveloped_event(envelope, db_path=tmp_path / "events.db")

    assert result["ok"] is True
    assert envelope["occurred_at"] == "2026-06-21T00:00:00+00:00"


def test_unauthorized_producer_cannot_append_scoring_or_memory_event(tmp_path: Path):
    result = es.append_event_envelope(
        "promotion.decision",
        {"decision_id": "p1", "state": "paper_learning", "passed": False},
        "paper_candidate_feeder",
        "promotion_evaluator_loop",
        "p1",
        db_path=tmp_path / "events.db",
    )

    assert result["ok"] is False
    assert "unauthorized_producer" in result["errors"]


def test_duplicate_fill_transaction_dedupes_even_with_new_envelope(tmp_path: Path):
    db = tmp_path / "events.db"
    payload = {"venue": "paper", "account_mode": "paper", "order_id": "o1", "fill_id": "f1", "symbol": "BTCUSDT", "side": "LONG", "qty": "1", "price": "100"}
    first = es.append_event_envelope("paper.fill", payload, "paper_execution_lifecycle_loop", "paper_execution_lifecycle_loop", "o1", db_path=db, sequence=1)
    second = es.append_event_envelope("paper.fill", payload, "paper_execution_lifecycle_loop", "paper_execution_lifecycle_loop", "o1", db_path=db, sequence=2)

    assert first["inserted"] is True
    assert second["deduped"] is True
    assert second["ledger_transaction_deduped"] is True
    assert second["event_id"] == first["event_id"]


def test_signed_audit_hash_chain_detects_tamper(tmp_path: Path):
    db = tmp_path / "events.db"
    first = es.append_event_envelope(
        "legacy_script_blocked",
        {"denial_id": "d1", "path": "execute.py", "reason": "blocked"},
        "legacy_live_blocker",
        "legacy_live_blocker",
        "d1",
        db_path=db,
        sequence=1,
    )
    second = es.append_event_envelope(
        "operator_command.denied",
        {"command_id": "c1", "operator_id": "op", "reason": "no_role"},
        "legacy_live_blocker",
        "legacy_live_blocker",
        "c1",
        db_path=db,
        sequence=2,
    )

    assert first["audit_hash"]
    assert second["audit_hash"]
    assert es.verify_audit_chain(db)["ok"] is True
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE audit_hash_chain SET audit_hash='sha256:tampered' WHERE event_id=?", (first["event_id"],))
    assert es.verify_audit_chain(db)["ok"] is False


def test_cutoff_proof_rejects_future_known_inputs(tmp_path: Path):
    db = tmp_path / "events.db"
    envelope = es.build_event_envelope(
        "candidate.generated",
        {"candidate_id": "c1", "symbol": "BTCUSDT", "side": "LONG"},
        "paper_candidate_feeder",
        "paper_candidate_feeder",
        "c1",
        occurred_at="2026-06-21T00:00:00+00:00",
        available_at="2026-06-21T00:00:00+00:00",
        known_at="2026-06-21T00:10:00+00:00",
    )
    inserted = es.append_enveloped_event(envelope, db_path=db)

    result = es.validate_cutoff_proof([inserted["event_id"]], {"max_known_at": "2026-06-21T00:05:00+00:00", "max_available_at": "2026-06-21T00:05:00+00:00"}, db_path=db)

    assert result["ok"] is False
    assert any(error.startswith("known_after_cutoff") for error in result["errors"])


def test_legacy_jsonl_backfill_manifest_maps_or_quarantines(tmp_path: Path):
    path = tmp_path / "legacy.jsonl"
    path.write_text('{"trade_id":"t1","symbol":"BTCUSDT","side":"LONG","entry":"100","exit":"101"}\nnot json\n', encoding="utf-8")

    manifest = es.dry_run_backfill_manifest([path], "paper.close", "paper_execution_lifecycle_loop", "paper_execution_lifecycle_loop")

    assert manifest["mapped_count"] == 1
    assert manifest["quarantined_count"] == 1
    assert manifest["files"][0]["sha256"]
    assert manifest["can_place_live_orders"] is False
