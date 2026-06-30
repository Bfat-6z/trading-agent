from datetime import datetime, timedelta, timezone

import chart_candle_service as ccs
import chart_pivot_detector as cpd
import chart_structure_detector as csd


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


def bundle(rows):
    batch = batch_from_ohlc(rows)
    pivots = cpd.compute_pivot_bundle(batch, left=1, right=1)
    return batch, pivots, csd.compute_market_structure_bundle(pivots, candle_batch=batch, significance_pct=0.001)


def uptrend_rows(last=(132, 120, 131, 100)):
    return [
        (105, 95, 100, 100),
        (100, 90, 95, 100),
        (110, 100, 105, 100),
        (105, 95, 100, 100),
        (120, 110, 115, 100),
        (115, 105, 110, 100),
        (130, 115, 125, 100),
        last,
    ]


def downtrend_rows(last=(88, 70, 72, 100)):
    return [
        (125, 115, 120, 100),
        (130, 120, 125, 100),
        (120, 105, 110, 100),
        (125, 115, 120, 100),
        (115, 100, 105, 100),
        (118, 108, 113, 100),
        (105, 90, 95, 100),
        last,
    ]


def test_uptrend_fixture_labels_hh_hl():
    _, _, structure = bundle(uptrend_rows())
    labels = [pivot["structure_label"] for pivot in structure["structures"]["pivots"]]

    assert "HH" in labels
    assert "HL" in labels
    assert structure["structures"]["trend_state"] == "uptrend"
    assert structure["structures"]["side_bias"] == "bullish"


def test_downtrend_fixture_labels_lh_ll():
    _, _, structure = bundle(downtrend_rows())
    labels = [pivot["structure_label"] for pivot in structure["structures"]["pivots"]]

    assert "LH" in labels
    assert "LL" in labels
    assert structure["structures"]["trend_state"] == "downtrend"
    assert structure["structures"]["side_bias"] == "bearish"


def test_close_through_prior_swing_triggers_bos():
    _, _, structure = bundle(uptrend_rows(last=(145, 125, 140, 100)))

    events = structure["structures"]["structure_events"]
    assert any(event["event_type"] == "BOS_UP" for event in events)
    assert "bos_up" in structure["structures"]["reason_codes"]


def test_wick_only_sweep_does_not_become_bos():
    _, _, structure = bundle(uptrend_rows(last=(145, 110, 119, 100)))

    events = structure["structures"]["structure_events"]
    assert any(event["event_type"] == "WICK_SWEEP_UP" for event in events)
    assert not any(event["event_type"] == "BOS_UP" for event in events)
    assert "bos_up" not in structure["structures"]["reason_codes"]


def test_choch_requires_prior_trend_context():
    _, _, up_break = bundle(uptrend_rows(last=(118, 90, 92, 100)))
    flat_batch = batch_from_ohlc([(105, 95, 100, 100), (100, 90, 95, 100), (105, 95, 100, 100), (100, 90, 95, 100), (106, 96, 101, 100), (101, 91, 96, 100)])
    flat_pivots = cpd.compute_pivot_bundle(flat_batch, left=1, right=1)
    flat = csd.compute_market_structure_bundle(flat_pivots, candle_batch=flat_batch, significance_pct=0.001)

    assert any(event["event_type"] == "CHOCH_DOWN" for event in up_break["structures"]["structure_events"])
    assert not any(event["event_type"].startswith("CHOCH") for event in flat["structures"]["structure_events"])


def test_structure_contract_and_live_flags():
    _, _, structure = bundle(uptrend_rows())

    assert structure["contract"] == "ChartStructureBundle.v1"
    assert structure["can_place_live_orders"] is False
    assert structure["live_permission"] is False
