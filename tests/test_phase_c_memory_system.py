from pathlib import Path

import atomic_state
import data_hygiene_auditor as dha
import dont_do_memory as ddm
import memory_consolidation_agent as mca
import memory_retrieval as mr
import self_model


def patch_memory_paths(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(mca, "CANDIDATES_JSONL", tmp_path / "memory_candidates.jsonl")
    monkeypatch.setattr(mca, "PROMOTED_JSONL", tmp_path / "memory_promoted.jsonl")
    monkeypatch.setattr(mca, "REJECTED_JSONL", tmp_path / "memory_rejected.jsonl")
    monkeypatch.setattr(mca, "LATEST_JSON", tmp_path / "memory_consolidation_latest.json")


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
        {"episode_id": "e2", "trigger": "daily_exam", "lesson": "stop too tight after valid momentum entry", "trade_id": "t2"},
    ]

    summary = mca.consolidate(rows)

    assert summary["promoted_count"] == 1
    assert summary["promoted"][0]["recall_count"] == 2


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
    atomic_state.write_json_atomic(memory / "test_result_memory_latest.json", {"lesson_count": 2, "known_gaps": ["counterfactual_coverage_low"], "curriculum": [{"priority": "high", "task": "raise replay coverage", "action": "run replay", "source": "counterfactual"}]})
    atomic_state.write_json_atomic(memory / "learning_exam_benchmark_latest.json", {"score": 0.8, "scenario_count": 5})

    model = self_model.build_self_model()

    assert "counterfactual_coverage_low" in model["known_gaps"]
    assert model["current_state"]["learning_benchmark_score"] == 0.8
    assert model["experience_counters"]["test_result_lessons"] == 2
    assert any(item.get("task") == "raise replay coverage" for item in model["curriculum"])


def test_data_hygiene_detects_bad_jsonl(tmp_path: Path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"ok": true}\nnot json\n', encoding="utf-8")

    report = dha.audit_learning_state([bad], output_path=tmp_path / "hygiene.json")

    assert report["ok"] is False
    assert report["bad_file_count"] == 1
