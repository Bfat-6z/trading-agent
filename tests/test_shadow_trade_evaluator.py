from decimal import Decimal
from pathlib import Path

import shadow_trade_evaluator as ev
from shadow_trade_logger import build_shadow_open

ENTRY_TS = "2026-06-20T00:00:00+00:00"
ENTRY_MS = ev.ts_ms(ENTRY_TS)


def _signal(side="LONG", score=8):
    return {"symbol": "TESTUSDT", "side": side, "score": score, "price": 100.0}


def _plan():
    return {"margin_usdt": "1", "leverage": 10, "notional": "10", "confidence": 0.7, "entry_type": "MARKET_NOW"}


def _shadow(side="LONG", entry="100", stop="99", take_profit="102", score=8):
    return build_shadow_open(
        _signal(side, score),
        _plan(),
        entry=entry,
        stop=stop,
        take_profit=take_profit,
        block_reason="memory_sleep",
        ts=ENTRY_TS,
    )


def _candle(open_ms, high, low, close, close_ms=None):
    return {
        "open_time": open_ms,
        "open": Decimal("100"),
        "high": Decimal(str(high)),
        "low": Decimal(str(low)),
        "close": Decimal(str(close)),
        "close_time": close_ms if close_ms is not None else open_ms + 59_999,
    }


def _assumptions(**kwargs):
    data = {"fee_rate": "0.0005", "slippage_bps": "0", "max_hold_seconds": 180}
    data.update(kwargs)
    return ev.Assumptions(**data)


def test_evaluate_long_take_profit_after_fees():
    row = ev.evaluate_against_candles(_shadow("LONG"), [_candle(ENTRY_MS, 102, 100, 102)], _assumptions(), "run")

    assert row["status"] == "closed"
    assert row["reason"] == "tp"
    assert Decimal(row["gross"]) == Decimal("0.2")
    assert Decimal(row["fees"]) == Decimal("0.010")
    assert Decimal(row["net"]) == Decimal("0.190")
    assert row["schema_version"] == ev.SCHEMA_VERSION
    assert row["close_id"].startswith("shadow_close_")


def test_evaluate_short_stop_loss_after_fees():
    row = ev.evaluate_against_candles(_shadow("SHORT", stop="101", take_profit="98"), [_candle(ENTRY_MS, 101, 99, 101)], _assumptions(), "run")

    assert row["status"] == "closed"
    assert row["reason"] == "sl"
    assert Decimal(row["net"]) < Decimal("0")


def test_ambiguous_same_candle_is_conservative_sl_first():
    row = ev.evaluate_against_candles(_shadow("LONG"), [_candle(ENTRY_MS, 103, 98, 100)], _assumptions(), "run")

    assert row["status"] == "closed"
    assert row["reason"] == "ambiguous_sl_first"
    assert row["data_quality"]["ambiguous"] is True
    assert Decimal(row["net"]) < Decimal("0")


def test_entry_partial_candle_is_skipped_by_default():
    row = ev.evaluate_against_candles(
        _shadow("LONG"),
        [
            _candle(ENTRY_MS - 30_000, 103, 98, 103, close_ms=ENTRY_MS + 29_999),
            _candle(ENTRY_MS + 30_000, 101, 100, 100),
        ],
        _assumptions(max_hold_seconds=0),
        "run",
    )

    assert row["status"] == "open"
    assert row["reason"] == "unresolved"
    assert row["data_quality"]["entry_partial_skipped"] is True


def test_timeout_closes_at_timeout_candle_close():
    row = ev.evaluate_against_candles(
        _shadow("LONG"),
        [_candle(ENTRY_MS, 100.5, 99.5, 100.4), _candle(ENTRY_MS + 60_000, 100.8, 99.8, 100.6)],
        _assumptions(max_hold_seconds=60),
        "run",
    )

    assert row["status"] == "closed"
    assert row["reason"] == "timeout"
    assert Decimal(row["close"]) == Decimal("100.6")


def test_malformed_shadow_is_skipped():
    row = ev.evaluate_against_candles({"shadow_id": "bad"}, [], _assumptions(), "run")

    assert row["status"] == "skipped"
    assert row["reason"] == "malformed"


def test_close_id_is_stable_for_same_assumptions_and_changes_for_new_assumptions():
    shadow = _shadow("LONG")
    first = ev.evaluate_against_candles(shadow, [_candle(ENTRY_MS, 102, 100, 102)], _assumptions(), "run1")
    second = ev.evaluate_against_candles(shadow, [_candle(ENTRY_MS, 102, 100, 102)], _assumptions(), "run2")
    third = ev.evaluate_against_candles(shadow, [_candle(ENTRY_MS, 102, 100, 102)], _assumptions(slippage_bps="5"), "run3")

    assert first["close_id"] == second["close_id"]
    assert first["close_id"] != third["close_id"]


def test_append_duplicate_close_ids_is_preventable(tmp_path: Path):
    path = tmp_path / "shadow_closes.jsonl"
    row = ev.evaluate_against_candles(_shadow("LONG"), [_candle(ENTRY_MS, 102, 100, 102)], _assumptions(), "run")

    ev.append_jsonl(path, [row])
    assert row["close_id"] in ev.existing_close_ids(path)


def test_aggregate_performance_separates_data_quality():
    rows = [
        ev.evaluate_against_candles(_shadow("LONG", score=8), [_candle(ENTRY_MS, 102, 100, 102)], _assumptions(), "run"),
        ev.evaluate_against_candles(_shadow("SHORT", stop="101", take_profit="98", score=7), [_candle(ENTRY_MS, 101, 99, 101)], _assumptions(), "run"),
        ev.evaluate_against_candles({"shadow_id": "bad"}, [], _assumptions(), "run"),
    ]

    perf = ev.aggregate_performance(rows, "run")

    assert perf["overall"]["closed"] == 2
    assert perf["overall"]["wins"] == 1
    assert perf["overall"]["losses"] == 1
    assert perf["overall"]["skipped_count"] == 1
    assert perf["segments"]["by_side"]
    assert perf["data_quality"]["selected_rows"] == 3


def test_evaluate_many_stops_fetching_after_rate_limit():
    shadows = [_shadow("LONG"), _shadow("SHORT", stop="101", take_profit="98")]
    calls = []

    def fetcher(symbol, start_ms, end_ms, interval):
        calls.append(symbol)
        raise ev.MarketDataError("http_418 rate limited", 418)

    rows = ev.evaluate_many(shadows, _assumptions(), "run", fetcher)

    assert len(calls) == 1
    assert [row["reason"] for row in rows] == ["api_error", "api_error"]
    assert rows[1]["data_quality"]["error"] == "http_418 rate limited"


def test_module_does_not_import_live_trading_client():
    source = Path(ev.__file__).read_text(encoding="utf-8")

    assert "tradingagents.binance.client" not in source
    assert "futures_create_order" not in source
    assert "load_dotenv" not in source
