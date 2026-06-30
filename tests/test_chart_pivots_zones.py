from datetime import datetime, timedelta, timezone
from copy import deepcopy

import agent_data_contracts as contracts
import chart_candle_service as ccs
import chart_indicator_engine as cie
import chart_pivot_detector as cpd
import chart_zone_detector as czd


def ms(dt):
    return int(dt.timestamp() * 1000)


def kline(open_dt, high, low, close, volume=100):
    open_price = (float(high) + float(low)) / 2
    return [
        ms(open_dt),
        str(open_price),
        str(high),
        str(low),
        str(close),
        str(volume),
        ms(open_dt) + 59_999,
        str(float(close) * float(volume)),
        10,
    ]


def batch_from_ohlc(rows, *, cutoff_extra_seconds=5, server_extra_seconds=0):
    base = datetime(2026, 6, 21, tzinfo=timezone.utc)
    raw = [kline(base + timedelta(minutes=idx), high, low, close, volume) for idx, (high, low, close, volume) in enumerate(rows)]
    server_time = base + timedelta(minutes=len(rows), seconds=server_extra_seconds)
    cutoff = server_time + timedelta(seconds=cutoff_extra_seconds)
    return ccs.build_chart_candle_batch(
        "BTCUSDT",
        "1m",
        raw,
        server_time=server_time.isoformat(timespec="seconds"),
        ingested_at=cutoff.isoformat(timespec="seconds"),
        decision_cutoff=cutoff.isoformat(timespec="seconds"),
        min_candles=1,
    )


def test_pivot_confirmed_only_after_right_window_known_at():
    rows = [
        (100, 90, 95, 100),
        (101, 91, 96, 100),
        (110, 92, 100, 120),
        (102, 92, 96, 100),
        (101, 91, 95, 100),
    ]

    early = cpd.compute_pivot_bundle(batch_from_ohlc(rows[:4]), left=2, right=2)
    confirmed = cpd.compute_pivot_bundle(batch_from_ohlc(rows), left=2, right=2)

    assert early["structures"]["pivots"] == []
    pivots = confirmed["structures"]["pivots"]
    assert [pivot["kind"] for pivot in pivots] == ["high"]
    assert pivots[0]["candle_index"] == 2
    assert pivots[0]["confirmation_index"] == 4
    assert pivots[0]["confirmed_known_at"] <= confirmed["decision_cutoff"]


def test_future_candles_same_cutoff_do_not_change_confirmed_pivot_hash():
    rows = [
        (100, 90, 95, 100),
        (101, 91, 96, 100),
        (110, 92, 100, 120),
        (102, 92, 96, 100),
        (101, 91, 95, 100),
    ]
    base_batch = batch_from_ohlc(rows)
    later = batch_from_ohlc(rows + [(150, 140, 145, 100)])
    future_batch = deepcopy(base_batch)
    future_batch["bars"] = deepcopy(base_batch["bars"]) + [deepcopy(later["bars"][-1])]
    future_batch["decision_cutoff"] = base_batch["decision_cutoff"]
    future_batch["cutoff_proof"] = base_batch["cutoff_proof"]

    first = cpd.compute_pivot_bundle(base_batch, left=2, right=2)
    second = cpd.compute_pivot_bundle(future_batch, left=2, right=2)

    assert first["structure_id"] == second["structure_id"]
    assert first["structures"]["pivots"] == second["structures"]["pivots"]


def test_shuffled_bar_order_keeps_pivot_payload_stable_after_sort():
    rows = [
        (100, 90, 95, 100),
        (101, 91, 96, 100),
        (110, 92, 100, 120),
        (102, 92, 96, 100),
        (101, 91, 95, 100),
    ]
    ordered = batch_from_ohlc(rows)
    shuffled = deepcopy(ordered)
    shuffled["bars"] = [shuffled["bars"][2], shuffled["bars"][0], shuffled["bars"][4], shuffled["bars"][1], shuffled["bars"][3]]

    first = cpd.compute_pivot_bundle(ordered, left=2, right=2)
    second = cpd.compute_pivot_bundle(shuffled, left=2, right=2)

    assert first["structure_id"] == second["structure_id"]
    assert first["structures"]["pivots"] == second["structures"]["pivots"]


def test_repeated_equal_highs_cluster_into_resistance_zone():
    rows = [
        (100, 90, 95, 100),
        (102, 92, 98, 100),
        (110, 94, 100, 120),
        (103, 93, 97, 100),
        (101, 91, 96, 100),
        (104, 94, 99, 100),
        (110.1, 95, 101, 130),
        (103, 93, 98, 100),
        (101, 91, 96, 100),
        (107, 97, 104, 100),
    ]
    batch = batch_from_ohlc(rows)
    pivots = cpd.compute_pivot_bundle(batch, left=2, right=2)
    indicators = cie.compute_indicator_bundle(batch)

    zones = czd.compute_zone_bundle(pivots, candle_batch=batch, indicator_bundle=indicators)

    resistance = [zone for zone in zones["structures"]["zones"] if zone["zone_type"] == "resistance"]
    assert resistance
    top = resistance[0]
    assert top["touch_count"] == 2
    assert len(top["constituent_pivot_ids"]) == 2
    assert top["strength"] > 0.35


def test_future_candle_same_cutoff_does_not_change_zone_relation_or_hash():
    rows = [
        (100, 90, 95, 100),
        (102, 92, 98, 100),
        (110, 94, 100, 120),
        (103, 93, 97, 100),
        (101, 91, 96, 100),
        (104, 94, 99, 100),
        (110.1, 95, 101, 130),
        (103, 93, 98, 100),
        (101, 91, 96, 100),
        (107, 97, 104, 100),
    ]
    base = batch_from_ohlc(rows)
    later = batch_from_ohlc(rows + [(140, 130, 135, 100)])
    future = deepcopy(base)
    future["bars"] = deepcopy(base["bars"]) + [deepcopy(later["bars"][-1])]
    future["decision_cutoff"] = base["decision_cutoff"]
    future["cutoff_proof"] = base["cutoff_proof"]
    pivots = cpd.compute_pivot_bundle(base, left=2, right=2)

    first = czd.compute_zone_bundle(pivots, candle_batch=base)
    second = czd.compute_zone_bundle(pivots, candle_batch=future)

    assert first["structure_id"] == second["structure_id"]
    assert first["structures"]["current_price"] == second["structures"]["current_price"]
    assert first["structures"]["current_price_relation"] == second["structures"]["current_price_relation"]


def test_old_weak_zone_decays_and_is_not_nearest_active():
    rows = [
        (100, 90, 95, 100),
        (101, 91, 96, 100),
        (102, 80, 95, 100),
        (103, 92, 98, 100),
        (104, 93, 99, 100),
    ] + [(105, 95, 100, 100) for _ in range(12)]
    batch = batch_from_ohlc(rows)
    pivots = cpd.compute_pivot_bundle(batch, left=2, right=2)

    zones = czd.compute_zone_bundle(pivots, candle_batch=batch, current_price=100, decay_bars=5)

    support = [zone for zone in zones["structures"]["zones"] if zone["zone_type"] == "support"][0]
    assert support["invalidation"]["state"] == "decayed"
    assert zones["structures"]["nearest"]["support"] is None


def test_price_inside_zone_blocks_low_quality_entries():
    rows = [
        (100, 90, 95, 100),
        (101, 91, 96, 100),
        (110, 100, 106, 120),
        (102, 92, 96, 100),
        (101, 91, 95, 100),
        (111, 101, 106, 100),
    ]
    batch = batch_from_ohlc(rows)
    pivots = cpd.compute_pivot_bundle(batch, left=2, right=2)
    zones = czd.compute_zone_bundle(pivots, candle_batch=batch, current_price=110.05, percent_tolerance=0.002)

    blockers = czd.zone_blockers_for_entry(zones, side="LONG", setup_score=5.5)

    assert "inside_messy_zone" in blockers


def test_quarantined_zone_bundle_blocks_entry_helper():
    blockers = czd.zone_blockers_for_entry(
        {"degradation_state": "quarantined", "capability_mask": {"action": "skip"}, "structures": {"zones": []}, "can_place_live_orders": False, "live_permission": False},
        side="LONG",
        setup_score=9.0,
    )

    assert blockers == ["chart_structure_unavailable"]


def test_breakout_retest_state_is_deterministic():
    rows = [
        (100, 90, 95, 100),
        (102, 92, 98, 100),
        (110, 94, 100, 120),
        (103, 93, 97, 100),
        (101, 91, 96, 100),
        (104, 94, 99, 100),
        (110.1, 95, 101, 130),
        (103, 93, 98, 100),
        (112.0, 108.0, 111.5, 160),
        (112.5, 109.9, 111.8, 150),
    ]
    batch = batch_from_ohlc(rows)
    pivots = cpd.compute_pivot_bundle(batch, left=2, right=2)

    first = czd.compute_zone_bundle(pivots, candle_batch=batch, current_price=111.8)
    second = czd.compute_zone_bundle(pivots, candle_batch=batch, current_price=111.8)

    relations = [zone["price_relation"] for zone in first["structures"]["zones"] if zone["zone_type"] == "resistance"]
    assert "retest_hold" in relations
    assert first["structure_id"] == second["structure_id"]


def test_chart_structure_bundle_contract_and_live_flags():
    batch = batch_from_ohlc([(100, 90, 95, 100), (101, 91, 96, 100), (110, 92, 100, 120), (102, 92, 96, 100), (101, 91, 95, 100)])

    pivots = cpd.compute_pivot_bundle(batch, left=2, right=2)
    zones = czd.compute_zone_bundle(pivots, candle_batch=batch)

    assert pivots["contract"] == "ChartStructureBundle.v1"
    assert zones["contract"] == "ChartStructureBundle.v1"
    assert pivots["can_place_live_orders"] is False
    assert zones["can_place_live_orders"] is False
    assert zones["live_permission"] is False


def test_chart_structure_semantic_contract_rejects_bad_nested_zone():
    payload = {
        "schema_version": contracts.SCHEMA_VERSION,
        "chart_model_version": contracts.CHART_MODEL_VERSION,
        "contract": "ChartStructureBundle.v1",
        "symbol": "BTCUSDT",
        "timeframe": "1m",
        "source_ids": ["chart_test"],
        "input_event_ids": ["event_1"],
        "decision_cutoff": "2026-06-21T00:05:05+00:00",
        "cutoff_proof": {"ok": True, "errors": []},
        "degradation_state": "ok",
        "structures": {"zones": [{"zone_id": "z1", "zone_type": "support", "lower": 101, "upper": 100, "constituent_pivot_ids": ["p1"], "strength": 1.5}]},
    }

    result = contracts.validate_chart_contract("ChartStructureBundle.v1", payload)

    assert result.ok is False
    assert "zone_lower_gt_upper:0" in result.errors
    assert "zone_strength_out_of_range:0" in result.errors
