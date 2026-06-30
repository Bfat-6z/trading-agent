from pathlib import Path

import atomic_state
import data_hygiene_auditor as dha
import dont_do_memory as ddm
import memory_consolidation_agent as mca
import memory_retrieval as mr
import self_model


def patch_memory_paths(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(mca, "CANDIDATES_JSONL", tmp_path / "memory_candidates.jsonl")
    monkeypatch.setattr(mca, "OVERFLOW_JSONL", tmp_path / "memory_candidates_overflow.jsonl")
    monkeypatch.setattr(mca, "PROMOTED_JSONL", tmp_path / "memory_promoted.jsonl")
    monkeypatch.setattr(mca, "REJECTED_JSONL", tmp_path / "memory_rejected.jsonl")
    monkeypatch.setattr(mca, "SKILL_FORGE_QUEUE_JSONL", tmp_path / "memory_skill_forge_queue.jsonl")
    monkeypatch.setattr(mca, "RETRIEVAL_DIRTY_JSONL", tmp_path / "memory_retrieval_dirty.jsonl")
    monkeypatch.setattr(mca, "LATEST_JSON", tmp_path / "memory_consolidation_latest.json")
    monkeypatch.setattr(mca, "HISTORY_JSONL", tmp_path / "memory_consolidation_history.jsonl")
    monkeypatch.setattr(mca, "HEARTBEAT_PATH", tmp_path / "memory_consolidation_agent_heartbeat.json")
    monkeypatch.setattr(mca, "EPISODES_JSONL", tmp_path / "episodes.jsonl")
    monkeypatch.setattr(mca, "POST_TRADE_REVIEWS_JSONL", tmp_path / "post_trade_reviews.jsonl")
    monkeypatch.setattr(mca, "COUNTERFACTUAL_JSONL", tmp_path / "counterfactual.jsonl")
    monkeypatch.setattr(mca, "LEGACY_COUNTERFACTUAL_JSONL", tmp_path / "legacy_counterfactual.jsonl")
    monkeypatch.setattr(mca, "DAILY_EXAM_HISTORY_JSONL", tmp_path / "daily_exam.jsonl")
    monkeypatch.setattr(mca, "TEST_RESULT_MEMORY_JSONL", tmp_path / "test_result_memory.jsonl")
    monkeypatch.setattr(mca, "LLM_REASONING_HISTORY_JSONL", tmp_path / "llm_reasoning.jsonl")
    monkeypatch.setattr(mca, "BELIEF_LEDGER_PATH", tmp_path / "belief_ledger.json")
    monkeypatch.setattr(mca, "DONT_DO_PATH", tmp_path / "dont_do_memory.json")
    monkeypatch.setattr(mca, "MEMORY_CONTROL_PATH", tmp_path / "memory_consolidation_control.json")


def test_one_anecdote_cannot_promote_memory(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)

    summary = mca.consolidate([{"episode_id": "e1", "trigger": "paper_close", "lesson": "avoid chasing thin pump"}])

    assert summary["promoted_count"] == 0
    assert summary["rejected_count"] == 1
    assert "insufficient_recall_count" in summary["rejected"][0]["errors"]


def test_repeated_lesson_across_contexts_promotes(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    rows = [
        {"episode_id": "e1", "trigger": "paper_close", "lesson": "stop too tight after valid momentum entry", "trade_id": "t1"},
        {"review_id": "r1", "_memory_source_type": "post_trade_review", "lesson": "stop too tight after valid momentum entry", "trade_id": "t2"},
    ]

    summary = mca.consolidate(rows)

    assert summary["promoted_count"] == 1
    assert summary["promoted"][0]["recall_count"] == 2

def test_memory_consolidation_run_once_collects_learning_rows(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    atomic_state.append_jsonl(mca.EPISODES_JSONL, {"episode_id": "e1", "trigger": "paper_close", "lesson": "stop too tight after valid momentum entry", "trade_id": "t1"})
    atomic_state.append_jsonl(mca.POST_TRADE_REVIEWS_JSONL, {"review_id": "r1", "setup_id": "momentum", "classification": "stop too tight after valid momentum entry"})

    result = mca.run_once(limit_per_file=20)

    assert result["source_row_count"] == 2
    assert result["can_place_live_orders"] is False
    assert mca.LATEST_JSON.exists()
    assert mca.HEARTBEAT_PATH.exists()

def test_memory_consolidation_uses_current_counterfactual_replays_path(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    atomic_state.append_jsonl(mca.COUNTERFACTUAL_JSONL, {"replay_id": "cf1", "conclusion": "stop too tight after valid momentum entry", "signal_id": "s1"})
    atomic_state.append_jsonl(mca.LEGACY_COUNTERFACTUAL_JSONL, {"replay_id": "cf1", "conclusion": "stop too tight after valid momentum entry", "signal_id": "s1"})

    rows = mca.collect_learning_rows(limit_per_file=20)

    assert len(rows) == 1
    assert rows[0]["_memory_source_type"] == "counterfactual"

def test_memory_default_episode_path_matches_ledger_producer():
    assert mca.EPISODES_JSONL.name == "episodes.jsonl"

def test_memory_lesson_text_is_rich_market_lesson():
    text = mca.lesson_text(
        {
            "review_id": "r1",
            "classification": "bad_loss",
            "setup_id": "funding_squeeze",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "regime": "risk_on",
            "primary_failure_reason": "spread_too_wide",
            "mae": -0.4,
            "mfe": 0.1,
            "costs": {"fees": 0.03, "funding_payment": -0.01, "slippage": 0.02},
            "counterfactual": {"conclusion": "wait_one_candle_better"},
        }
    )

    assert "bad_loss" in text
    assert "setup=funding_squeeze" in text
    assert "symbol=BTCUSDT" in text
    assert "fee=0.03" in text
    assert "counterfactual=wait_one_candle_better" in text

def test_memory_rejects_fake_evidence_id(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    candidate = {
        "candidate_id": "candidate_fake",
        "text": "avoid chasing thin pump",
        "recall_count": 3,
        "unique_contexts": 3,
        "unique_days": 2,
        "trade_samples": 3,
        "contradiction_count": 0,
        "confidence_score": 0.9,
        "evidence": [{"evidence_id": "post_trade_review:missing", "payload_hash": "sha256:bad", "outcome_known_at": "2026-06-21T00:00:00+00:00", "source_type": "post_trade_review"}],
        "raw": {"trade_id": "t1"},
    }

    summary = mca.deep_promote(candidate and [candidate], evidence_index={}, promotion_cutoff="2026-06-22T00:00:00+00:00")

    assert summary["promoted_count"] == 0
    assert any("evidence_id_not_found" in error for error in summary["rejected"][0]["errors"])

def test_memory_rejects_outcome_after_promotion_cutoff(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    row = {"review_id": "r1", "_memory_source_type": "post_trade_review", "lesson": "avoid chasing thin pump", "trade_id": "t1", "outcome_known_at": "2026-06-23T00:00:00+00:00"}
    candidate = mca.rem_extract_patterns(mca.light_sleep([row]), [row])[0]

    summary = mca.deep_promote([candidate], evidence_index=mca.known_evidence_index([row]), promotion_cutoff="2026-06-22T00:00:00+00:00")

    assert summary["promoted_count"] == 0
    assert any("evidence_after_promotion_cutoff" in error for error in summary["rejected"][0]["errors"])

def test_memory_rejects_missing_evidence_payload_hash(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    row = {"review_id": "r1", "_memory_source_type": "post_trade_review", "lesson": "avoid chasing thin pump", "trade_id": "t1", "outcome_known_at": "2026-06-21T00:00:00+00:00"}
    candidate = {
        "candidate_id": "candidate_hashless",
        "text": "avoid chasing thin pump",
        "claim": "avoid chasing thin pump",
        "recall_count": 3,
        "unique_contexts": 3,
        "unique_days": 2,
        "trade_samples": 3,
        "contradiction_count": 0,
        "confidence_score": 0.9,
        "evidence": [{"evidence_id": "post_trade_review:r1", "outcome_known_at": "2026-06-21T00:00:00+00:00", "source_type": "post_trade_review"}],
        "raw": row,
    }

    summary = mca.deep_promote([candidate], evidence_index=mca.known_evidence_index([row]), promotion_cutoff="2026-06-22T00:00:00+00:00")

    assert summary["promoted_count"] == 0
    assert any("missing_evidence_payload_hash" in error for error in summary["rejected"][0]["errors"])


def test_memory_rejects_existing_evidence_id_hash_mismatch(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    row = {"review_id": "r1", "_memory_source_type": "post_trade_review", "lesson": "avoid chasing thin pump", "trade_id": "t1", "outcome_known_at": "2026-06-21T00:00:00+00:00"}
    candidate = mca.rem_extract_patterns(mca.light_sleep([row]), [row])[0]
    candidate["evidence"][0]["payload_hash"] = "sha256:forged"
    candidate.update({"recall_count": 3, "unique_contexts": 3, "unique_days": 2, "source_quorum": 2, "independent_evidence_count": 2, "confidence_score": 0.9})

    summary = mca.deep_promote([candidate], evidence_index=mca.known_evidence_index([row]), promotion_cutoff="2026-06-22T00:00:00+00:00")

    assert summary["promoted_count"] == 0
    assert any("evidence_hash_mismatch" in error for error in summary["rejected"][0]["errors"])


def test_memory_rejects_invalid_evidence_outcome_known_at(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    row = {"review_id": "r1", "_memory_source_type": "post_trade_review", "lesson": "avoid chasing thin pump", "trade_id": "t1", "outcome_known_at": "not-a-time"}
    candidate = mca.rem_extract_patterns(mca.light_sleep([row]), [row])[0]

    summary = mca.deep_promote([candidate], evidence_index=mca.known_evidence_index([row]), promotion_cutoff="2026-06-22T00:00:00+00:00")

    assert summary["promoted_count"] == 0
    assert any("invalid_evidence_outcome_known_at" in error for error in summary["rejected"][0]["errors"])

def test_memory_rejects_evidence_claim_mismatch(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    rows = [
        {"review_id": "r1", "_memory_source_type": "post_trade_review", "lesson": "avoid chasing thin pump", "trade_id": "t1", "outcome_known_at": "2026-06-21T00:00:00+00:00"},
        {"review_id": "r2", "_memory_source_type": "post_trade_review", "lesson": "avoid chasing thin pump", "trade_id": "t2", "outcome_known_at": "2026-06-22T00:00:00+00:00"},
    ]
    evidence = [mca.evidence_record(row) for row in rows]
    candidate = {
        "candidate_id": "candidate_forged",
        "text": "increase leverage after three green candles",
        "claim": "increase leverage after three green candles",
        "recall_count": 3,
        "unique_contexts": 3,
        "unique_days": 2,
        "trade_samples": 3,
        "contradiction_count": 0,
        "confidence_score": 0.9,
        "evidence": evidence,
        "raw": rows[0],
    }

    summary = mca.deep_promote([candidate], evidence_index=mca.known_evidence_index(rows), promotion_cutoff="2026-06-23T00:00:00+00:00")

    assert summary["promoted_count"] == 0
    assert any("evidence_claim_mismatch" in error for error in summary["rejected"][0]["errors"])

def test_memory_does_not_promote_label_only_bad_loss(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    rows = [
        {"review_id": "r1", "_memory_source_type": "post_trade_review", "classification": "bad_loss", "trade_id": "t1", "setup_id": "A", "outcome_known_at": "2026-06-21T00:00:00+00:00"},
        {"replay_id": "cf1", "_memory_source_type": "counterfactual", "classification": "bad_loss", "signal_id": "s1", "setup_id": "B", "outcome_known_at": "2026-06-22T00:00:00+00:00"},
    ]

    summary = mca.consolidate(rows)

    assert summary["promoted_count"] == 0
    assert summary["candidate_count"] == 0

def test_memory_requires_source_quorum(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    rows = [
        {"review_id": "r1", "_memory_source_type": "post_trade_review", "lesson": "avoid chasing thin pump", "trade_id": "t1", "outcome_known_at": "2026-06-21T00:00:00+00:00"},
        {"review_id": "r2", "_memory_source_type": "post_trade_review", "lesson": "avoid chasing thin pump", "trade_id": "t2", "outcome_known_at": "2026-06-22T00:00:00+00:00"},
    ]
    candidate = mca.rem_extract_patterns(mca.light_sleep(rows), rows)[0]

    summary = mca.deep_promote([candidate], evidence_index=mca.known_evidence_index(rows), promotion_cutoff="2026-06-23T00:00:00+00:00")

    assert summary["promoted_count"] == 0
    assert "insufficient_source_quorum" in summary["rejected"][0]["errors"]


def test_memory_recomputes_quorum_from_canonical_evidence(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    row = {"review_id": "r1", "_memory_source_type": "post_trade_review", "lesson": "avoid chasing thin pump", "trade_id": "t1", "outcome_known_at": "2026-06-21T00:00:00+00:00"}
    candidate = mca.rem_extract_patterns(mca.light_sleep([row]), [row])[0]
    candidate.update({"recall_count": 9, "unique_contexts": 9, "unique_days": 9, "source_quorum": 9, "independent_evidence_count": 9, "trade_samples": 9, "confidence_score": 0.99})

    summary = mca.deep_promote([candidate], evidence_index=mca.known_evidence_index([row]), promotion_cutoff="2026-06-22T00:00:00+00:00")

    assert summary["promoted_count"] == 0
    errors = summary["rejected"][0]["errors"]
    assert "insufficient_recall_count" in errors
    assert "insufficient_source_quorum" in errors
    assert "insufficient_independent_evidence" in errors


def test_memory_rejects_evidence_source_type_mismatch(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    rows = [
        {"review_id": "r1", "_memory_source_type": "post_trade_review", "lesson": "avoid chasing thin pump", "trade_id": "t1", "outcome_known_at": "2026-06-21T00:00:00+00:00"},
        {"replay_id": "cf1", "_memory_source_type": "counterfactual", "conclusion": "avoid chasing thin pump", "signal_id": "s1", "outcome_known_at": "2026-06-22T00:00:00+00:00"},
    ]
    candidate = mca.rem_extract_patterns(mca.light_sleep(rows), rows)[0]
    candidate["evidence"][0]["source_type"] = "counterfactual"

    summary = mca.deep_promote([candidate], evidence_index=mca.known_evidence_index(rows), promotion_cutoff="2026-06-23T00:00:00+00:00")

    assert summary["promoted_count"] == 0
    assert any("evidence_source_type_mismatch" in error for error in summary["rejected"][0]["errors"])

def test_memory_same_trade_multiple_artifacts_cannot_promote(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    rows = [
        {"episode_id": "e1", "_memory_source_type": "episode", "lesson": "avoid chasing thin pump", "trade_id": "same_trade", "outcome_known_at": "2026-06-21T00:00:00+00:00"},
        {"review_id": "r1", "_memory_source_type": "post_trade_review", "lesson": "avoid chasing thin pump", "trade_id": "same_trade", "outcome_known_at": "2026-06-22T00:00:00+00:00"},
    ]
    candidate = mca.rem_extract_patterns(mca.light_sleep(rows), rows)[0]

    summary = mca.deep_promote([candidate], evidence_index=mca.known_evidence_index(rows), promotion_cutoff="2026-06-23T00:00:00+00:00")

    assert summary["promoted_count"] == 0
    assert "insufficient_independent_evidence" in summary["rejected"][0]["errors"]

def test_memory_requires_evidence_index(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    row = {"review_id": "r1", "_memory_source_type": "post_trade_review", "lesson": "avoid chasing thin pump", "trade_id": "t1", "outcome_known_at": "2026-06-21T00:00:00+00:00"}
    candidate = mca.rem_extract_patterns(mca.light_sleep([row]), [row])[0]
    candidate.update({"recall_count": 3, "unique_contexts": 3, "unique_days": 2, "source_quorum": 2, "independent_evidence_count": 2, "confidence_score": 0.9})

    summary = mca.deep_promote([candidate], evidence_index=None, promotion_cutoff="2026-06-23T00:00:00+00:00")

    assert summary["promoted_count"] == 0
    assert "missing_evidence_index" in summary["rejected"][0]["errors"]

def test_memory_rejects_evidence_without_source_claim(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    rows = [
        {"review_id": "r1", "_memory_source_type": "post_trade_review", "trade_id": "t1", "outcome_known_at": "2026-06-21T00:00:00+00:00"},
        {"replay_id": "cf1", "_memory_source_type": "counterfactual", "signal_id": "s1", "outcome_known_at": "2026-06-22T00:00:00+00:00"},
    ]
    evidence = [mca.evidence_record(row) for row in rows]
    candidate = {
        "candidate_id": "candidate_no_source_claim",
        "text": "avoid chasing thin pump",
        "claim": "avoid chasing thin pump",
        "recall_count": 3,
        "unique_contexts": 3,
        "unique_days": 2,
        "source_quorum": 2,
        "independent_evidence_count": 2,
        "trade_samples": 2,
        "contradiction_count": 0,
        "confidence_score": 0.9,
        "evidence": evidence,
        "raw": rows[0],
    }

    summary = mca.deep_promote([candidate], evidence_index=mca.known_evidence_index(rows), promotion_cutoff="2026-06-23T00:00:00+00:00")

    assert summary["promoted_count"] == 0
    assert any("missing_source_claim" in error for error in summary["rejected"][0]["errors"])

def test_memory_rejects_readiness_holdout_evidence(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    rows = [
        {"review_id": "r1", "_memory_source_type": "post_trade_review", "lesson": "avoid chasing thin pump", "trade_id": "t1", "readiness_holdout": True, "outcome_known_at": "2026-06-21T00:00:00+00:00"},
        {"review_id": "r2", "_memory_source_type": "post_trade_review", "lesson": "avoid chasing thin pump", "trade_id": "t2", "readiness_holdout": True, "outcome_known_at": "2026-06-21T01:00:00+00:00"},
    ]
    candidate = mca.rem_extract_patterns(mca.light_sleep(rows), rows)[0]

    summary = mca.deep_promote([candidate], evidence_index=mca.known_evidence_index(rows), promotion_cutoff="2026-06-22T00:00:00+00:00")

    assert summary["promoted_count"] == 0
    assert any("readiness_holdout_evidence_forbidden" in error for error in summary["rejected"][0]["errors"])


def test_memory_uses_canonical_evidence_metadata_not_candidate_copy(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    rows = [
        {"review_id": "r1", "_memory_source_type": "post_trade_review", "lesson": "avoid chasing thin pump", "trade_id": "t1", "readiness_holdout": True, "trial_partition_id": "holdout", "outcome_known_at": "2026-06-24T00:00:00+00:00"},
        {"replay_id": "cf1", "_memory_source_type": "counterfactual", "conclusion": "avoid chasing thin pump", "signal_id": "s1", "trial_partition_id": "train", "outcome_known_at": "2026-06-21T00:00:00+00:00"},
    ]
    candidate = mca.rem_extract_patterns(mca.light_sleep(rows), rows)[0]
    candidate["evidence"][0]["outcome_known_at"] = "2026-06-21T00:00:00+00:00"
    candidate["evidence"][0]["readiness_holdout"] = False
    candidate["evidence"][0]["trial_partition_id"] = "train"

    summary = mca.deep_promote([candidate], evidence_index=mca.known_evidence_index(rows), promotion_cutoff="2026-06-23T00:00:00+00:00", trial_partition_id="train")

    assert summary["promoted_count"] == 0
    errors = summary["rejected"][0]["errors"]
    assert any("evidence_after_promotion_cutoff" in error for error in errors)
    assert any("readiness_holdout_evidence_forbidden" in error for error in errors)
    assert any("wrong_trial_partition" in error for error in errors)


def test_memory_uses_canonical_timestamp_for_stale_ttl(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    rows = [
        {"review_id": "r1", "_memory_source_type": "post_trade_review", "lesson": "avoid chasing thin pump", "trade_id": "t1", "outcome_known_at": "2020-01-01T00:00:00+00:00"},
        {"replay_id": "cf1", "_memory_source_type": "counterfactual", "conclusion": "avoid chasing thin pump", "signal_id": "s1", "outcome_known_at": "2026-06-21T00:00:00+00:00"},
    ]
    candidate = mca.rem_extract_patterns(mca.light_sleep(rows), rows)[0]
    candidate["evidence"][0]["outcome_known_at"] = "2026-06-21T00:00:00+00:00"

    summary = mca.deep_promote([candidate], evidence_index=mca.known_evidence_index(rows), promotion_cutoff="2026-06-23T00:00:00+00:00")

    assert summary["promoted_count"] == 0
    assert any("stale_evidence_ttl_expired" in error for error in summary["rejected"][0]["errors"])


def test_promoted_memory_updates_belief_and_dont_do_consumers(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    rows = [
        {"review_id": "r1", "_memory_source_type": "post_trade_review", "lesson": "avoid chasing thin pump", "trade_id": "t1", "symbol": "BTCUSDT", "outcome_known_at": "2026-06-21T00:00:00+00:00"},
        {"replay_id": "cf1", "_memory_source_type": "counterfactual", "conclusion": "avoid chasing thin pump", "signal_id": "s1", "symbol": "ETHUSDT", "outcome_known_at": "2026-06-22T00:00:00+00:00"},
    ]
    candidate = mca.rem_extract_patterns(mca.light_sleep(rows), rows)[0]

    summary = mca.deep_promote([candidate], evidence_index=mca.known_evidence_index(rows), promotion_cutoff="2026-06-23T00:00:00+00:00")

    assert summary["promoted_count"] == 1
    assert summary["promoted"][0]["deterministic_consumer_impact"] is True
    assert summary["promoted"][0]["learning_claim"]["claim_type"] == "learned"
    assert (tmp_path / "belief_ledger.json").exists()
    assert (tmp_path / "dont_do_memory.json").exists()
    assert (tmp_path / "memory_skill_forge_queue.jsonl").exists()
    assert (tmp_path / "memory_retrieval_dirty.jsonl").exists()


def test_duplicate_memory_in_same_batch_does_not_double_apply_consumers(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    rows = [
        {"review_id": "r1", "_memory_source_type": "post_trade_review", "lesson": "avoid chasing thin pump", "trade_id": "t1", "symbol": "BTCUSDT", "outcome_known_at": "2026-06-21T00:00:00+00:00"},
        {"replay_id": "cf1", "_memory_source_type": "counterfactual", "conclusion": "avoid chasing thin pump", "signal_id": "s1", "symbol": "ETHUSDT", "outcome_known_at": "2026-06-22T00:00:00+00:00"},
    ]
    candidate = mca.rem_extract_patterns(mca.light_sleep(rows), rows)[0]

    summary = mca.deep_promote([candidate, dict(candidate)], evidence_index=mca.known_evidence_index(rows), promotion_cutoff="2026-06-23T00:00:00+00:00")

    assert summary["promoted_count"] == 1
    assert summary["rejected_count"] == 1
    assert "duplicate_existing_memory" in summary["rejected"][0]["errors"]
    assert len(atomic_state.read_jsonl(tmp_path / "memory_promoted.jsonl")) == 1
    ledger = atomic_state.read_json(tmp_path / "belief_ledger.json")
    belief = next(iter(ledger["beliefs"].values()))
    assert len(belief["evidence_for"]) == 1
    dont_do = atomic_state.read_json(tmp_path / "dont_do_memory.json")
    assert dont_do["rules"][0]["evidence_count"] == 2


def test_untrusted_llm_reasoning_cannot_supply_promotion_quorum(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    rows = [
        {"review_id": "r1", "_memory_source_type": "post_trade_review", "lesson": "avoid chasing thin pump", "trade_id": "t1", "symbol": "BTCUSDT", "outcome_known_at": "2026-06-21T00:00:00+00:00"},
        {"reasoning_id": "llm1", "_memory_source_type": "llm_reasoning", "lesson": "avoid chasing thin pump", "taint_class": "llm_generated", "outcome_known_at": "2026-06-21T01:00:00+00:00"},
    ]
    candidate = mca.rem_extract_patterns(mca.light_sleep(rows), rows)[0]

    summary = mca.deep_promote([candidate], evidence_index=mca.known_evidence_index(rows), promotion_cutoff="2026-06-23T00:00:00+00:00")

    assert summary["promoted_count"] == 0
    errors = summary["rejected"][0]["errors"]
    assert any("source_type_cannot_promote" in error for error in errors)
    assert any("tainted_evidence_cannot_promote" in error for error in errors)

def test_test_result_eval_rows_cannot_supply_promotion_quorum(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    rows = [
        {"exam_id": "d1", "_memory_source_type": "daily_exam", "lesson": "avoid chasing thin pump", "outcome_known_at": "2026-06-21T00:00:00+00:00"},
        {"test_id": "eval1", "_memory_source_type": "test_result", "lesson": "avoid chasing thin pump", "outcome_known_at": "2026-06-22T00:00:00+00:00"},
    ]
    candidate = mca.rem_extract_patterns(mca.light_sleep(rows), rows)[0]

    summary = mca.deep_promote([candidate], evidence_index=mca.known_evidence_index(rows), promotion_cutoff="2026-06-23T00:00:00+00:00")

    assert summary["promoted_count"] == 0
    errors = summary["rejected"][0]["errors"]
    assert any("eval_source_cannot_promote" in error for error in errors)
    assert any("source_type_cannot_promote" in error for error in errors)

def test_memory_id_stable_when_evidence_order_changes(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    rows = [
        {"review_id": "r1", "_memory_source_type": "post_trade_review", "lesson": "avoid chasing thin pump", "trade_id": "t1", "symbol": "BTCUSDT", "outcome_known_at": "2026-06-21T00:00:00+00:00"},
        {"replay_id": "cf1", "_memory_source_type": "counterfactual", "conclusion": "avoid chasing thin pump", "signal_id": "s1", "symbol": "ETHUSDT", "outcome_known_at": "2026-06-22T00:00:00+00:00"},
    ]
    first = mca.rem_extract_patterns(mca.light_sleep(rows), rows)[0]
    second = mca.rem_extract_patterns(mca.light_sleep(list(reversed(rows))), list(reversed(rows)))[0]

    a = mca.deep_promote([first], evidence_index=mca.known_evidence_index(rows), promotion_cutoff="2026-06-23T00:00:00+00:00", apply_consumers=False)
    monkeypatch.setattr(mca, "PROMOTED_JSONL", tmp_path / "memory_promoted_second.jsonl")
    b = mca.deep_promote([second], evidence_index=mca.known_evidence_index(rows), promotion_cutoff="2026-06-23T00:00:00+00:00", apply_consumers=False)

    assert a["rejected"][0]["memory_id"] == b["rejected"][0]["memory_id"]
    assert "learning_claim_without_deterministic_consumer_impact" in a["rejected"][0]["errors"]


def test_memory_candidate_storage_overflow_is_quarantined(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    candidates = [
        {"candidate_id": f"c{i}", "text": f"lesson {i}", "evidence": [{"source_type": "episode"}], "raw": {"_memory_source_type": "episode"}}
        for i in range(3)
    ]

    storage = mca.append_bounded_candidates(candidates, max_staged=2)

    assert storage["staged_appended"] == 1
    assert storage["overflowed"] == 2
    assert (tmp_path / "memory_candidates_overflow.jsonl").exists()

def test_memory_candidate_overflow_quarantine_persists(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    candidate = {"candidate_id": "c1", "text": "lesson", "evidence": [{"source_type": "episode"}], "raw": {"_memory_source_type": "episode"}}
    atomic_state.append_jsonl(mca.OVERFLOW_JSONL, {"candidate_id": "c1", "reason": "memory_candidate_storage_cap"})

    storage = mca.append_bounded_candidates([candidate], max_staged=10)

    assert storage["staged_appended"] == 0
    assert storage["overflowed"] == 1
    assert mca.CANDIDATES_JSONL.exists() is False

def test_memory_overflowed_candidate_does_not_promote(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    rows = [
        {"review_id": "r1", "_memory_source_type": "post_trade_review", "lesson": "avoid chasing thin pump", "trade_id": "t1", "outcome_known_at": "2026-06-21T00:00:00+00:00"},
        {"replay_id": "cf1", "_memory_source_type": "counterfactual", "conclusion": "avoid chasing thin pump", "signal_id": "s1", "outcome_known_at": "2026-06-22T00:00:00+00:00"},
    ]
    monkeypatch.setattr(mca, "append_bounded_candidates", lambda candidates: {"staged_ids": [], "overflowed": len(candidates), "overflowed_ids": [c["candidate_id"] for c in candidates]})

    summary = mca.consolidate(rows)

    assert summary["candidate_count"] == 0
    assert summary["promoted_count"] == 0
    assert summary["candidate_storage"]["overflowed"] == 1

def test_memory_rejects_stale_evidence_ttl(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    rows = [
        {"review_id": "r1", "_memory_source_type": "post_trade_review", "lesson": "avoid chasing thin pump", "trade_id": "t1", "outcome_known_at": "2020-01-01T00:00:00+00:00"},
        {"replay_id": "cf1", "_memory_source_type": "counterfactual", "conclusion": "avoid chasing thin pump", "signal_id": "s1", "outcome_known_at": "2020-01-02T00:00:00+00:00"},
    ]
    candidate = mca.rem_extract_patterns(mca.light_sleep(rows), rows)[0]

    summary = mca.deep_promote([candidate], evidence_index=mca.known_evidence_index(rows), promotion_cutoff="2026-06-23T00:00:00+00:00")

    assert summary["promoted_count"] == 0
    assert any("stale_evidence_ttl_expired" in error for error in summary["rejected"][0]["errors"])

def test_memory_rejects_stale_evidence_when_cutoff_omitted(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    rows = [
        {"review_id": "r1", "_memory_source_type": "post_trade_review", "lesson": "avoid chasing thin pump", "trade_id": "t1", "outcome_known_at": "2020-01-01T00:00:00+00:00"},
        {"replay_id": "cf1", "_memory_source_type": "counterfactual", "conclusion": "avoid chasing thin pump", "signal_id": "s1", "outcome_known_at": "2020-01-02T00:00:00+00:00"},
    ]
    candidate = mca.rem_extract_patterns(mca.light_sleep(rows), rows)[0]

    summary = mca.deep_promote([candidate], evidence_index=mca.known_evidence_index(rows))

    assert summary["promoted_count"] == 0
    assert any("stale_evidence_ttl_expired" in error for error in summary["rejected"][0]["errors"])

def test_memory_freeze_and_budget_degraded_block_promotion(monkeypatch, tmp_path: Path):
    patch_memory_paths(monkeypatch, tmp_path)
    rows = [
        {"review_id": "r1", "_memory_source_type": "post_trade_review", "lesson": "avoid chasing thin pump", "trade_id": "t1", "outcome_known_at": "2026-06-21T00:00:00+00:00"},
        {"replay_id": "cf1", "_memory_source_type": "counterfactual", "conclusion": "avoid chasing thin pump", "signal_id": "s1", "outcome_known_at": "2026-06-22T00:00:00+00:00"},
    ]
    atomic_state.write_json_atomic(mca.MEMORY_CONTROL_PATH, {"active_trial_freeze": True, "budget": {"status": "degraded"}})
    candidate = mca.rem_extract_patterns(mca.light_sleep(rows), rows)[0]

    summary = mca.deep_promote([candidate], evidence_index=mca.known_evidence_index(rows), promotion_cutoff="2026-06-23T00:00:00+00:00")

    assert summary["promoted_count"] == 0
    assert "active_trial_freeze_blocks_memory_promotion" in summary["rejected"][0]["errors"]
    assert "memory_budget_degraded_blocks_promotion" in summary["rejected"][0]["errors"]


def test_dont_do_blocks_then_counter_evidence_weakens(tmp_path: Path):
    path = tmp_path / "dont_do.json"
    rule = ddm.add_or_update_rule("do not long alt momentum", scope="setup", severity="high", evidence_delta=2, path=path)

    blocked = ddm.evaluate_candidate({"side": "LONG", "setup": "alt momentum continuation"}, path=path)
    ddm.add_counter_evidence(rule["rule_id"], amount=3, path=path)
    weakened = ddm.evaluate_candidate({"side": "LONG", "setup": "alt momentum continuation"}, path=path)

    assert blocked["action"] == "block_paper"
    assert weakened["blocked"] is False


def test_expired_dont_do_rule_no_longer_blocks(tmp_path: Path):
    path = tmp_path / "dont_do.json"
    ddm.add_or_update_rule("do not short btc", severity="high", expires_at="2020-01-01T00:00:00+00:00", path=path)

    decision = ddm.evaluate_candidate({"side": "SHORT", "symbol": "BTCUSDT"}, path=path)

    assert decision["blocked"] is False


def test_memory_retrieval_returns_relevant_rows(monkeypatch, tmp_path: Path):
    memory_dir = tmp_path / "agent_memory"
    memory_dir.mkdir()
    monkeypatch.setattr(mr, "MEMORY_DIR", memory_dir)
    atomic_state.append_jsonl(memory_dir / "post_trade_reviews.jsonl", {"review_id": "r1", "classification": "stop_too_tight", "lesson": "exhaustion fade stop too tight on pump"})

    db = tmp_path / "memory.db"
    report = mr.rebuild_index(db)
    rows = mr.search_memory("exhaustion", db)

    assert report["indexed"] == 1
    assert rows[0]["doc_id"] == "r1"


def test_retrieval_handles_empty_db(tmp_path: Path):
    assert mr.search_memory("anything", tmp_path / "missing.db") == []


def test_self_model_records_known_gaps(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(self_model, "STATE_DIR", tmp_path)
    monkeypatch.setattr(self_model, "MEMORY_DIR", tmp_path / "agent_memory")
    monkeypatch.setattr(self_model, "SELF_MODEL_LATEST", tmp_path / "agent_memory" / "self_model_latest.json")
    (tmp_path / "agent_memory").mkdir()

    model = self_model.build_self_model()

    assert model["can_trade_live"] is False
    assert "no_post_trade_reviews_yet" in model["known_gaps"]

def test_self_model_consumes_test_result_memory_curriculum(monkeypatch, tmp_path: Path):
    memory = tmp_path / "agent_memory"
    monkeypatch.setattr(self_model, "STATE_DIR", tmp_path)
    monkeypatch.setattr(self_model, "MEMORY_DIR", memory)
    monkeypatch.setattr(self_model, "SELF_MODEL_LATEST", memory / "self_model_latest.json")
    memory.mkdir()
    atomic_state.write_json_atomic(
        memory / "test_result_memory_latest.json",
        {
            "lesson_count": 2,
            "known_gaps": ["counterfactual_coverage_low"],
            "curriculum": [{"priority": "high", "task": "raise replay coverage", "action": "run replay", "source": "counterfactual"}],
            "priority_curriculum": [{"priority": "high", "gap": "counterfactual_coverage_low", "priority_score": 9, "occurrences": 3, "task": "raise replay coverage", "action": "run replay", "source": "counterfactual"}],
        },
    )
    atomic_state.write_json_atomic(memory / "learning_exam_benchmark_latest.json", {"score": 0.8, "scenario_count": 5})

    model = self_model.build_self_model()

    assert "counterfactual_coverage_low" in model["known_gaps"]
    assert model["current_state"]["learning_benchmark_score"] == 0.8
    assert model["current_state"]["test_memory_top_gap"] == "counterfactual_coverage_low"
    assert model["experience_counters"]["test_result_lessons"] == 2
    assert model["experience_counters"]["test_memory_priority_items"] == 1
    assert any(item.get("task") == "raise replay coverage" for item in model["curriculum"])


def test_data_hygiene_detects_bad_jsonl(tmp_path: Path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"ok": true}\nnot json\n', encoding="utf-8")

    report = dha.audit_learning_state([bad], output_path=tmp_path / "hygiene.json")

    assert report["ok"] is False
    assert report["bad_file_count"] == 1
