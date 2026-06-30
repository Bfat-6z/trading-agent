import chart_setup_scorer as css
import paper_candidate_feeder as feeder


def cutoff_proof():
    return {"ok": True, "decision_cutoff": "2026-06-21T00:10:00+00:00", "errors": []}


def trend(bias="bullish", blockers=None):
    return {
        "contract": "ChartTrendRegime.v1",
        "trend_regime_id": "trend_1",
        "symbol": "BTCUSDT",
        "timeframe": "1m",
        "bias": bias,
        "confidence": 0.9,
        "reason_codes": ["ema_ribbon_bull", "trend_aligned"] if bias == "bullish" else ["ema_ribbon_bear", "trend_aligned"],
        "blockers": blockers or [],
        "overextended": "too_far_from_ema" in (blockers or []),
        "source_ids": ["chart"],
        "input_event_ids": ["e1"],
        "decision_cutoff": "2026-06-21T00:10:00+00:00",
    }


def aggregate(bias="bullish", blockers=None):
    return {"aggregate_id": "agg_1", "bias": bias, "blockers": blockers or [], "source_ids": ["chart"], "input_event_ids": ["e2"]}


def structure(side_bias="bullish", invalidation=100):
    return {
        "contract": "ChartStructureBundle.v1",
        "structure_id": "structure_1",
        "symbol": "BTCUSDT",
        "timeframe": "1m",
        "source_ids": ["chart"],
        "input_event_ids": ["e3"],
        "decision_cutoff": "2026-06-21T00:10:00+00:00",
        "cutoff_proof": cutoff_proof(),
        "degradation_state": "ok",
        "structures": {"side_bias": side_bias, "reason_codes": ["bos_up"] if side_bias == "bullish" else ["bos_down"], "invalidation_level": invalidation, "confidence": 0.8},
    }


def zones(inside=False):
    return {
        "contract": "ChartStructureBundle.v1",
        "structure_id": "zones_1",
        "symbol": "BTCUSDT",
        "timeframe": "1m",
        "source_ids": ["chart"],
        "input_event_ids": ["e4"],
        "decision_cutoff": "2026-06-21T00:10:00+00:00",
        "cutoff_proof": cutoff_proof(),
        "degradation_state": "ok",
        "structures": {
            "nearest": {"support": {"zone_id": "s1"}, "resistance": None},
            "current_price_relation": {"inside_zone_ids": ["z1"] if inside else [], "blockers": ["inside_messy_zone"] if inside else []},
        },
    }


def liquidity(volume=True):
    return {
        "contract": "ChartLiquidityBundle.v1",
        "liquidity_id": "liq_1",
        "symbol": "BTCUSDT",
        "timeframe": "1m",
        "source_ids": ["chart"],
        "input_event_ids": ["e5"],
        "decision_cutoff": "2026-06-21T00:10:00+00:00",
        "cutoff_proof": cutoff_proof(),
        "degradation_state": "ok",
        "liquidity": {
            "events": [{"event_type": "BREAKOUT_UP", "side": "bullish"}],
            "reason_codes": ["volume_confirmed"] if volume else ["volume_missing"],
            "blockers": [],
            "volume": {"status": "ok" if volume else "missing_volume", "confirmed": volume},
            "vwap": {"status": "ok", "relation": "above_vwap"},
            "confidence": 0.75,
        },
    }


def score(**overrides):
    payload = {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "setup_family": "trend_continuation",
        "trend_bundle": trend(),
        "trend_aggregate": aggregate(),
        "zone_bundle": zones(),
        "structure_bundle": structure(),
        "liquidity_bundle": liquidity(),
        "chart_intelligence_id": "chart_report_1",
    }
    payload.update(overrides)
    return css.score_chart_setup(**payload)


def test_perfect_fixture_gets_high_score_and_reason_codes():
    result = score()

    assert result["score"] >= 9
    assert result["tier"] == "5A+"
    assert "trend_aligned" in result["reason_codes"]
    assert "bos_up" in result["reason_codes"]
    assert "volume_confirmed" in result["reason_codes"]
    assert result["can_place_live_orders"] is False


def test_stale_data_cannot_pass():
    stale_zone = zones()
    stale_zone["degradation_state"] = "stale"

    result = score(zone_bundle=stale_zone)

    assert result["tier"] == "blocked"
    assert "stale_candles" in result["blockers"]
    assert result["capability_mask"]["action"] == "skip"


def test_conflicting_higher_timeframe_caps_score():
    result = score(trend_aggregate=aggregate(bias="bearish", blockers=["mixed_timeframes"]))

    assert result["tier"] == "blocked"
    assert "mixed_timeframes" in result["blockers"]
    assert "conflicting_higher_timeframe" in result["blockers"]


def test_missing_invalidation_level_blocks_paper_open():
    result = score(structure_bundle=structure(invalidation=None))

    assert result["tier"] == "blocked"
    assert "no_sl_level" in result["blockers"]
    assert "no_sl_level" in result["reason_codes"]


def test_same_inputs_produce_same_score_id():
    first = score()
    second = score()

    assert first["score_id"] == second["score_id"]
    assert first["components"] == second["components"]


def test_missing_chart_intelligence_id_blocks_when_required():
    result = score(require_chart_intelligence_id=True, chart_intelligence_id=None)

    assert result["tier"] == "blocked"
    assert "missing_chart_intelligence_id" in result["blockers"]


def test_attach_chart_score_to_candidate_sets_chart_fields():
    candidate = feeder.build_candidate_payload(
        {"symbol": "BTCUSDT", "change_pct": 20, "quote_volume": 100_000_000, "funding_pct": 0, "range_pos": 0.9},
        "2026-06-21T00:10:00+00:00",
        "exhaustion_fade",
        "SHORT",
        100,
        102,
        98,
        ["test"],
    )
    chart_score = score(side="SHORT", trend_bundle=trend("bearish"), trend_aggregate=aggregate("bearish"), zone_bundle=zones(), structure_bundle=structure("bearish", invalidation=102), liquidity_bundle=liquidity())

    updated = css.attach_chart_score_to_candidate(candidate, chart_score)

    assert updated["chart_score_value"] == chart_score["score"]
    assert updated["chart_intelligence_id"] == chart_score["chart_intelligence_id"]
    assert updated["chart_decision_eligible"] is True


def test_missing_chart_score_blocks_chart_required_candidate():
    updated = css.attach_chart_score_to_candidate({"candidate_id": "c1"}, None, chart_required=True)

    assert updated["chart_decision_eligible"] is False
    assert updated["chart_data_capability_mask"]["action"] == "skip"
