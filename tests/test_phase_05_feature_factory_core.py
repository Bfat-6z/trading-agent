import json
from pathlib import Path

import pytest

import autonomous_paper_trading_brain as brain
import data_source_registry as dsr
import market_data_lake as mdl
import market_feature_store as mfs
import news_signal_model as nsm
import paper_candidate_feeder as feeder
import post_trade_learning_agent as ptl
import source_provenance as sp


@pytest.fixture
def paper_brain_host_ok(monkeypatch):
    monkeypatch.setattr(brain, "paper_opens_paused_by_runtime", lambda: {"paused": False, "reason": "ok", "replay_required": False, "promotion_window_valid": True})


def candles(volume=1000):
    return [
        {"ts": "2026-06-21T00:00:00+00:00", "open": 100, "high": 101, "low": 99.5, "close": 100.5, "volume": volume},
        {"ts": "2026-06-21T00:01:00+00:00", "open": 100.5, "high": 102, "low": 100, "close": 101.5, "volume": volume},
        {"ts": "2026-06-21T00:02:00+00:00", "open": 101.5, "high": 103, "low": 101, "close": 102.5, "volume": volume},
    ]


def patch_feature_paths(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(mfs, "FEATURE_STORE_DIR", tmp_path / "feature_store")
    monkeypatch.setattr(mfs, "REGIME_LATEST", tmp_path / "regime_latest.json")


def test_feature_id_hashes_full_candles_and_derivatives(tmp_path: Path, monkeypatch):
    patch_feature_paths(tmp_path, monkeypatch)
    base = candles()
    changed_candle = [dict(row) for row in base]
    changed_candle[-1]["close"] = 102.6

    first = mfs.compute_market_features("BTCUSDT", "1m", base, derivatives={"funding_pct": 0.01, "confidence": 0.5})
    second = mfs.compute_market_features("BTCUSDT", "1m", changed_candle, derivatives={"funding_pct": 0.01, "confidence": 0.5})
    third = mfs.compute_market_features("BTCUSDT", "1m", base, derivatives={"funding_pct": -0.02, "confidence": 0.5})

    assert first["feature_id"] != second["feature_id"]
    assert first["feature_id"] != third["feature_id"]
    assert first["artifact_digest"].startswith("sha256:")


def test_missing_volume_is_distinct_from_zero(tmp_path: Path, monkeypatch):
    patch_feature_paths(tmp_path, monkeypatch)
    missing_volume = [dict(row) for row in candles()]
    missing_volume[-1].pop("volume")
    zero_volume = [dict(row) for row in candles()]
    zero_volume[-1]["volume"] = 0

    missing = mfs.compute_market_features("BTCUSDT", "1m", missing_volume)
    zero = mfs.compute_market_features("BTCUSDT", "1m", zero_volume)

    assert missing["value_status"]["candles[2].volume"] == "missing"
    assert zero["value_status"]["candles[2].volume"] == "zero"
    assert missing["missing_rate"] > zero["missing_rate"]


def test_required_stale_source_marks_feature_unusable(tmp_path: Path, monkeypatch):
    patch_feature_paths(tmp_path, monkeypatch)
    registry = tmp_path / "sources.json"
    monkeypatch.setattr(dsr, "DATA_SOURCES_LATEST", registry)
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at": "2026-06-21T00:00:00+00:00",
                "sources": {
                    "stale_klines": {
                        "source_id": "stale_klines",
                        "provider": "binance",
                        "source_type": "market_candles",
                        "status": "ok",
                        "last_success_at": "2026-06-21T00:00:00+00:00",
                        "freshness_sla_seconds": 60,
                        "trust_score": 0.9,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sp, "load_source_registry", lambda: json.loads(registry.read_text(encoding="utf-8")))

    row = mfs.compute_market_features("BTCUSDT", "1m", candles(), source_ids=["stale_klines"])

    assert row["usable_for_paper"] is False
    assert row["decision_data_capability_mask"]["action"] == "skip"
    assert "source_stale" in row["quarantine_reasons"]


def test_future_available_input_rejected_by_cutoff(tmp_path: Path, monkeypatch):
    patch_feature_paths(tmp_path, monkeypatch)
    rows = candles()
    rows[-1]["available_at"] = "2026-06-21T00:03:00+00:00"

    row = mfs.compute_market_features("BTCUSDT", "1m", rows, decision_cutoff="2026-06-21T00:02:00+00:00")

    assert row["usable_for_paper"] is False
    assert "available_at_after_cutoff:candle:2:2026-06-21T00:02:00+00:00" in row["cutoff_proof"]["errors"]


def test_candle_cache_and_replay_manifest_hash_full_inputs(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(mdl, "MARKET_CACHE_DIR", tmp_path / "market_cache")
    monkeypatch.setattr(mdl, "REPLAY_MANIFEST_DIR", tmp_path / "replay_manifests")
    changed = [dict(row) for row in candles()]
    changed[-1]["close"] = 103

    first = mdl.store_candles("BTCUSDT", "1m", candles(), source_id="local_state")
    second = mdl.store_candles("BTCUSDT", "1m", changed, source_id="local_state")
    manifest_a = mdl.create_replay_manifest("t1", first["cache_id"], ["local_state"], {"b": 2, "a": 1}, input_ids=["i1"])
    manifest_b = mdl.create_replay_manifest("t1", first["cache_id"], ["local_state"], {"a": 1, "b": 2}, input_ids=["i1"])
    manifest_c = mdl.create_replay_manifest("t1", first["cache_id"], ["local_state"], {"a": 1, "b": 2}, input_ids=["i2"])

    assert first["cache_id"] != second["cache_id"]
    assert manifest_a["manifest_id"] == manifest_b["manifest_id"]
    assert manifest_a["manifest_id"] != manifest_c["manifest_id"]
    assert manifest_a["schema_digest"].startswith("sha256:")


def test_candidate_feeder_attaches_feature_row_id(tmp_path: Path, monkeypatch):
    patch_feature_paths(tmp_path, monkeypatch)
    # Freeze "now" just after the fixed snapshot so the local_state source is not
    # flagged source_stale (SLA 3600s). Without this the test is time-dependent
    # and starts failing once the hardcoded 2026-06-21 ts ages past the SLA.
    monkeypatch.setattr(dsr, "utc_now", lambda: "2026-06-21T00:05:00+00:00")
    market = {
        "ts": "2026-06-21T00:02:00+00:00",
        "source_ids": ["local_state"],
        "hot": [{"symbol": "ABCUSDT", "price": 10, "high": 11, "low": 6, "change_pct": 25, "range_pos": 0.9, "quote_volume": 100_000_000, "funding_pct": 0.01}],
    }

    candidate = feeder.build_candidates(market)[0]

    assert candidate["feature_id"]
    assert candidate["feature_manifest_id"]
    assert candidate["decision_data_capability_mask"]["action"] in {"normal", "size_cap"}
    assert candidate["decision_regime_state"]["labeler_version"] == "decision_regime_v1"


def test_runtime_paper_candidate_without_feature_row_is_skipped(tmp_path: Path, monkeypatch, paper_brain_host_ok):
    monkeypatch.setattr(brain, "BRAIN_LATEST", tmp_path / "brain.json")
    monkeypatch.setattr(brain, "BRAIN_HISTORY", tmp_path / "brain.jsonl")
    monkeypatch.setattr(brain, "PAPER_RISK_STATE", tmp_path / "risk.json")

    decision = brain.decide_paper_action(
        [{"producer_id": "paper_candidate_feeder", "candidate_id": "c1", "symbol": "BTCUSDT", "side": "LONG", "setup_id": "s1", "score": 9, "entry": 100, "sl": 99, "tp": 102}],
        [{"setup_id": "s1", "trades": 60, "expectancy": 0.05, "profit_factor": 1.5, "win_rate": 0.55}],
        {"equity": "100", "cash": "100"},
    )

    assert decision["action"] == "skip"
    assert "missing_feature_row_id" in decision["risk_decision"]["errors"]


def test_late_ingested_news_cannot_affect_pre_cutoff_score():
    result = nsm.score_events(
        [
            {
                "title": "BTC ETF approval inflow",
                "source": "rss",
                "published_at": "2026-06-21T00:00:00+00:00",
                "ts_seen": "2026-06-21T00:10:00+00:00",
                "ingested_at": "2026-06-21T00:10:00+00:00",
            }
        ],
        decision_cutoff="2026-06-21T00:05:00+00:00",
    )

    assert result["event_count"] == 0
    assert result["cutoff_filtered_event_count"] == 1
    assert result["catalyst_score"] == 0


def test_post_trade_outcome_regime_is_separate_from_decision_state():
    decision_regime = {"labeler_version": "decision_regime_v1", "label": "uptrend:normal_vol:normal_participation"}
    review = ptl.review_closed_trade(
        {
            "trade_id": "r1",
            "side": "LONG",
            "entry": "100",
            "exit": "99",
            "sl": "99",
            "tp": "102",
            "net": "-1",
            "decision_regime_state": decision_regime,
            "market_regime": "risk_off",
            "setup_expected_regime": "risk_on",
            "close_ts": "2026-06-21T00:02:00+00:00",
        },
        candles(),
        setup_score={"score": 0.7},
        append=False,
    )

    assert review["decision_regime_state"] == decision_regime
    assert review["post_trade_regime_outcome"]["labeler_version"] == "post_trade_regime_outcome_v1"
    assert review["post_trade_regime_outcome"]["post_trade_outcome"] is True
