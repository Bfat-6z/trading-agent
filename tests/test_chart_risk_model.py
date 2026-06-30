import chart_risk_model as crm


def cutoff_proof():
    return {"ok": True, "decision_cutoff": "2026-06-21T00:10:00+00:00", "errors": []}


def chart_score():
    return {
        "score_id": "score_1",
        "symbol": "BTCUSDT",
        "side": "LONG",
        "source_ids": ["chart"],
        "input_event_ids": ["e1"],
        "decision_cutoff": "2026-06-21T00:10:00+00:00",
        "cutoff_proof": cutoff_proof(),
        "degradation_state": "ok",
    }


def zone_bundle():
    return {
        "structures": {
            "nearest": {
                "support": {"zone_id": "support_1", "lower": 98, "upper": 99},
                "resistance": {"zone_id": "resistance_1", "lower": 106, "upper": 107},
            }
        }
    }


def structure_bundle(side="LONG"):
    return {"structures": {"invalidation_level": 99 if side == "LONG" else 101}}


def indicator(atr=1.0):
    return {"indicators": {"atr14": atr}}


def instrument(max_leverage=50, maintenance=0.005):
    return {"tick_size": 0.1, "step_size": 0.001, "min_notional": 5, "max_leverage": max_leverage, "leverage_bracket": "tier_1", "maintenance_margin_rate": maintenance}


def test_support_based_long_sl_below_support_with_buffer():
    plan = crm.compute_chart_risk_plan(symbol="BTCUSDT", side="LONG", entry_reference=100, chart_score=chart_score(), zone_bundle=zone_bundle(), structure_bundle=structure_bundle(), indicator_bundle=indicator(), instrument=instrument(), mark_price=100, index_price=100)

    assert plan["sl"] < 98
    assert "support_zone" in plan["invalidation"]["source"]
    assert plan["capability_mask"]["action"] in {"normal", "size_cap"}


def test_short_sl_above_resistance_with_buffer():
    plan = crm.compute_chart_risk_plan(symbol="BTCUSDT", side="SHORT", entry_reference=100, chart_score=chart_score(), zone_bundle=zone_bundle(), structure_bundle=structure_bundle("SHORT"), indicator_bundle=indicator(), instrument=instrument(), mark_price=100, index_price=100)

    assert plan["sl"] > 107
    assert "resistance_zone" in plan["invalidation"]["source"]


def test_too_wide_sl_reduces_leverage_hint():
    wide_zones = {"structures": {"nearest": {"support": {"zone_id": "s", "lower": 80, "upper": 81}, "resistance": {"zone_id": "r", "lower": 140, "upper": 141}}}}

    plan = crm.compute_chart_risk_plan(symbol="BTCUSDT", side="LONG", entry_reference=100, chart_score=chart_score(), zone_bundle=wide_zones, structure_bundle={"structures": {"invalidation_level": 80}}, indicator_bundle=indicator(), instrument=instrument(), mark_price=100, index_price=100)

    assert plan["risk_hint"]["leverage_hint"] <= 3
    assert plan["risk_hint"]["stop_distance_pct"] > 0.15


def test_no_valid_tp_rr_blocks():
    no_tp_zone = {"structures": {"nearest": {"support": {"zone_id": "s", "lower": 98, "upper": 99}, "resistance": {"zone_id": "r", "lower": 100.2, "upper": 100.3}}}}

    plan = crm.compute_chart_risk_plan(symbol="BTCUSDT", side="LONG", entry_reference=100, chart_score=chart_score(), zone_bundle=no_tp_zone, structure_bundle=structure_bundle(), indicator_bundle=indicator(atr=0), instrument=instrument(), mark_price=100, index_price=100, min_rr=2.0)

    assert "no_valid_tp_rr_after_costs" in plan["capability_mask"]["value_errors"]
    assert plan["capability_mask"]["action"] == "skip"


def test_liquidation_proximity_blocks_high_leverage():
    thin_zone = {"structures": {"nearest": {"support": None, "resistance": {"zone_id": "r", "lower": 106, "upper": 107}}}}
    plan = crm.compute_chart_risk_plan(symbol="BTCUSDT", side="LONG", entry_reference=100, chart_score=chart_score(), zone_bundle=thin_zone, structure_bundle={"structures": {"invalidation_level": 99.8}}, indicator_bundle=indicator(atr=0.1), instrument=instrument(max_leverage=50, maintenance=0.02), mark_price=100, index_price=100)

    assert plan["risk_hint"]["leverage_hint"] < 50
    assert "liquidation_proximity_reduced_leverage" in plan["capability_mask"]["warnings"]


def test_tick_step_min_notional_bracket_constraints_are_cited():
    plan = crm.compute_chart_risk_plan(symbol="BTCUSDT", side="LONG", entry_reference=100, chart_score=chart_score(), zone_bundle=zone_bundle(), structure_bundle=structure_bundle(), indicator_bundle=indicator(), instrument=instrument(), mark_price=100, index_price=100)

    assert plan["exchange_filters"]["tick_size"] == 0.1
    assert plan["exchange_filters"]["step_size"] == 0.001
    assert plan["exchange_filters"]["min_notional"] == 5
    assert plan["exchange_filters"]["leverage_bracket"] == "tier_1"


def test_same_direction_correlated_exposure_caps_leverage():
    plan = crm.compute_chart_risk_plan(symbol="BTCUSDT", side="LONG", entry_reference=100, chart_score=chart_score(), zone_bundle=zone_bundle(), structure_bundle=structure_bundle(), indicator_bundle=indicator(), instrument=instrument(), portfolio_context={"same_direction_exposure_usd": 500, "same_direction_leverage_cap": 5}, mark_price=100, index_price=100)

    assert plan["risk_hint"]["leverage_hint"] <= 5
    assert "correlated_exposure_cap" in plan["capability_mask"]["warnings"]


def test_mark_index_price_basis_is_cited_for_liquidation_distance():
    plan = crm.compute_chart_risk_plan(symbol="BTCUSDT", side="LONG", entry_reference=100, chart_score=chart_score(), zone_bundle=zone_bundle(), structure_bundle=structure_bundle(), indicator_bundle=indicator(), instrument=instrument(), mark_price=99.8, index_price=100.1)

    assert plan["price_basis_refs"]["entry"] == "last_trade"
    assert plan["price_basis_refs"]["mark_price"] == 99.8
    assert plan["price_basis_refs"]["index_price"] == 100.1
    assert plan["price_basis_refs"]["liquidation_reference"] == "mark_price"
    assert plan["can_place_live_orders"] is False
