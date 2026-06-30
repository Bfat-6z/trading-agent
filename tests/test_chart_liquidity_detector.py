from datetime import datetime, timedelta, timezone

import chart_candle_service as ccs
import chart_indicator_engine as cie
import chart_liquidity_detector as cld
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


def base_rows(last=(111, 100, 106, 100)):
    return [
        (100, 90, 95, 100),
        (102, 92, 98, 100),
        (110, 94, 100, 120),
        (103, 93, 97, 100),
        (101, 91, 96, 100),
        (104, 94, 99, 100),
        (110.1, 95, 101, 130),
        (103, 93, 98, 100),
        last,
    ]


def zone_bundle_for(batch):
    pivots = cpd.compute_pivot_bundle(batch, left=2, right=2)
    indicators = cie.compute_indicator_bundle(batch)
    return czd.compute_zone_bundle(pivots, candle_batch=batch, indicator_bundle=indicators)


def test_equal_highs_create_buy_side_liquidity_zone():
    batch = batch_from_ohlc(base_rows())
    indicators = cie.compute_indicator_bundle(batch)

    bundle = cld.compute_liquidity_bundle(batch, indicator_bundle=indicators)

    assert bundle["liquidity"]["buy_side"]
    assert bundle["liquidity"]["buy_side"][0]["kind"] == "equal_highs"
    assert bundle["liquidity"]["buy_side"][0]["touch_count"] >= 2


def test_sweep_above_resistance_close_below_marks_bearish_sweep():
    batch = batch_from_ohlc(base_rows(last=(112, 101, 109.5, 180)))
    indicators = cie.compute_indicator_bundle(batch)
    zones = zone_bundle_for(batch)

    bundle = cld.compute_liquidity_bundle(batch, indicator_bundle=indicators, zone_bundle=zones)

    events = bundle["liquidity"]["events"]
    assert any(event["event_type"] == "BEARISH_LIQUIDITY_SWEEP" for event in events)
    assert "liquidity_sweep_up" in bundle["liquidity"]["reason_codes"]


def test_breakout_with_volume_confirms_without_volume_is_weak():
    weak_batch = batch_from_ohlc(base_rows(last=(114, 105, 113, 100)))
    strong_batch = batch_from_ohlc(base_rows(last=(114, 105, 113, 1000)))
    weak_indicators = cie.compute_indicator_bundle(weak_batch)
    strong_indicators = cie.compute_indicator_bundle(strong_batch)
    zones = zone_bundle_for(weak_batch)

    weak = cld.compute_liquidity_bundle(weak_batch, indicator_bundle=weak_indicators, zone_bundle=zones)
    strong = cld.compute_liquidity_bundle(strong_batch, indicator_bundle=strong_indicators, zone_bundle=zones)

    assert any(event.get("volume_status") == "weak_breakout_no_volume" for event in weak["liquidity"]["events"])
    assert "weak_breakout_no_volume" in weak["liquidity"]["blockers"]
    assert any(event.get("volume_status") == "confirmed_breakout" for event in strong["liquidity"]["events"])
    assert "volume_confirmed" in strong["liquidity"]["reason_codes"]


def test_missing_volume_disables_volume_confirmation_only():
    batch = batch_from_ohlc(base_rows(last=(114, 105, 113, 0)))
    for bar in batch["bars"]:
        bar.pop("volume", None)
    indicators = cie.compute_indicator_bundle(batch)

    bundle = cld.compute_liquidity_bundle(batch, indicator_bundle=indicators)

    assert bundle["degradation_state"] == "partial"
    assert bundle["liquidity"]["volume"]["status"] == "missing_volume"
    assert "volume_missing" in bundle["capability_mask"]["warnings"]


def test_optional_oi_funding_stale_caps_confidence_not_hard_fail():
    batch = batch_from_ohlc(base_rows())
    indicators = cie.compute_indicator_bundle(batch)

    bundle = cld.compute_liquidity_bundle(batch, indicator_bundle=indicators, optional_context={"oi": {"status": "stale"}, "funding": {"status": "ok"}})

    assert bundle["degradation_state"] == "partial"
    assert "optional_oi_stale" in bundle["capability_mask"]["warnings"]
    assert bundle["capability_mask"]["source_confidence"] <= 0.65
    assert bundle["capability_mask"]["action"] == "size_cap"


def test_divergence_is_weak_context_only():
    batch = batch_from_ohlc(base_rows())
    indicators = cie.compute_indicator_bundle(batch)
    indicators["series"]["close"] = [100, 101, 102, 103, 104, 105]
    indicators["series"]["rsi14"] = [70, 68, 66, 64, 62, 60]

    bundle = cld.compute_liquidity_bundle(batch, indicator_bundle=indicators)

    assert bundle["liquidity"]["divergence"][0]["confidence"] == "weak_context_only"
    assert bundle["liquidity"]["liquidity_policy"]["divergence_standalone_entry_allowed"] is False


def test_liquidity_contract_and_live_flags():
    batch = batch_from_ohlc(base_rows())
    indicators = cie.compute_indicator_bundle(batch)

    bundle = cld.compute_liquidity_bundle(batch, indicator_bundle=indicators)

    assert bundle["contract"] == "ChartLiquidityBundle.v1"
    assert bundle["can_place_live_orders"] is False
    assert bundle["live_permission"] is False
