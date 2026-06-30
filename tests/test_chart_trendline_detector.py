from copy import deepcopy
from datetime import datetime, timedelta, timezone

import agent_data_contracts as contracts
import chart_candle_service as ccs
import chart_pivot_detector as cpd
import chart_trendline_detector as ctl


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


def batch_from_ohlc(rows):
    base = datetime(2026, 6, 21, tzinfo=timezone.utc)
    raw = [kline(base + timedelta(minutes=idx), high, low, close, volume) for idx, (high, low, close, volume) in enumerate(rows)]
    server_time = base + timedelta(minutes=len(rows))
    cutoff = server_time + timedelta(seconds=5)
    return ccs.build_chart_candle_batch(
        "BTCUSDT",
        "1m",
        raw,
        server_time=server_time.isoformat(timespec="seconds"),
        ingested_at=cutoff.isoformat(timespec="seconds"),
        decision_cutoff=cutoff.isoformat(timespec="seconds"),
        min_candles=1,
    )


def channel_rows(last_close=111):
    return [
        (100, 90, 96, 100),
        (99, 84, 90, 100),
        (106, 91, 101, 120),
        (103, 88, 96, 100),
        (110, 94, 106, 120),
        (108, 92, 101, 100),
        (114, 98, 112, 130),
        (112, 98, last_close, 100),
    ]


def pivot_bundle(rows):
    batch = batch_from_ohlc(rows)
    return batch, cpd.compute_pivot_bundle(batch, left=1, right=1)


def lines_by_type(bundle, line_type):
    return [line for line in bundle["structures"]["trendlines"] if line["line_type"] == line_type]


def test_two_confirmed_higher_lows_create_rising_support_line():
    batch, pivots = pivot_bundle(channel_rows())

    bundle = ctl.compute_trendline_bundle(pivots, candle_batch=batch, min_span_bars=2)

    support = lines_by_type(bundle, "support")[0]
    assert support["direction"] == "rising"
    assert support["touch_count"] >= 2
    assert support["current_relation"] == "holding_line"
    assert support["overlay"]["type"] == "trendline"


def test_violated_line_loses_strength_and_relation():
    clean_batch, clean_pivots = pivot_bundle(channel_rows(last_close=111))
    broken_batch, broken_pivots = pivot_bundle(channel_rows(last_close=90))

    clean = ctl.compute_trendline_bundle(clean_pivots, candle_batch=clean_batch, min_span_bars=2)
    broken = ctl.compute_trendline_bundle(broken_pivots, candle_batch=broken_batch, min_span_bars=2)

    clean_support = lines_by_type(clean, "support")[0]
    broken_support = lines_by_type(broken, "support")[0]
    assert broken_support["violation_count"] > clean_support["violation_count"]
    assert broken_support["strength"] < clean_support["strength"]
    assert broken_support["current_relation"] == "losing_line"


def test_near_parallel_channel_detected():
    batch, pivots = pivot_bundle(channel_rows())

    bundle = ctl.compute_trendline_bundle(pivots, candle_batch=batch, min_span_bars=2, parallel_slope_pct=0.01)

    channels = bundle["structures"]["channels"]
    assert channels
    assert channels[0]["direction"] == "rising"
    assert channels[0]["support_line_id"]
    assert channels[0]["resistance_line_id"]
    assert channels[0]["current_relation"] in {"inside_channel", "near_resistance", "mid_channel"}


def test_extreme_slope_rejected():
    rows = [
        (100, 90, 96, 100),
        (95, 50, 80, 100),
        (110, 90, 100, 100),
        (160, 120, 150, 100),
        (170, 140, 160, 100),
    ]
    batch, pivots = pivot_bundle(rows)

    bundle = ctl.compute_trendline_bundle(pivots, candle_batch=batch, min_span_bars=2, max_slope_pct_per_bar=0.02)

    assert lines_by_type(bundle, "support") == []
    assert "no_trendlines" in bundle["capability_mask"]["warnings"]


def test_future_pivots_are_not_used_for_trendlines():
    batch, pivots = pivot_bundle(channel_rows())
    future = deepcopy(pivots)
    future_pivot = deepcopy(pivots["structures"]["pivots"][-1])
    future_pivot["pivot_id"] = "future_pivot"
    future_pivot["sequence_index"] = 99
    future_pivot["source_index"] = 99
    future_pivot["price"] = 200
    future_pivot["confirmed_known_at"] = "2099-01-01T00:00:00+00:00"
    future["structures"]["pivots"].append(future_pivot)

    first = ctl.compute_trendline_bundle(pivots, candle_batch=batch, min_span_bars=2)
    second = ctl.compute_trendline_bundle(future, candle_batch=batch, min_span_bars=2)

    assert first["structure_id"] == second["structure_id"]
    assert first["structures"]["trendlines"] == second["structures"]["trendlines"]


def test_trendline_contract_and_live_flags():
    batch, pivots = pivot_bundle(channel_rows())

    bundle = ctl.compute_trendline_bundle(pivots, candle_batch=batch, min_span_bars=2)

    assert contracts.validate_chart_contract("ChartStructureBundle.v1", bundle).ok is True
    assert bundle["can_place_live_orders"] is False
    assert bundle["live_permission"] is False


def test_trendline_semantic_contract_rejects_bad_nested_line():
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
        "structures": {"trendlines": [{"line_id": "l1", "line_type": "support", "pivot_ids": ["p1", "p2"], "slope": 1, "intercept": 1, "current_relation": "holding_line", "strength": 1.5}]},
    }

    result = contracts.validate_chart_contract("ChartStructureBundle.v1", payload)

    assert result.ok is False
    assert "trendline_strength_out_of_range:0" in result.errors
