import json
import tempfile
from pathlib import Path

import agent_data_contracts as contracts
import paper_candidate_feeder as feeder


def valid_cutoff_proof():
    return {"ok": True, "decision_cutoff": "2026-06-21T00:01:00+00:00", "errors": []}


def valid_bar(is_final=True):
    return {
        "open_time": "2026-06-21T00:00:00+00:00",
        "close_time": "2026-06-21T00:01:00+00:00",
        "open": "100",
        "high": "101",
        "low": "99",
        "close": "100.5",
        "volume": "1000",
        "is_final": is_final,
        "available_at": "2026-06-21T00:01:01+00:00",
        "known_at": "2026-06-21T00:01:01+00:00",
        "ingested_at": "2026-06-21T00:01:02+00:00",
        "finalized_at": "2026-06-21T00:01:00+00:00",
    }


def valid_candle_batch(**overrides):
    payload = {
        "schema_version": contracts.SCHEMA_VERSION,
        "chart_model_version": contracts.CHART_MODEL_VERSION,
        "contract": "ChartCandleBatch.v1",
        "symbol": "BTCUSDT",
        "timeframe": "1m",
        "closed_only": True,
        "source_ids": ["binance_usdm_klines"],
        "input_event_ids": ["event_1"],
        "decision_cutoff": "2026-06-21T00:01:05+00:00",
        "cutoff_proof": valid_cutoff_proof(),
        "degradation_state": "ok",
        "bars": [valid_bar()],
    }
    payload.update(overrides)
    return payload


def test_valid_chart_candle_batch_contract_passes():
    result = contracts.validate_chart_contract("ChartCandleBatch.v1", valid_candle_batch())

    assert result.ok is True
    assert result.errors == []


def test_chart_candle_batch_missing_cutoff_proof_rejects():
    payload = valid_candle_batch()
    payload.pop("cutoff_proof")

    result = contracts.validate_chart_contract("ChartCandleBatch.v1", payload)

    assert result.ok is False
    assert "missing_cutoff_proof" in result.errors


def test_chart_candle_batch_unknown_timeframe_rejects():
    result = contracts.validate_chart_contract("ChartCandleBatch.v1", valid_candle_batch(timeframe="2m"))

    assert result.ok is False
    assert "invalid_timeframe" in result.errors


def test_forming_candle_requires_diagnostic_only():
    payload = valid_candle_batch(bars=[valid_bar(is_final=False)])

    result = contracts.validate_chart_contract("ChartCandleBatch.v1", payload)

    assert result.ok is False
    assert "forming_candle_requires_diagnostic_only:0" in result.errors


def test_forming_candle_can_only_pass_as_diagnostic():
    payload = valid_candle_batch(bars=[valid_bar(is_final=False)], degradation_state="diagnostic_only")

    result = contracts.validate_chart_contract("ChartCandleBatch.v1", payload)

    assert result.ok is True


def test_chart_setup_score_unknown_reason_code_rejects():
    payload = {
        "schema_version": contracts.SCHEMA_VERSION,
        "chart_model_version": contracts.CHART_MODEL_VERSION,
        "contract": "ChartSetupScore.v1",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "setup_family": "breakout_retest",
        "score": 8.1,
        "confidence": 0.72,
        "reason_codes": ["trend_aligned", "made_up_signal"],
        "blockers": [],
        "evidence_ids": ["chart_report_1"],
        "source_ids": ["binance_usdm_klines"],
        "input_event_ids": ["event_1"],
        "decision_cutoff": "2026-06-21T00:01:05+00:00",
        "cutoff_proof": valid_cutoff_proof(),
        "degradation_state": "ok",
    }

    result = contracts.validate_chart_contract("ChartSetupScore.v1", payload)

    assert result.ok is False
    assert "unknown_reason_codes:made_up_signal" in result.errors


def test_chart_intelligence_generated_event_schema_accepts_provenance():
    result = contracts.validate_event_payload(
        "chart_intelligence.generated",
        "chart_intelligence",
        {
            "report_id": "chart_report_1",
            "symbol": "BTCUSDT",
            "timeframes": ["1m", "5m"],
            "decision_cutoff": "2026-06-21T00:01:05+00:00",
            "cutoff_proof": valid_cutoff_proof(),
        },
        provenance_id="prov_chart_1",
        source_id="binance_usdm_klines",
    )

    assert result.ok is True


def test_ticker_proxy_candles_are_not_chart_decision_eligible():
    # The proxy builder still exists (diagnostic only) and must self-label as
    # non-decision-eligible. Phase 1 removed it from the decision path — see
    # test_feature_row_uses_real_candles_not_ticker_proxy below.
    rows = feeder.feature_candles_from_market_row(
        {"price": 100, "high": 110, "low": 90, "change_pct": 5, "quote_volume": 3000},
        "2026-06-21T00:02:00+00:00",
    )

    assert len(rows) == 3
    assert all(row["is_synthetic_chart_proxy"] is True for row in rows)
    assert all(row["chart_decision_eligible"] is False for row in rows)
    assert all(row["chart_candle_source"] == "ticker_24h_proxy" for row in rows)


def test_feature_row_uses_real_candles_not_ticker_proxy(tmp_path, monkeypatch):
    """Phase 1: the decision feature path must consume real closed candles at the
    decision timeframe, never the fabricated ticker_24h_proxy."""
    from _candle_seed import seed_candles

    cutoff = "2026-06-21T00:02:00+00:00"
    monkeypatch.setenv("INGEST_DECISION_CANDLES", "0")
    seed_candles(monkeypatch, tmp_path, "ABCUSDT", cutoff, base_price=10.0)
    market = {"ts": cutoff, "source_ids": ["local_state"]}
    row = {"symbol": "ABCUSDT", "price": 10, "high": 11, "low": 6, "change_pct": 25, "range_pos": 0.9, "quote_volume": 100_000_000, "funding_pct": 0.01}

    fr = feeder.feature_row_for_market_row(row, market, cutoff)

    assert fr["timeframe"] == feeder.DECISION_CANDLE_TIMEFRAME  # "5m", not ticker_24h_proxy
    assert fr["feature_status"] == "ok"
    assert fr["decision_data_capability_mask"]["action"] in {"normal", "size_cap"}
    assert int(fr.get("candle_count") or 0) >= feeder.MIN_DECISION_CANDLES


def test_real_candles_usable_when_ingested_after_cutoff(tmp_path, monkeypatch):
    """Phase 1 M1 regression: the real ingestor stamps ingested_at at fetch time
    (~now), which is LATER than the decision snapshot cutoff. Those bars must
    still be usable — ingested_at is operational, not a lookahead gate.
    (The prior seed helper hid this by stamping ingested_at in the past.)"""
    from _candle_seed import seed_candles

    cutoff = "2026-06-21T00:02:00+00:00"
    monkeypatch.setenv("INGEST_DECISION_CANDLES", "0")
    seed_candles(monkeypatch, tmp_path, "ABCUSDT", cutoff, base_price=10.0, ingested_after_cutoff=True)
    market = {"ts": cutoff, "source_ids": ["local_state"]}
    row = {"symbol": "ABCUSDT", "price": 10, "high": 11, "low": 6, "change_pct": 25, "range_pos": 0.9, "quote_volume": 100_000_000, "funding_pct": 0.01}

    fr = feeder.feature_row_for_market_row(row, market, cutoff)

    assert fr["feature_status"] == "ok", "bars ingested after cutoff must remain usable"
    assert fr["decision_data_capability_mask"]["action"] in {"normal", "size_cap"}
    assert (fr.get("cutoff_proof") or {}).get("ok") is True


def test_feature_row_skips_when_no_real_candles(monkeypatch):
    """Reject-not-fake: with no real candle cache, the decision feature path must
    NOT fabricate — the candidate is dropped/quarantined."""
    monkeypatch.setenv("INGEST_DECISION_CANDLES", "0")
    import chart_candle_service as ccs
    # point cache somewhere empty
    monkeypatch.setattr(ccs, "CHART_CANDLE_DIR", Path(tempfile.mkdtemp()) / "empty")
    market = {"ts": "2026-06-21T00:02:00+00:00", "source_ids": ["local_state"]}
    row = {"symbol": "ZZZUSDT", "price": 10, "high": 11, "low": 6, "change_pct": 25, "range_pos": 0.9, "quote_volume": 100_000_000, "funding_pct": -0.30}

    cands = feeder.build_candidates(market)

    assert cands == []  # no fabrication -> no candidate


def test_chart_fixture_skeletons_are_valid_json():
    fixture_dir = Path(__file__).parent / "fixtures" / "chart_contracts_v1"
    files = sorted(fixture_dir.glob("*.json"))

    assert {path.name for path in files} == {"btcusdt_1m_candle_batch.json", "ethusdt_5m_candle_batch.json"}
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["contract"] == "ChartCandleBatch.v1"
        assert contracts.validate_chart_contract("ChartCandleBatch.v1", payload).ok is True
