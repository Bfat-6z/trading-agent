from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import skill_forge_agent as sfa
import setup_ranker
import setup_skill_library as skl
import autonomous_paper_trading_brain as brain


APPROVAL_SECRET = "phase15-test-secret"

@pytest.fixture
def paper_brain_host_ok(monkeypatch):
    monkeypatch.setattr(brain, "paper_opens_paused_by_runtime", lambda: {"paused": False, "reason": "ok", "replay_required": False, "promotion_window_valid": True})


def approval_manifest(patch: dict, before_hash: str, after_hash: str) -> dict:
    ids = patch.get("evidence_ids") or sfa.evidence_ids(patch.get("evidence") or {})
    approved_at = datetime.fromisoformat(sfa.utc_now()) - timedelta(minutes=1)
    expires_at = approved_at + timedelta(hours=24)
    manifest = {
        "signer": "human:test",
        "reason": "paper metadata risk cap canary",
        "scope": "metadata_only",
        "ttl": "24h",
        "nonce": "nonce-1",
        "approved_at": approved_at.isoformat(timespec="seconds"),
        "rollback_owner": "operator",
        "signer_role": "risk_approver",
        "signature": "",
        "expires_at": expires_at.isoformat(timespec="seconds"),
        "evidence_ids": ids,
        "before_hash": before_hash,
        "after_hash": after_hash,
    }
    manifest["signature"] = sfa.expected_approval_signature(manifest, APPROVAL_SECRET)
    return manifest


def attach_replay_proof(patch: dict, before_hash: str, after_hash: str) -> None:
    proof = {
        "source": "deterministic_replay",
        "verifier": "counterfactual_replay_agent",
        "replay_artifact_id": "replay:phase15",
        "changed_decisions": ["c1"],
    }
    proof["proof_hash"] = sfa.expected_replay_proof_hash(patch, before_hash, after_hash, proof)
    proof["signature"] = sfa.expected_replay_proof_signature(patch, before_hash, after_hash, proof, APPROVAL_SECRET)
    patch["decision_diff_proof"] = proof


def test_skill_patch_scope_rejects_code_or_live_paths(tmp_path: Path):
    review = sfa.propose_skill_patch(
        {
            "setup_id": "x",
            "patch_type": "sl_tp_template",
            "invalidation": "bad stop",
            "rollback_criteria": "future paper fails",
            "target_path": "live_permission_firewall.py",
            "code_diff": "print('mutate code')",
        },
        {"expectancy": 0.1, "sample_size": 30, "evidence_ids": ["r1"]},
        pending_path=tmp_path / "pending.jsonl",
        review_path=tmp_path / "reviews.jsonl",
        latest_path=tmp_path / "latest.json",
    )

    assert review["ok"] is False
    assert any(error.startswith("denied_patch_key") for error in review["errors"])
    assert any(error.startswith("denied_patch_value") for error in review["errors"])


def test_scope_scanner_rejects_python_references_under_innocent_keys():
    errors = sfa.scan_patch_scope(
        {
            "note": "please edit strategy.py#L12",
            "reason": "see helper.py?raw=1",
            "encoded": "strategy%2epy",
            "base64": "c3RyYXRlZ3kucHk=",
        }
    )

    assert any(error.endswith(":.py") for error in errors)
    assert len([error for error in errors if error.endswith(":.py")]) >= 4


def test_leverage_cap_patch_requires_signed_manifest_to_apply(monkeypatch, tmp_path: Path):
    pending = tmp_path / "pending.jsonl"
    applied = tmp_path / "applied.jsonl"
    output = tmp_path / "integration.json"
    latest = tmp_path / "latest.json"
    library = {"skills": {"x": {"setup_id": "x", "metadata": {}}}, "history": []}
    monkeypatch.setattr(sfa, "load_library", lambda: library)
    monkeypatch.setattr(sfa, "save_library", lambda payload: payload)
    sfa.append_jsonl_once(
        pending,
        {
            "patch_id": "p_cap",
            "setup_id": "x",
            "patch_type": "leverage_cap_by_setup",
            "max_leverage": 12,
            "invalidation": "liq risk too close",
            "rollback_criteria": "future paper expectancy recovers",
            "status": "paper_shadow_only",
            "evidence_ids": ["r1"],
            "evidence": {"sample_size": 30, "evidence_ids": ["r1"], "expectancy": 0.1},
        },
        "patch_id",
    )

    result = sfa.apply_paper_shadow_patches(pending_path=pending, output_path=output, applied_path=applied, latest_path=latest, lock_path=tmp_path / "lock")

    assert result["applied_count"] == 0
    assert "missing_approval_manifest" in result["skipped"][0]["errors"]


def test_signed_leverage_cap_patch_applies_with_hash_manifest(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(sfa.APPROVAL_HMAC_SECRET_ENV, APPROVAL_SECRET)
    pending = tmp_path / "pending.jsonl"
    applied = tmp_path / "applied.jsonl"
    output = tmp_path / "integration.json"
    latest = tmp_path / "latest.json"
    registry = tmp_path / "registry.json"
    ledger = tmp_path / "ledger.jsonl"
    library = {"skills": {"x": {"setup_id": "x", "metadata": {}}}, "history": []}
    saved = {}
    patch = {
        "patch_id": "p_cap",
        "setup_id": "x",
        "patch_type": "leverage_cap_by_setup",
        "max_leverage": 12,
        "invalidation": "liq risk too close",
        "rollback_criteria": "future paper expectancy recovers",
        "status": "paper_shadow_only",
        "evidence_ids": ["r1"],
        "evidence": {"sample_size": 30, "evidence_ids": ["r1"], "expectancy": 0.1},
    }
    preview = sfa.preview_patch_application(library["skills"]["x"], patch)
    attach_replay_proof(patch, preview["before_skill_hash"], preview["after_skill_hash"])
    patch["approval_manifest"] = approval_manifest(patch, preview["before_skill_hash"], preview["after_skill_hash"])
    sfa.append_jsonl_once(pending, patch, "patch_id")
    monkeypatch.setattr(sfa, "load_library", lambda: library)
    monkeypatch.setattr(sfa, "save_library", lambda payload: saved.setdefault("library", payload) or payload)

    result = sfa.apply_paper_shadow_patches(pending_path=pending, output_path=output, applied_path=applied, latest_path=latest, registry_path=registry, ledger_path=ledger, lock_path=tmp_path / "lock")

    assert result["applied_count"] == 1
    assert saved["library"]["skills"]["x"]["metadata"]["paper_only_leverage_cap"] == 12
    row = sfa.read_jsonl(applied)[0]
    assert row["apply_manifest"]["approval_manifest"]["signer"] == "human:test"
    assert row["learning_claim"]["claim_type"] == "learned"
    assert registry.exists()
    assert sfa.read_jsonl(ledger)[0]["event"] == "patch_applied"


def test_manifest_hashes_are_stable_across_time(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(sfa.APPROVAL_HMAC_SECRET_ENV, APPROVAL_SECRET)
    pending = tmp_path / "pending.jsonl"
    library = {"skills": {"x": {"setup_id": "x", "metadata": {}}}, "history": []}
    saved = {}
    patch = {
        "patch_id": "p_cap",
        "setup_id": "x",
        "patch_type": "leverage_cap_by_setup",
        "max_leverage": 12,
        "invalidation": "liq risk too close",
        "rollback_criteria": "future paper expectancy recovers",
        "status": "paper_shadow_only",
        "evidence_ids": ["r1"],
        "evidence": {"sample_size": 30, "evidence_ids": ["r1"], "expectancy": 0.1},
    }
    preview = sfa.preview_patch_application(library["skills"]["x"], patch)
    patch["approval_manifest"] = approval_manifest(patch, preview["before_skill_hash"], preview["after_skill_hash"])
    sfa.append_jsonl_once(pending, patch, "patch_id")
    monkeypatch.setattr(sfa, "load_library", lambda: library)
    monkeypatch.setattr(sfa, "save_library", lambda payload: saved.setdefault("library", payload) or payload)
    monkeypatch.setattr(sfa, "utc_now", lambda: "2026-06-29T00:00:05+00:00")

    result = sfa.apply_paper_shadow_patches(pending_path=pending, output_path=tmp_path / "out.json", applied_path=tmp_path / "applied.jsonl", latest_path=tmp_path / "latest.json", lock_path=tmp_path / "lock")

    assert result["applied_count"] == 1


def test_patch_apply_rejects_base_skill_hash_mismatch(monkeypatch, tmp_path: Path):
    pending = tmp_path / "pending.jsonl"
    library = {"skills": {"x": {"setup_id": "x", "metadata": {}}}, "history": []}
    monkeypatch.setattr(sfa, "load_library", lambda: library)
    monkeypatch.setattr(sfa, "save_library", lambda payload: payload)
    sfa.append_jsonl_once(
        pending,
        {
            "patch_id": "p1",
            "setup_id": "x",
            "patch_type": "setup_retirement",
            "invalidation": "losing",
            "rollback_criteria": "recovers",
            "status": "paper_shadow_only",
            "base_skill_hash": "sha256:wrong",
            "evidence_ids": ["r1"],
            "evidence": {"sample_size": 30, "evidence_ids": ["r1"], "expectancy": 0.1},
        },
        "patch_id",
    )

    result = sfa.apply_paper_shadow_patches(pending_path=pending, output_path=tmp_path / "out.json", applied_path=tmp_path / "applied.jsonl", latest_path=tmp_path / "latest.json", lock_path=tmp_path / "lock")

    assert result["applied_count"] == 0
    assert "skill_base_hash_mismatch" in result["skipped"][0]["errors"]


def test_approval_manifest_rejects_reused_nonce(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(sfa.APPROVAL_HMAC_SECRET_ENV, APPROVAL_SECRET)
    pending = tmp_path / "pending.jsonl"
    applied = tmp_path / "applied.jsonl"
    library = {"skills": {"x": {"setup_id": "x", "metadata": {}}}, "history": []}
    patch = {
        "patch_id": "p_cap_2",
        "setup_id": "x",
        "patch_type": "leverage_cap_by_setup",
        "max_leverage": 12,
        "invalidation": "liq risk too close",
        "rollback_criteria": "future paper expectancy recovers",
        "status": "paper_shadow_only",
        "evidence_ids": ["r1"],
        "evidence": {"sample_size": 30, "evidence_ids": ["r1"], "expectancy": 0.1},
    }
    preview = sfa.preview_patch_application(library["skills"]["x"], patch)
    patch["approval_manifest"] = approval_manifest(patch, preview["before_skill_hash"], preview["after_skill_hash"])
    sfa.append_jsonl(applied, {"patch_id": "old", "apply_manifest": {"approval_manifest": {"nonce": "nonce-1"}}})
    sfa.append_jsonl_once(pending, patch, "patch_id")
    monkeypatch.setattr(sfa, "load_library", lambda: library)
    monkeypatch.setattr(sfa, "save_library", lambda payload: payload)

    result = sfa.apply_paper_shadow_patches(pending_path=pending, output_path=tmp_path / "out.json", applied_path=applied, latest_path=tmp_path / "latest.json", lock_path=tmp_path / "lock")

    assert result["applied_count"] == 0
    assert "approval_nonce_reused" in result["skipped"][0]["errors"]


def test_approval_manifest_rejects_bad_signature_and_expired(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(sfa.APPROVAL_HMAC_SECRET_ENV, APPROVAL_SECRET)
    monkeypatch.setattr(sfa, "utc_now", lambda: "2026-06-29T00:00:05+00:00")
    manifest = approval_manifest({"evidence_ids": ["r1"]}, "before", "after")
    manifest["signature"] = "sig:any"
    manifest["expires_at"] = "2026-06-29T00:00:04+00:00"

    errors = sfa.manifest_errors(manifest, expected_before_hash="before", expected_after_hash="after", expected_evidence_ids=["r1"], required=True)

    assert "approval_signature_invalid" in errors
    assert "approval_expired" in errors


def test_rollback_removes_patch_metadata_and_blocks_old_head(monkeypatch, tmp_path: Path):
    library = {
        "skills": {
            "x": {
                "setup_id": "x",
                "metadata": {
                    "paper_only_retired": True,
                    "paper_shadow_patches": [
                        {"patch_id": "old", "patch_type": "setup_retirement"},
                        {"patch_id": "new", "patch_type": "symbol_graylist"},
                    ],
                },
            }
        },
        "history": [],
    }
    saved = {}
    applied = tmp_path / "applied.jsonl"
    sfa.append_jsonl(applied, {"patch_id": "old", "setup_id": "x", "applied_at": "2026-06-29T00:00:00+00:00"})
    sfa.append_jsonl(applied, {"patch_id": "new", "setup_id": "x", "applied_at": "2026-06-29T00:01:00+00:00"})
    monkeypatch.setattr(sfa, "load_library", lambda: library)
    monkeypatch.setattr(sfa, "save_library", lambda payload: saved.setdefault("library", payload) or payload)

    blocked = sfa.rollback_paper_shadow_patch("old", applied_path=applied, reverted_path=tmp_path / "reverted.jsonl", output_path=tmp_path / "rollback.json", lock_path=tmp_path / "lock")
    rolled = sfa.rollback_paper_shadow_patch("old", applied_path=applied, reverted_path=tmp_path / "reverted.jsonl", output_path=tmp_path / "rollback2.json", lock_path=tmp_path / "lock", inverse_patch=True)

    assert blocked["rolled_back"] is False
    assert "rollback_head_mismatch" in blocked["errors"]
    assert rolled["rolled_back"] is True
    assert saved["library"]["skills"]["x"]["metadata"]["paper_shadow_patches"][0]["patch_id"] == "new"
    assert "paper_only_retired" not in saved["library"]["skills"]["x"]["metadata"]


def test_rollback_recomputes_symbol_graylist(monkeypatch, tmp_path: Path):
    library = {
        "skills": {
            "x": {
                "setup_id": "x",
                "metadata": {
                    "paper_only_symbol_graylist": ["BTCUSDT", "ETHUSDT"],
                    "paper_shadow_patches": [
                        {"patch_id": "old", "patch_type": "symbol_graylist", "symbols": ["BTCUSDT"]},
                        {"patch_id": "new", "patch_type": "symbol_graylist", "symbols": ["ETHUSDT"]},
                    ],
                },
            }
        },
        "history": [],
    }
    saved = {}
    applied = tmp_path / "applied.jsonl"
    sfa.append_jsonl(applied, {"patch_id": "old", "setup_id": "x", "applied_at": "same"})
    sfa.append_jsonl(applied, {"patch_id": "new", "setup_id": "x", "applied_at": "same"})
    monkeypatch.setattr(sfa, "load_library", lambda: library)
    monkeypatch.setattr(sfa, "save_library", lambda payload: saved.setdefault("library", payload) or payload)

    blocked = sfa.rollback_paper_shadow_patch("old", applied_path=applied, reverted_path=tmp_path / "reverted.jsonl", output_path=tmp_path / "rollback.json", lock_path=tmp_path / "lock")
    rolled = sfa.rollback_paper_shadow_patch("old", applied_path=applied, reverted_path=tmp_path / "reverted.jsonl", output_path=tmp_path / "rollback2.json", lock_path=tmp_path / "lock", inverse_patch=True)

    assert "rollback_head_mismatch" in blocked["errors"]
    assert rolled["rolled_back"] is True
    assert saved["library"]["skills"]["x"]["metadata"]["paper_only_symbol_graylist"] == ["ETHUSDT"]


def test_rollback_recomputes_latest_scalar_patch_metadata(monkeypatch, tmp_path: Path):
    library = {
        "skills": {
            "x": {
                "setup_id": "x",
                "metadata": {
                    "paper_only_leverage_cap": 20,
                    "paper_shadow_patches": [
                        {"patch_id": "old", "patch_type": "leverage_cap_by_setup", "max_leverage": 12},
                        {"patch_id": "new", "patch_type": "leverage_cap_by_setup", "max_leverage": 20},
                    ],
                },
            }
        },
        "history": [],
    }
    saved = {}
    applied = tmp_path / "applied.jsonl"
    sfa.append_jsonl(applied, {"patch_id": "old", "setup_id": "x", "applied_at": "same"})
    sfa.append_jsonl(applied, {"patch_id": "new", "setup_id": "x", "applied_at": "same"})
    monkeypatch.setattr(sfa, "load_library", lambda: library)
    monkeypatch.setattr(sfa, "save_library", lambda payload: saved.setdefault("library", payload) or payload)

    rolled = sfa.rollback_paper_shadow_patch("new", applied_path=applied, reverted_path=tmp_path / "reverted.jsonl", output_path=tmp_path / "rollback.json", lock_path=tmp_path / "lock")

    assert rolled["rolled_back"] is True
    assert saved["library"]["skills"]["x"]["metadata"]["paper_only_leverage_cap"] == 12


def test_forged_decision_diff_does_not_claim_learning(monkeypatch, tmp_path: Path):
    pending = tmp_path / "pending.jsonl"
    applied = tmp_path / "applied.jsonl"
    library = {"skills": {"x": {"setup_id": "x", "metadata": {}}}, "history": []}
    monkeypatch.setattr(sfa, "load_library", lambda: library)
    monkeypatch.setattr(sfa, "save_library", lambda payload: payload)
    sfa.append_jsonl_once(
        pending,
        {
            "patch_id": "p1",
            "setup_id": "x",
            "patch_type": "setup_retirement",
            "invalidation": "losing",
            "rollback_criteria": "recovers",
            "status": "paper_shadow_only",
            "evidence_ids": ["r1"],
            "evidence": {"sample_size": 30, "evidence_ids": ["r1"], "expectancy": 0.1},
            "before_after_decision_diff": {"changed_decisions": ["fake"]},
        },
        "patch_id",
    )

    result = sfa.apply_paper_shadow_patches(pending_path=pending, output_path=tmp_path / "out.json", applied_path=applied, latest_path=tmp_path / "latest.json", lock_path=tmp_path / "lock")

    assert result["applied_count"] == 1
    assert sfa.read_jsonl(applied)[0]["learning_claim"]["claim_type"] == "hypothesis_only"


def test_forged_deterministic_replay_proof_does_not_claim_learning(monkeypatch, tmp_path: Path):
    pending = tmp_path / "pending.jsonl"
    applied = tmp_path / "applied.jsonl"
    library = {"skills": {"x": {"setup_id": "x", "metadata": {}}}, "history": []}
    monkeypatch.setenv(sfa.APPROVAL_HMAC_SECRET_ENV, APPROVAL_SECRET)
    monkeypatch.setattr(sfa, "load_library", lambda: library)
    monkeypatch.setattr(sfa, "save_library", lambda payload: payload)
    sfa.append_jsonl_once(
        pending,
        {
            "patch_id": "p1",
            "setup_id": "x",
            "patch_type": "setup_retirement",
            "invalidation": "losing",
            "rollback_criteria": "recovers",
            "status": "paper_shadow_only",
            "evidence_ids": ["r1"],
            "evidence": {"sample_size": 30, "evidence_ids": ["r1"], "expectancy": 0.1},
            "decision_diff_proof": {
                "source": "deterministic_replay",
                "verifier": "counterfactual_replay_agent",
                "changed_decisions": ["fake"],
                "signature": "replay_sig:v1:fake",
            },
        },
        "patch_id",
    )

    result = sfa.apply_paper_shadow_patches(pending_path=pending, output_path=tmp_path / "out.json", applied_path=applied, latest_path=tmp_path / "latest.json", lock_path=tmp_path / "lock")

    assert result["applied_count"] == 1
    assert sfa.read_jsonl(applied)[0]["learning_claim"]["claim_type"] == "hypothesis_only"


def test_setup_contract_hash_flows_into_ranker_and_paper_decision(monkeypatch, tmp_path: Path, paper_brain_host_ok):
    monkeypatch.setattr(brain, "BRAIN_LATEST", tmp_path / "brain.json")
    monkeypatch.setattr(brain, "BRAIN_HISTORY", tmp_path / "brain.jsonl")
    monkeypatch.setattr(brain, "PAPER_RISK_STATE", tmp_path / "risk.json")
    monkeypatch.setattr(brain, "run_preflight", lambda *args, **kwargs: {"allowed": True, "errors": []})
    monkeypatch.setattr(brain, "evaluate_candidate", lambda candidate: {"blocked": False, "action": "allow"})
    monkeypatch.setattr(brain, "evaluate_paper_order", lambda *args, **kwargs: {"can_open_paper": True, "errors": [], "risk_decision_id": "r1"})
    library = skl.default_library()
    row = setup_ranker.build_setup_evidence_rows(library)[0]
    row.update({"trades": 60, "expectancy": 0.05, "profit_factor": 1.5, "win_rate": 0.55})
    rankings = setup_ranker.rank_setups([row], output_path=tmp_path / "rank.json")["rankings"]
    monkeypatch.setattr(brain, "rank_setups", lambda rows: {"rankings": rankings})

    decision = brain.decide_paper_action(
        [{"symbol": "BTCUSDT", "side": "LONG", "setup_id": row["setup_id"], "score": 9, "entry": 100, "sl": 99, "tp": 102}],
        [],
        {"equity": "100", "cash": "100"},
    )

    assert decision["action"] == "paper_open_candidate"
    assert decision["candidate"]["setup_contract_hash"] == row["setup_contract_hash"]
    assert decision["candidate"]["setup_quality_tier"] == "unrated"

def test_paper_only_leverage_cap_is_enforced_at_decision_time(monkeypatch, tmp_path: Path, paper_brain_host_ok):
    monkeypatch.setattr(brain, "BRAIN_LATEST", tmp_path / "brain.json")
    monkeypatch.setattr(brain, "BRAIN_HISTORY", tmp_path / "brain.jsonl")
    monkeypatch.setattr(brain, "PAPER_RISK_STATE", tmp_path / "risk.json")
    monkeypatch.setattr(brain, "run_preflight", lambda *args, **kwargs: {"allowed": True, "errors": []})
    monkeypatch.setattr(brain, "evaluate_candidate", lambda candidate: {"blocked": False, "action": "allow"})
    monkeypatch.setattr(brain, "active_recall_for_decision", lambda candidate, decision_cutoff=None: brain.empty_active_recall("test"))
    monkeypatch.setattr(
        brain,
        "rank_setups",
        lambda rows: {
            "rankings": [
                {
                    "setup_id": "cap_setup",
                    "rank_score": 2.0,
                    "allocation_hint": "normal",
                    "evidence_expectancy": 0.08,
                    "expectancy": 0.08,
                    "sample_confidence": 1.0,
                    "paper_only_leverage_cap": 12,
                    "setup_contract_hash": "sha256:contract",
                    "setup_quality_tier": "paper_only",
                }
            ]
        },
    )
    captured = {}

    def fake_evaluate_paper_order(*args, **kwargs):
        captured["requested_leverage"] = kwargs["requested_leverage"]
        return {"can_open_paper": True, "errors": [], "risk_decision_id": "r1", "leverage": kwargs["requested_leverage"]}

    monkeypatch.setattr(brain, "evaluate_paper_order", fake_evaluate_paper_order)

    decision = brain.decide_paper_action(
        [{"symbol": "BTCUSDT", "side": "LONG", "setup_id": "cap_setup", "score": 10, "entry": 100, "sl": 99, "tp": 104}],
        [],
        {"equity": "100", "cash": "100", "open_margin": "0"},
    )

    assert decision["action"] == "paper_open_candidate"
    assert decision["candidate"]["paper_only_leverage_cap"] == 12
    assert captured["requested_leverage"] <= 12
    assert decision["risk_decision"]["paper_sizing"]["leverage_factors"]["paper_only_leverage_cap"] == 12


def test_paper_only_leverage_cap_below_adaptive_floor_still_wins():
    leverage, factors = brain.adaptive_paper_leverage(
        {"symbol": "BTCUSDT", "side": "LONG", "score": 10, "entry": 100, "sl": 99, "paper_only_leverage_cap": 1},
        {"tier": "normal_paper", "risk_fraction": 0.05},
        {"equity": "100", "cash": "100", "open_margin": "0"},
    )

    assert leverage <= 1
    assert factors["tier_cap"] == 1
