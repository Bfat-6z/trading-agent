import chart_candle_service as ccs
import chart_indicator_engine as cie
import chart_trend_regime as ctr

from datetime import datetime, timedelta, timezone


def ms(dt):
    return int(dt.timestamp() * 1000)


def kline(open_dt, price: float, volume: float = 100.0):
    return [
        ms(open_dt),
        str(price),
        str(price + 1),
        str(price - 1),
        str(price),
        str(volume),
        ms(open_dt) + 59_999,
        str(price * volume),
        10,
    ]


def batch_from_prices(prices, timeframe="1m"):
    base = datetime(2026, 6, 21, tzinfo=timezone.utc)
    step = timedelta(seconds=ccs.timeframe_seconds(timeframe))
    rows = [kline(base + step * idx, float(price), volume=100) for idx, price in enumerate(prices)]
    server_time = base + step * len(prices)
    cutoff = server_time + timedelta(seconds=5)
    return ccs.build_chart_candle_batch(
        "BTCUSDT",
        timeframe,
        rows,
        server_time=server_time.isoformat(timespec="seconds"),
        ingested_at=cutoff.isoformat(timespec="seconds"),
        decision_cutoff=cutoff.isoformat(timespec="seconds"),
    )


def indicator_from_prices(prices, timeframe="1m"):
    return cie.compute_indicator_bundle(batch_from_prices(prices, timeframe=timeframe))


def test_uptrend_fixture_returns_bull_bias():
    indicators = indicator_from_prices(range(1, 230))

    regime = ctr.classify_timeframe_trend(indicators)

    assert regime["bias"] == "bullish"
    assert "ema_ribbon_bull" in regime["reason_codes"]
    assert "trend_aligned" in regime["reason_codes"]
    assert regime["confidence"] > 0.6


def test_downtrend_fixture_returns_bear_bias():
    indicators = indicator_from_prices(list(range(230, 1, -1)))

    regime = ctr.classify_timeframe_trend(indicators)

    assert regime["bias"] == "bearish"
    assert "ema_ribbon_bear" in regime["reason_codes"]
    assert "trend_aligned" in regime["reason_codes"]


def test_flat_chop_fixture_returns_neutral():
    indicators = indicator_from_prices([100] * 220)

    regime = ctr.classify_timeframe_trend(indicators)

    assert regime["bias"] == "neutral"
    assert "ribbon_flat" in regime["blockers"]


def test_price_far_from_ema_marks_overextended():
    prices = [100] * 210 + [180]
    indicators = indicator_from_prices(prices)

    regime = ctr.classify_timeframe_trend(indicators)

    assert regime["overextended"] is True
    assert "too_far_from_ema" in regime["blockers"]
    assert "overextended" in regime["reason_codes"]


def test_mixed_timeframes_lower_aggregate_confidence():
    one_day = ctr.classify_timeframe_trend(indicator_from_prices(range(1, 230), timeframe="1D"))
    four_hour = ctr.classify_timeframe_trend(indicator_from_prices(list(range(230, 1, -1)), timeframe="4h"))
    one_hour = ctr.classify_timeframe_trend(indicator_from_prices([100] * 220, timeframe="1h"))

    aggregate = ctr.aggregate_trend_regime({"1D": one_day, "4h": four_hour, "1h": one_hour})

    assert "mixed_timeframes" in aggregate["blockers"]
    assert aggregate["agreement_score"] < 0.75
