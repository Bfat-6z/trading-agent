from datetime import datetime, timezone

import chart_candle_service as ccs


def ms(ts: str) -> int:
    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)


def kline(open_ts: str, open_price: str = "100", close_price: str = "101"):
    return [
        ms(open_ts),
        open_price,
        "102",
        "99",
        close_price,
        "10",
        ms(open_ts) + 59_999,
        "1000",
        42,
    ]


def fixed_ingested():
    return "2026-06-21T00:03:00+00:00"


def test_build_batch_excludes_current_forming_candle():
    batch = ccs.build_chart_candle_batch(
        "BTCUSDT",
        "1m",
        [kline("2026-06-21T00:00:00+00:00"), kline("2026-06-21T00:02:00+00:00")],
        server_time="2026-06-21T00:02:30+00:00",
        ingested_at=fixed_ingested(),
        decision_cutoff="2026-06-21T00:03:00+00:00",
    )

    assert batch["degradation_state"] == "partial"
    assert batch["capability_mask"]["action"] == "skip"
    assert [row["open_time"] for row in batch["bars"]] == ["2026-06-21T00:00:00+00:00"]
    assert "excluded_forming_candles:1" in batch["capability_mask"]["warnings"]


def test_available_at_after_close_plus_latency():
    batch = ccs.build_chart_candle_batch(
        "BTCUSDT",
        "1m",
        [kline("2026-06-21T00:00:00+00:00")],
        server_time="2026-06-21T00:02:00+00:00",
        ingested_at=fixed_ingested(),
        decision_cutoff="2026-06-21T00:03:00+00:00",
        finality_latency_seconds=2,
    )

    bar = batch["bars"][0]
    assert bar["close_time"] == "2026-06-21T00:01:00+00:00"
    assert bar["available_at"] == "2026-06-21T00:01:02+00:00"
    assert bar["known_at"] == "2026-06-21T00:01:02+00:00"


def test_load_closed_candles_replay_cutoff_ignores_newer_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ccs, "CHART_CANDLE_DIR", tmp_path / "chart" / "candles")
    first = ccs.build_chart_candle_batch(
        "BTCUSDT",
        "1m",
        [kline("2026-06-21T00:00:00+00:00")],
        server_time="2026-06-21T00:02:00+00:00",
        ingested_at="2026-06-21T00:01:05+00:00",
        decision_cutoff="2026-06-21T00:01:05+00:00",
    )
    second = ccs.build_chart_candle_batch(
        "BTCUSDT",
        "1m",
        [kline("2026-06-21T00:02:00+00:00")],
        server_time="2026-06-21T00:04:00+00:00",
        ingested_at="2026-06-21T00:03:05+00:00",
        decision_cutoff="2026-06-21T00:03:05+00:00",
    )
    ccs.store_candle_batch(first)
    ccs.store_candle_batch(second)

    replay = ccs.load_closed_candles("BTCUSDT", "1m", "2026-06-21T00:01:05+00:00", limit=20)

    assert replay["degradation_state"] == "ok"
    assert [row["open_time"] for row in replay["bars"]] == ["2026-06-21T00:00:00+00:00"]


def test_missing_cache_degrades_not_crashes(tmp_path, monkeypatch):
    monkeypatch.setattr(ccs, "CHART_CANDLE_DIR", tmp_path / "chart" / "candles")

    batch = ccs.load_closed_candles("BTCUSDT", "1m", "2026-06-21T00:01:05+00:00")

    assert batch["degradation_state"] == "quarantined"
    assert "provider_error:missing_cache" in batch["capability_mask"]["value_errors"]


def test_malformed_kline_quarantines_batch():
    batch = ccs.build_chart_candle_batch(
        "BTCUSDT",
        "1m",
        [["bad"]],
        server_time="2026-06-21T00:02:00+00:00",
        ingested_at=fixed_ingested(),
        decision_cutoff="2026-06-21T00:03:00+00:00",
    )

    assert batch["degradation_state"] == "quarantined"
    assert any(error.startswith("malformed_kline") for error in batch["capability_mask"]["value_errors"])


def test_identical_raw_inputs_have_stable_batch_id():
    kwargs = {
        "server_time": "2026-06-21T00:02:00+00:00",
        "ingested_at": fixed_ingested(),
        "decision_cutoff": "2026-06-21T00:03:00+00:00",
    }

    first = ccs.build_chart_candle_batch("BTCUSDT", "1m", [kline("2026-06-21T00:00:00+00:00")], **kwargs)
    second = ccs.build_chart_candle_batch("BTCUSDT", "1m", [kline("2026-06-21T00:00:00+00:00")], **kwargs)

    assert first["batch_id"] == second["batch_id"]


def test_strict_dict_candle_missing_finality_metadata_rejects_paper_eligibility():
    batch = ccs.build_chart_candle_batch(
        "BTCUSDT",
        "1m",
        [{"open_time": "2026-06-21T00:00:00+00:00", "open": "100", "high": "101", "low": "99", "close": "100.5"}],
        server_time="2026-06-21T00:02:00+00:00",
        ingested_at=fixed_ingested(),
        decision_cutoff="2026-06-21T00:03:00+00:00",
    )

    assert batch["degradation_state"] == "quarantined"
    assert any(error.startswith("missing_finality_metadata") for error in batch["capability_mask"]["value_errors"])


def test_gap_duplicate_and_out_of_order_quarantine_batch():
    batch = ccs.build_chart_candle_batch(
        "BTCUSDT",
        "1m",
        [
            kline("2026-06-21T00:02:00+00:00"),
            kline("2026-06-21T00:00:00+00:00"),
            kline("2026-06-21T00:00:00+00:00"),
        ],
        server_time="2026-06-21T00:04:00+00:00",
        ingested_at=fixed_ingested(),
        decision_cutoff="2026-06-21T00:05:00+00:00",
    )

    assert batch["degradation_state"] == "quarantined"
    errors = batch["capability_mask"]["value_errors"]
    assert "out_of_order_candles" in errors
    assert "duplicate_candle:2026-06-21T00:00:00+00:00" in errors
    assert any(error.startswith("candle_gap:") for error in errors)


def test_price_basis_and_native_policy_are_visible():
    batch = ccs.build_chart_candle_batch(
        "BTCUSDT",
        "1m",
        [kline("2026-06-21T00:00:00+00:00")],
        server_time="2026-06-21T00:02:00+00:00",
        ingested_at=fixed_ingested(),
        decision_cutoff="2026-06-21T00:03:00+00:00",
        price_basis="mark",
        native_timeframe=False,
    )

    assert batch["price_basis"] == "mark"
    assert batch["source_policy"]["price_basis"] == "mark"
    assert batch["source_policy"]["native_timeframe"] is False
    assert batch["bars"][0]["price_basis"] == "mark"
    assert batch["bars"][0]["native_timeframe"] is False


def test_provider_error_batch_records_rate_limit_as_missing_capability():
    batch = ccs.provider_error_batch("BTCUSDT", "1m", "rate_limited_429", decision_cutoff="2026-06-21T00:01:00+00:00")

    assert batch["degradation_state"] == "quarantined"
    assert "provider_error:rate_limited_429" in batch["capability_mask"]["value_errors"]
    assert batch["can_place_live_orders"] is False
