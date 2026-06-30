from datetime import datetime

import chart_candle_service as ccs
import chart_indicator_engine as cie


def ms(ts: str) -> int:
    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)


def kline(open_ts: str, price: float, volume: float = 100.0):
    return [
        ms(open_ts),
        str(price),
        str(price + 1),
        str(price - 1),
        str(price),
        str(volume),
        ms(open_ts) + 59_999,
        str(price * volume),
        10,
    ]


def batch_from_prices(prices, *, volume=100.0, native=True):
    rows = [kline(f"2026-06-21T00:{idx:02d}:00+00:00", float(price), volume=volume) for idx, price in enumerate(prices)]
    return ccs.build_chart_candle_batch(
        "BTCUSDT",
        "1m",
        rows,
        server_time=f"2026-06-21T00:{len(prices):02d}:00+00:00",
        ingested_at=f"2026-06-21T00:{len(prices):02d}:05+00:00",
        decision_cutoff=f"2026-06-21T00:{len(prices):02d}:05+00:00",
        native_timeframe=native,
    )


def test_indicator_fixture_values_match_expected_sma_and_vwap():
    batch = batch_from_prices(range(1, 31), volume=10)

    bundle = cie.compute_indicator_bundle(batch)

    assert bundle["degradation_state"] == "partial"
    assert bundle["indicators"]["sma"]["9"] == 26.0
    assert bundle["indicators"]["sma"]["20"] == 20.5
    assert bundle["indicators"]["vwap"]["status"] == "ok"
    assert bundle["indicators"]["vwap"]["value"] == 15.5
    assert bundle["indicator_status"]["macd"] == "ok"


def test_warmup_incomplete_does_not_fake_full_confidence():
    batch = batch_from_prices(range(1, 31))

    bundle = cie.compute_indicator_bundle(batch)

    assert bundle["warmup_complete"] is False
    assert bundle["min_candle_count"] == 200
    assert bundle["capability_mask"]["action"] == "size_cap"
    assert bundle["indicator_status"]["ema200"] == "warmup_incomplete"


def test_flat_candles_have_sane_rsi_atr_adx():
    batch = batch_from_prices([100] * 40)

    bundle = cie.compute_indicator_bundle(batch)

    assert bundle["indicators"]["rsi14"] == 50.0
    assert bundle["indicators"]["atr14"] == 2.0
    assert bundle["indicators"]["adx14"] == 0.0


def test_missing_volume_disables_vwap_and_volume_only():
    batch = batch_from_prices(range(1, 31))
    for bar in batch["bars"]:
        bar.pop("volume", None)

    bundle = cie.compute_indicator_bundle(batch)

    assert bundle["indicators"]["vwap"]["status"] == "missing_volume"
    assert bundle["indicators"]["volume_ratio"]["status"] == "missing_volume"
    assert bundle["indicators"]["rsi14"] is not None
    assert bundle["indicator_status"]["macd"] == "ok"


def test_indicator_id_changes_when_candle_input_changes():
    first = cie.compute_indicator_bundle(batch_from_prices(range(1, 31)))
    second = cie.compute_indicator_bundle(batch_from_prices(list(range(1, 30)) + [35]))

    assert first["indicator_id"] != second["indicator_id"]


def test_vwap_session_boundary_is_declared_and_replayable():
    batch = batch_from_prices(range(1, 31), volume=5)

    first = cie.compute_indicator_bundle(batch, session_timezone="UTC")
    second = cie.compute_indicator_bundle(batch, session_timezone="UTC")

    assert first["indicators"]["vwap"] == second["indicators"]["vwap"]
    assert first["indicators"]["vwap"]["session_timezone"] == "UTC"
    assert first["session"]["storage_timezone"] == "UTC"


def test_resampled_indicator_cannot_masquerade_as_native():
    batch = batch_from_prices(range(1, 31), native=False)

    bundle = cie.compute_indicator_bundle(batch)

    assert bundle["native_timeframe"] is False
    assert bundle["price_basis"] == "last_trade"
