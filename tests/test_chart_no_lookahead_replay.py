from copy import deepcopy
from datetime import datetime, timedelta, timezone

import chart_candle_service as ccs
import chart_no_lookahead_replay as replay
import chart_pivot_detector as cpd


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
        price_basis="mark",
        native_timeframe=False,
    )


def rows():
    return [
        (105, 95, 100, 100),
        (100, 90, 95, 100),
        (110, 100, 105, 100),
        (105, 95, 100, 100),
        (120, 110, 115, 100),
        (115, 105, 110, 100),
        (130, 115, 125, 100),
        (132, 120, 131, 100),
    ]


def test_future_candles_appended_do_not_alter_old_decision_hash():
    base = batch_from_ohlc(rows())
    cutoff = base["decision_cutoff"]
    future = deepcopy(base)
    later = batch_from_ohlc(rows() + [(150, 140, 145, 100)])
    future["bars"] = deepcopy(base["bars"]) + [deepcopy(later["bars"][-1])]

    first = replay.rebuild_chart_decision(base, cutoff=cutoff, side="LONG")
    second = replay.rebuild_chart_decision(future, cutoff=cutoff, side="LONG")

    assert first["summary"]["artifact_hash"] == second["summary"]["artifact_hash"]
    assert second["artifacts"]["candle_batch"]["replay"]["ignored_after_cutoff_count"] == 1


def test_pivot_needing_right_candles_appears_only_after_confirmation():
    pivot_rows = [(100, 90, 95, 100), (101, 91, 96, 100), (110, 92, 100, 120), (102, 92, 96, 100), (101, 91, 95, 100)]
    early = batch_from_ohlc(pivot_rows[:4])
    confirmed = batch_from_ohlc(pivot_rows)

    early_pivots = cpd.compute_pivot_bundle(replay.rebuild_candle_batch_at_cutoff(early, early["decision_cutoff"]), left=2, right=2)
    confirmed_pivots = cpd.compute_pivot_bundle(replay.rebuild_candle_batch_at_cutoff(confirmed, confirmed["decision_cutoff"]), left=2, right=2)

    assert early_pivots["structures"]["pivots"] == []
    assert confirmed_pivots["structures"]["pivots"]


def test_forming_candle_cannot_be_used_for_decision():
    batch = batch_from_ohlc(rows())
    batch["bars"][-1]["is_final"] = False

    result = replay.rebuild_chart_decision(batch, cutoff=batch["decision_cutoff"], side="LONG")

    assert result["summary"]["degradation_state"] == "quarantined"
    assert "forming_candle:7" in result["artifacts"]["candle_batch"]["capability_mask"]["value_errors"]


def test_replayed_score_equals_stored_score():
    batch = batch_from_ohlc(rows())
    first = replay.rebuild_chart_decision(batch, cutoff=batch["decision_cutoff"], side="LONG")
    stored_score = first["artifacts"]["score"]
    second = replay.rebuild_chart_decision(batch, cutoff=batch["decision_cutoff"], side="LONG")

    assert stored_score["score_id"] == second["artifacts"]["score"]["score_id"]
    assert stored_score["components"] == second["artifacts"]["score"]["components"]


def test_source_timestamp_violation_quarantines_output():
    batch = batch_from_ohlc(rows())
    batch["bars"][0]["known_at"] = "2099-01-01T00:00:00+00:00"

    result = replay.rebuild_chart_decision(batch, cutoff=batch["decision_cutoff"], side="LONG")

    assert result["summary"]["degradation_state"] == "quarantined"


def test_missing_finality_metadata_cannot_pass_strict_fixture():
    batch = batch_from_ohlc(rows())
    batch["bars"][0].pop("available_at", None)

    rebuilt = replay.rebuild_candle_batch_at_cutoff(batch, batch["decision_cutoff"])

    assert rebuilt["degradation_state"] == "quarantined"
    assert any(error.startswith("missing_finality_metadata") for error in rebuilt["capability_mask"]["value_errors"])


def test_later_cache_same_cutoff_preserves_price_basis_and_native_policy():
    batch = batch_from_ohlc(rows())
    result = replay.rebuild_chart_decision(batch, cutoff=batch["decision_cutoff"], side="LONG")

    assert result["summary"]["price_basis"] == "mark"
    assert result["summary"]["native_timeframe"] is False
    assert result["artifacts"]["candle_batch"]["price_basis"] == "mark"
    assert result["artifacts"]["candle_batch"]["native_timeframe"] is False
