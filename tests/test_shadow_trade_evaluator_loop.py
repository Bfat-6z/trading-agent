from decimal import Decimal
from pathlib import Path

import shadow_trade_evaluator as ev
import shadow_trade_evaluator_loop as loop
from shadow_trade_logger import build_shadow_open


ENTRY_TS = "2026-06-24T01:00:00+00:00"
ENTRY_MS = ev.ts_ms(ENTRY_TS)


def _shadow() -> dict:
    return build_shadow_open(
        {"symbol": "TESTUSDT", "side": "LONG", "score": 8, "price": 100},
        {"margin_usdt": "1", "leverage": 10, "notional": "10", "confidence": 0.7, "entry_type": "MARKET_NOW"},
        entry="100",
        stop="99",
        take_profit="102",
        block_reason="paper_only",
        ts=ENTRY_TS,
    )


def _candle(open_ms: int, high: str = "102", low: str = "100", close: str = "102") -> dict:
    return {
        "open_time": open_ms,
        "open": Decimal("100"),
        "high": Decimal(high),
        "low": Decimal(low),
        "close": Decimal(close),
        "close_time": open_ms + 59_999,
    }


def _patch_paths(monkeypatch, tmp_path: Path) -> Path:
    state = tmp_path / "state"
    memory = state / "agent_memory"
    reports = tmp_path / "reports"
    memory.mkdir(parents=True)
    monkeypatch.setattr(ev, "SHADOW_JSONL", memory / "shadow_trades.jsonl")
    monkeypatch.setattr(ev, "SCALP_JSONL", state / "scalp_autotrader.jsonl")
    monkeypatch.setattr(ev, "SHADOW_CLOSE_JSONL", memory / "shadow_closes.jsonl")
    monkeypatch.setattr(ev, "SHADOW_PERFORMANCE_JSON", memory / "shadow_performance_latest.json")
    monkeypatch.setattr(ev, "RATE_LIMIT_STATE_JSON", state / "shadow_rate_limit.json")
    monkeypatch.setattr(ev, "REPORTS_DIR", reports)
    monkeypatch.setattr(loop, "LATEST_PATH", memory / "shadow_trade_evaluator_loop_latest.json")
    monkeypatch.setattr(loop, "HISTORY_PATH", memory / "shadow_trade_evaluator_loop_history.jsonl")
    monkeypatch.setattr(loop, "HEARTBEAT_PATH", state / "shadow_trade_evaluator_loop_heartbeat.json")
    monkeypatch.setattr(ev, "safe_append_event", lambda *args, **kwargs: None)
    return memory


def test_shadow_trade_evaluator_loop_writes_latest_and_heartbeat(monkeypatch, tmp_path: Path):
    _patch_paths(monkeypatch, tmp_path)
    ev.append_jsonl(ev.SHADOW_JSONL, [_shadow()])

    result = loop.run_once(max_age_hours=24, max_trades=5, fetcher=lambda *args: [_candle(ENTRY_MS)])

    assert result["status"] == "ok"
    assert result["evaluated"] == 1
    assert result["new_rows"] == 1
    assert result["can_place_live_orders"] is False
    assert loop.LATEST_PATH.exists()
    assert loop.HEARTBEAT_PATH.exists()
    closes = ev.read_jsonl(ev.SHADOW_CLOSE_JSONL)
    assert closes[0]["status"] == "closed"
    assert ev.read_json(ev.SHADOW_PERFORMANCE_JSON)["fresh_window"]["overall"]["closed"] == 1


def test_shadow_trade_evaluator_loop_retries_after_api_error_without_double_counting(monkeypatch, tmp_path: Path):
    _patch_paths(monkeypatch, tmp_path)
    shadow = _shadow()
    assumptions = ev.Assumptions()
    previous = ev.malformed_close(shadow, "old_run", assumptions, "api_error", "temporary")
    ev.append_jsonl(ev.SHADOW_JSONL, [shadow])
    ev.append_jsonl(ev.SHADOW_CLOSE_JSONL, [previous])

    result = loop.run_once(max_age_hours=24, max_trades=5, fetcher=lambda *args: [_candle(ENTRY_MS)])
    performance = ev.read_json(ev.SHADOW_PERFORMANCE_JSON)

    assert result["filter_stats"]["retryable_selected"] == 1
    assert result["new_rows"] == 1
    assert performance["overall"]["closed"] == 1
    assert performance["data_quality"]["api_error_count"] == 0
    assert len(ev.read_jsonl(ev.SHADOW_CLOSE_JSONL)) == 2


def test_shadow_trade_evaluator_loop_respects_rate_limit_backoff(monkeypatch, tmp_path: Path):
    _patch_paths(monkeypatch, tmp_path)
    ev.append_jsonl(ev.SHADOW_JSONL, [_shadow()])
    ev.record_rate_limit_backoff("http_429", 429, 600, ev.RATE_LIMIT_STATE_JSON)

    def fail_fetcher(*args):
        raise AssertionError("fetcher should not be called during active backoff")

    result = loop.run_once(max_age_hours=24, max_trades=5, fetcher=fail_fetcher)

    assert result["status"] == "rate_limited_backoff"
    assert result["evaluated"] == 1
    assert result["backoff"]["active"] is True
    closes = ev.read_jsonl(ev.SHADOW_CLOSE_JSONL)
    assert closes[0]["reason"] == "api_error"
    assert "rate_limited_backoff_until" in closes[0]["data_quality"]["error"]
