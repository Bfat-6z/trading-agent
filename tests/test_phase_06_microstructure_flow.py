import json
from pathlib import Path

import pytest

import agent_process_supervisor as aps
import autonomous_paper_trading_brain as brain
import data_source_registry as dsr
import derivatives_observer as dob
import instrument_registry as ir
import liquidation_observer as lob
import microstructure_flow_factory as mff
import orderbook_observer as obo
import paper_candidate_feeder as feeder


@pytest.fixture
def paper_brain_host_ok(monkeypatch):
    monkeypatch.setattr(brain, "paper_opens_paused_by_runtime", lambda: {"paused": False, "reason": "ok", "replay_required": False, "promotion_window_valid": True})


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def exchange_registry() -> dict:
    return {
        "schema_version": 1,
        "registry_version": "fixture-v1",
        "updated_at": "2026-06-21T00:00:00+00:00",
        "instruments": {
            "BTCUSDT": {
                "schema_version": 1,
                "symbol": "BTCUSDT",
                "status": "trading",
                "tick_size": "0.1",
                "step_size": "0.001",
                "min_notional": "5",
                "max_leverage": "50",
                "base_asset": "BTC",
                "quote_asset": "USDT",
                "contract_type": "PERPETUAL",
            }
        },
    }


def patch_phase06_paths(tmp_path: Path, monkeypatch):
    memory = tmp_path / "agent_memory"
    monkeypatch.setattr(mff, "STATE_DIR", tmp_path)
    monkeypatch.setattr(mff, "MEMORY_DIR", memory)
    monkeypatch.setattr(mff, "ORDERBOOK_SOURCE", tmp_path / "orderbook.json")
    monkeypatch.setattr(mff, "DERIVATIVES_SOURCE", tmp_path / "derivatives.json")
    monkeypatch.setattr(mff, "LIQUIDATIONS_SOURCE", tmp_path / "liquidations.json")
    monkeypatch.setattr(mff, "WHALE_FLOW_SOURCE", memory / "whale.json")
    monkeypatch.setattr(mff, "NEWS_SOURCE", memory / "news.json")
    monkeypatch.setattr(mff, "LATEST_PATH", memory / "microstructure_flow_latest.json")
    monkeypatch.setattr(mff, "HISTORY_PATH", memory / "microstructure_flow_history.jsonl")
    monkeypatch.setattr(mff, "HEARTBEAT_PATH", tmp_path / "microstructure_flow_factory_heartbeat.json")
    monkeypatch.setattr(mff, "utc_now", lambda: "2026-06-21T00:00:20+00:00")
    monkeypatch.setattr(ir, "utc_now", lambda: "2026-06-21T00:00:20+00:00")
    monkeypatch.setattr(ir, "REGISTRY_PATH", tmp_path / "instrument_registry.json")
    monkeypatch.setattr(ir, "QUALITY_PATH", memory / "quality.json")
    write_json(ir.REGISTRY_PATH, exchange_registry())
    monkeypatch.setattr(mff, "load_registry", lambda: exchange_registry())


def test_phase06_bundle_carries_instrument_price_basis_and_sources(tmp_path: Path, monkeypatch):
    patch_phase06_paths(tmp_path, monkeypatch)
    write_json(mff.ORDERBOOK_SOURCE, obo.evaluate_orderbook("BTCUSDT", [[100, 2], [99.9, 2]], [[100.1, 2], [100.2, 2]], updated_at="2026-06-21T00:00:10+00:00"))
    write_json(mff.DERIVATIVES_SOURCE, dob.evaluate_derivatives("BTCUSDT", funding_rate=0.001, oi_now=100, oi_prev=90, mark_price=100, updated_at="2026-06-21T00:00:10+00:00"))
    write_json(mff.LIQUIDATIONS_SOURCE, lob.aggregate_liquidations("BTCUSDT", [{"ts": "2026-06-21T00:00:05+00:00", "side": "LONG", "notional": 2_000_000, "price": 100}], decision_cutoff="2026-06-21T00:00:20+00:00", reference_price=100))
    write_json(mff.WHALE_FLOW_SOURCE, {"updated_at": "2026-06-21T00:00:10+00:00", "by_symbol": {"BTCUSDT": {"pressure_side": "LONG", "pressure_score": 0.4, "source_quorum_passed": True, "market_confirmed": True, "event_count": 2}}})
    write_json(mff.NEWS_SOURCE, {"ts": "2026-06-21T00:00:10+00:00", "macro_risk_score": 0.1, "headline_chaos": 0.1, "symbol_impacts": {"BTC": {"risk": 0.2, "bullish": 0.3, "confidence": 0.7}}})

    bundle = mff.build_symbol_bundle("BTCUSDT", decision_cutoff="2026-06-21T00:00:20+00:00")

    assert bundle["canonical_instrument_id"] == "binance_usdm:BTCUSDT:PERPETUAL"
    assert bundle["instrument_snapshot_id"].startswith("instrument_snapshot_")
    assert bundle["price_basis"]["fills"] == "BOOK_MID/LAST+slippage"
    assert bundle["components"]["orderbook"]["price_basis"] == "BOOK_MID"
    assert bundle["components"]["derivatives"]["payer_side"] == "LONG"
    assert bundle["components"]["liquidations"]["near_price_event_count"] == 1
    assert bundle["decision_data_capability_mask"]["action"] == "normal"


def test_orderbook_rejects_crossed_gap_checksum_bad_books(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(obo, "ORDERBOOK_LATEST", tmp_path / "orderbook.json")

    result = obo.evaluate_orderbook("BTCUSDT", bids=[[101, 1]], asks=[[100, 1]], last_update_id=9, previous_update_id=10, checksum_ok=False)

    assert result["paper_entry_allowed"] is False
    assert {"crossed_orderbook", "orderbook_update_id_gap", "orderbook_checksum_failed"}.issubset(set(result["errors"]))


def test_liquidation_cutoff_and_proximity_are_explicit(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(lob, "LIQUIDATIONS_LATEST", tmp_path / "liq.json")
    monkeypatch.setattr(lob, "LIQUIDATION_EVENTS", tmp_path / "liq.jsonl")

    result = lob.aggregate_liquidations(
        "BTCUSDT",
        [
            {"ts": "2026-06-21T00:00:05+00:00", "available_at": "2026-06-21T00:00:05+00:00", "side": "LONG", "notional": 1_000_000, "price": 100.1},
            {"ts": "2026-06-21T00:00:25+00:00", "available_at": "2026-06-21T00:00:25+00:00", "side": "SHORT", "notional": 5_000_000, "price": 110},
        ],
        decision_cutoff="2026-06-21T00:00:20+00:00",
        reference_price=100,
        proximity_bps=20,
    )

    assert result["event_count"] == 1
    assert result["excluded_after_cutoff"] == 1
    assert result["near_price_event_count"] == 1
    assert result["coverage"] == "partial"


def test_candidate_feeder_reads_microstructure_bundle_not_raw_whale_latest(tmp_path: Path, monkeypatch):
    memory = tmp_path / "agent_memory"
    monkeypatch.setattr(feeder, "MEMORY_DIR", memory)
    monkeypatch.setattr(feeder, "MICROSTRUCTURE_FLOW_LATEST", memory / "microstructure_flow_latest.json")
    monkeypatch.setattr(feeder, "MARKET_LATEST", tmp_path / "market.json")
    monkeypatch.setattr(feeder, "REGISTRY_PATH", tmp_path / "registry.json")
    monkeypatch.setattr(feeder, "REGISTRY_QUALITY_PATH", memory / "quality.json")
    monkeypatch.setattr(feeder, "CANDIDATES_PATH", memory / "candidates.json")
    monkeypatch.setattr(feeder, "LATEST_PATH", memory / "latest.json")
    monkeypatch.setattr(feeder, "HISTORY_PATH", memory / "history.jsonl")
    monkeypatch.setattr(feeder, "HEARTBEAT_PATH", tmp_path / "hb.json")
    write_json(feeder.MARKET_LATEST, {"ts": "2026-06-21T00:00:20+00:00", "source_ids": ["local_state"], "hot": [{"symbol": "BTCUSDT", "price": 100, "high": 101, "low": 80, "change_pct": 25, "range_pos": 0.9, "quote_volume": 100_000_000, "funding_pct": 0.01}]})
    write_json(feeder.MICROSTRUCTURE_FLOW_LATEST, {"updated_at": "2026-06-21T00:00:20+00:00", "symbols": {"BTCUSDT": {"feature_confidence": 0.8}}, "by_symbol": {"BTCUSDT": {"pressure_side": "SHORT", "pressure_score": -0.4, "source_quorum_passed": True, "market_confirmed": True, "event_count": 2}}})
    monkeypatch.setattr(feeder, "enqueue_job", lambda *args, **kwargs: {"ok": True, "job_id": "j"})

    result = feeder.run_once(enqueue=False)

    assert result["candidate_count"] == 1
    candidate = result["candidates"][0]
    assert candidate["feature_id"]
    assert candidate["external_flow"]["source_quorum_passed"] is True


def test_brain_size_caps_optional_microstructure_gaps(monkeypatch, tmp_path: Path, paper_brain_host_ok):
    monkeypatch.setattr(brain, "BRAIN_LATEST", tmp_path / "brain.json")
    monkeypatch.setattr(brain, "BRAIN_HISTORY", tmp_path / "brain.jsonl")
    monkeypatch.setattr(brain, "PAPER_RISK_STATE", tmp_path / "risk.json")
    monkeypatch.setattr(brain, "run_preflight", lambda *args, **kwargs: {"allowed": True, "errors": []})
    monkeypatch.setattr(brain, "evaluate_candidate", lambda candidate: {"blocked": False, "action": "allow"})
    monkeypatch.setattr(brain, "evaluate_paper_order", lambda *args, **kwargs: {"can_open_paper": True, "errors": [], "risk_decision_id": "r1", "margin": kwargs.get("requested_margin"), "notional": 1})
    monkeypatch.setattr(brain, "rank_setups", lambda rows: {"rankings": [{"setup_id": "s1", "allocation_hint": "normal", "evidence_expectancy": 0.1, "rank_score": 2.0, "risk_multiplier": 1.0}]})

    decision = brain.decide_paper_action(
        [{"producer_id": "paper_candidate_feeder", "candidate_id": "c1", "feature_id": "f1", "feature_status": "ok", "symbol": "BTCUSDT", "side": "LONG", "setup_id": "s1", "score": 10, "entry": 100, "sl": 99, "tp": 102, "decision_data_capability_mask": {"action": "size_cap"}}],
        [],
        {"equity": "100", "cash": "100"},
    )

    assert decision["action"] == "paper_open_candidate"
    assert decision["allocation"]["capability_action"] == "size_cap"
    assert decision["allocation"]["risk_fraction"] <= 0.02


def test_microstructure_flow_factory_is_supervised():
    names = {spec.name for spec in aps.specs()}

    assert "microstructure_flow_factory" in names
