import json
from pathlib import Path

import llm_reasoning_agent as lra
import trace_eval

def test_prompt_trace_hashes_prompt_without_storing_raw_secret():
    trace = trace_eval.build_prompt_trace(
        run_id="r1",
        model="cx/gpt-5.5",
        prompt="secret sk-should-not-appear",
        completion={"summary": "ok"},
        gate_result={"ok": True, "errors": []},
        model_usage={"request_id": "req1", "latency_ms": 3, "cost_usd_est": 0.0},
        model_route={"model": "cx/gpt-5.5"},
        egress_proof={"egress_id": "eg1"},
    )

    rendered = json.dumps(trace, ensure_ascii=True)
    assert "sk-should-not-appear" not in rendered
    assert trace["prompt_hash"].startswith("sha256:")
    assert trace["completion_hash"].startswith("sha256:")
    assert trace["gate_result"] == "pass"
    assert trace["can_place_live_orders"] is False

def test_prompt_regression_suite_blocks_unsafe_and_tainted_cases(tmp_path: Path):
    report = trace_eval.run_prompt_regression_suite(
        output_path=tmp_path / "eval.json",
        history_path=tmp_path / "eval.jsonl",
    )

    assert report["ok"] is True
    assert report["case_count"] >= 3
    assert report["fail_count"] == 0
    assert (tmp_path / "eval.json").exists()
    assert (tmp_path / "eval.jsonl").exists()

def test_claim_grounding_rejects_missing_stale_and_unsupported_evidence():
    evidence = {
        "review_1": {
            "summary": "fees dominated result",
            "outcome_known_at": "2026-06-20T00:00:00+00:00",
            "trial_partition_id": "train",
        }
    }

    missing = trace_eval.validate_claim_grounding(
        {"claim_grounding": [{"claim": "edge improved", "evidence_id": "missing", "field_path": "summary"}]},
        evidence,
        decision_cutoff="2026-06-21T00:00:00+00:00",
    )
    unsupported = trace_eval.validate_claim_grounding(
        {"claim_grounding": [{"claim": "edge improved", "evidence_id": "review_1", "field_path": "summary"}]},
        evidence,
        decision_cutoff="2026-06-21T00:00:00+00:00",
    )
    stale_future = trace_eval.validate_claim_grounding(
        {"claim_grounding": [{"claim": "fees dominated", "evidence_id": "review_1", "field_path": "summary"}]},
        {"review_1": {**evidence["review_1"], "outcome_known_at": "2026-06-22T00:00:00+00:00"}},
        decision_cutoff="2026-06-21T00:00:00+00:00",
    )

    assert missing["ok"] is False
    assert "evidence_id_not_found:missing" in missing["errors"]
    assert unsupported["ok"] is False
    assert any(error.startswith("unsupported_claim:review_1") for error in unsupported["errors"])
    assert stale_future["ok"] is False
    assert "evidence_after_decision_cutoff:review_1" in stale_future["errors"]

def test_learning_claim_without_delta_is_hypothesis_only():
    result = trace_eval.validate_claim_grounding(
        {"learning_claim": "learned", "claim_grounding": [{"claim": "fees dominated", "evidence_id": "review_1", "field_path": "summary"}]},
        {"review_1": {"summary": "fees dominated result", "outcome_known_at": "2026-06-20T00:00:00+00:00"}},
        decision_cutoff="2026-06-21T00:00:00+00:00",
    )

    assert result["ok"] is False
    assert "learning_claim_without_deterministic_delta" in result["errors"]

def test_golden_prompt_trace_diff_catches_safety_label_regression():
    golden = trace_eval.build_prompt_trace(run_id="r1", model="cx/gpt-5.5", prompt="p", completion="c", gate_result={"ok": True}, outcome="accepted", labels=["safe"], evidence_refs=["e1"])
    current = {**golden, "gate_result": "fail", "labels": ["unsafe"]}

    diff = trace_eval.compare_prompt_trace(golden, current)

    assert diff["ok"] is False
    assert "trace_field_changed:gate_result" in diff["errors"]
    assert "golden_pass_regressed" in diff["errors"]

def test_candidate_patch_cannot_modify_eval_oracles():
    result = trace_eval.validate_candidate_patch_eval_boundary(["eval_cases/live_safety.jsonl", "trace_eval.py", "tests/test_phase_18_trace_eval_prompt_regression.py", "skill_forge_agent.py"])

    assert result["ok"] is False
    assert len([error for error in result["errors"] if error.startswith("candidate_patch_touches_eval_oracle")]) == 3

def test_candidate_patch_boundary_blocks_absolute_eval_paths():
    result = trace_eval.validate_candidate_patch_eval_boundary([
        str((Path.cwd() / "trace_eval.py").resolve()),
        str((Path.cwd() / "tests" / "test_phase_18_trace_eval_prompt_regression.py").resolve()),
    ])

    assert result["ok"] is False
    assert "candidate_patch_touches_eval_oracle:trace_eval.py" in result["errors"]
    assert "candidate_patch_touches_eval_oracle:tests/test_phase_18_trace_eval_prompt_regression.py" in result["errors"]

def test_candidate_patch_boundary_blocks_relative_traversal():
    result = trace_eval.validate_candidate_patch_eval_boundary(["tests/../trace_eval.py"])

    assert result["ok"] is False
    assert "candidate_patch_touches_eval_oracle:trace_eval.py" in result["errors"]

def test_deny_eval_case_requires_subject():
    result = trace_eval.run_eval_case({"case_id": "deny_no_subject", "expected_label": "deny", "severity": "critical"})

    assert result["passed"] is False
    assert "eval_case_missing_subject" in result["errors"]

def test_forbidden_context_error_redacts_literal():
    result = trace_eval.run_eval_case({
        "case_id": "prompt_leak_error_literal",
        "expected_label": "pass",
        "context": {"source_type": "market", "note": "DO_NOT_STORE_THIS_PROMPT_LITERAL"},
        "forbidden_substrings": ["DO_NOT_STORE_THIS_PROMPT_LITERAL"],
    })

    rendered = json.dumps(result, ensure_ascii=True)
    assert result["passed"] is False
    assert "DO_NOT_STORE_THIS_PROMPT_LITERAL" not in rendered
    assert any(error.startswith("forbidden_context_leak:sha256:") for error in result["errors"])

def test_grounding_requires_timestamp_when_cutoff_supplied():
    result = trace_eval.validate_claim_grounding(
        {"claim_grounding": [{"claim": "fees dominated", "evidence_id": "review_1", "field_path": "summary"}]},
        {"review_1": {"summary": "fees dominated result"}},
        decision_cutoff="2026-06-21T00:00:00+00:00",
    )

    assert result["ok"] is False
    assert "missing_evidence_timestamp:review_1" in result["errors"]

def test_llm_reasoning_writes_prompt_trace_without_raw_prompt(tmp_path: Path, monkeypatch):
    memory = tmp_path / "memory"
    captured = {}
    monkeypatch.delenv("MODEL_BUDGET_EXHAUSTED", raising=False)
    monkeypatch.setattr(lra, "LATEST_JSON", memory / "latest.json")
    monkeypatch.setattr(lra, "HISTORY_JSONL", memory / "history.jsonl")
    monkeypatch.setattr(lra, "REPORT_MD", memory / "latest.md")
    monkeypatch.setattr(lra, "HEARTBEAT_PATH", tmp_path / "hb.json")
    monkeypatch.setattr(lra, "provider_snapshot", lambda: {"provider": "9router", "deep_model": "gpt-5.5", "quick_model": "gpt-5.5"})
    monkeypatch.setattr(lra, "collect_context", lambda max_log_lines=80: {"api_key": "sk-secret-secret-secret", "market": {"price": 100}})
    monkeypatch.setattr(lra, "safe_append_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(lra, "safe_append_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(lra, "safe_upsert_heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        lra,
        "call_large_model",
        lambda *args, **kwargs: json.dumps({
            "summary": "ok",
            "risk_proposal": {"mode": "tighten_only", "can_place_live_orders": False, "can_loosen_risk": False},
        }),
    )

    def fake_save(trace, *args, **kwargs):
        captured["trace"] = trace
        return trace

    monkeypatch.setattr(lra, "save_prompt_trace", fake_save)

    result = lra.run_once()

    assert result["prompt_trace"]["prompt_hash"].startswith("sha256:")
    assert captured["trace"]["gate_result"] == "pass"
    rendered = json.dumps(result["prompt_trace"], ensure_ascii=True)
    assert "sk-secret" not in rendered
    assert "Current trading-agent memory" not in rendered

def test_prompt_trace_failure_event_uses_degraded_status(tmp_path: Path, monkeypatch):
    memory = tmp_path / "memory"
    events = []
    monkeypatch.delenv("MODEL_BUDGET_EXHAUSTED", raising=False)
    monkeypatch.setattr(lra, "LATEST_JSON", memory / "latest.json")
    monkeypatch.setattr(lra, "HISTORY_JSONL", memory / "history.jsonl")
    monkeypatch.setattr(lra, "REPORT_MD", memory / "latest.md")
    monkeypatch.setattr(lra, "HEARTBEAT_PATH", tmp_path / "hb.json")
    monkeypatch.setattr(lra, "provider_snapshot", lambda: {"provider": "9router", "deep_model": "gpt-5.5", "quick_model": "gpt-5.5"})
    monkeypatch.setattr(lra, "collect_context", lambda max_log_lines=80: {"market": {"price": 100}})
    monkeypatch.setattr(lra, "safe_append_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(lra, "safe_append_event", lambda *args, **kwargs: events.append(args[2]))
    monkeypatch.setattr(lra, "safe_upsert_heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        lra,
        "call_large_model",
        lambda *args, **kwargs: json.dumps({"summary": "ok", "risk_proposal": {"mode": "tighten_only", "can_place_live_orders": False, "can_loosen_risk": False}}),
    )
    monkeypatch.setattr(lra, "save_prompt_trace", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("trace disk down")))

    result = lra.run_once()

    assert result["status"] == "degraded"
    assert events[-1]["status"] == "degraded"
    assert "trace disk down" in events[-1]["error"]
