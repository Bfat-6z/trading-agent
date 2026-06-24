from pathlib import Path

import llm_council
import llm_output_quality_gate as qg
import model_router


def test_model_router_uses_deep_model_for_council(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(model_router, "MODEL_HEALTH_LATEST", tmp_path / "model.json")

    route = model_router.route_model("council_synthesis", env={"NINE_ROUTER_MODEL": "cx/gpt-5.5", "NINE_ROUTER_QUICK_MODEL": "small"})

    assert route["model"] == "cx/gpt-5.5"
    assert route["can_place_live_orders"] is False


def test_llm_quality_gate_rejects_live_order_intent(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(qg, "QUALITY_LATEST", tmp_path / "quality.json")
    monkeypatch.setattr(qg, "QUALITY_HISTORY", tmp_path / "quality.jsonl")

    result = qg.sanitize_output({"role": "risk_critic", "summary": "place_order now", "data_ids": ["d1"], "risk_proposal": {"can_place_live_orders": True}}, "council_role")

    assert result["ok"] is False
    assert "unsafe_live_intent" in result["errors"]
    assert result["sanitized"]["can_place_live_orders"] is False


def test_llm_quality_gate_requires_data_ids(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(qg, "QUALITY_LATEST", tmp_path / "quality.json")
    monkeypatch.setattr(qg, "QUALITY_HISTORY", tmp_path / "quality.jsonl")

    result = qg.sanitize_output({"role": "market_analyst", "summary": "ok"}, "council_role")

    assert result["ok"] is False
    assert any(error.startswith("missing:") for error in result["errors"])


def test_council_drops_unsafe_role_and_keeps_synthesis_paper_only(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(llm_council, "COUNCIL_HISTORY", tmp_path / "council.jsonl")
    monkeypatch.setattr(llm_council, "COUNCIL_LATEST", tmp_path / "council.json")
    monkeypatch.setattr(qg, "QUALITY_LATEST", tmp_path / "quality.json")
    monkeypatch.setattr(qg, "QUALITY_HISTORY", tmp_path / "quality.jsonl")
    monkeypatch.setattr(model_router, "MODEL_HEALTH_LATEST", tmp_path / "model.json")

    good = llm_council.accept_role_output("market_analyst", {"summary": "trend ok", "data_ids": ["feature_1"], "recommendation": "paper observe"}, path=tmp_path / "council.jsonl")
    bad = llm_council.accept_role_output("risk_critic", {"summary": "create_order", "data_ids": ["feature_1"]}, path=tmp_path / "council.jsonl")
    result = llm_council.synthesize_council([good, bad], ["feature_1"], output_path=tmp_path / "council.json")

    assert good["accepted"] is True
    assert bad["accepted"] is False
    assert result["can_place_live_orders"] is False
    assert result["quality_gate"]["ok"] is True
