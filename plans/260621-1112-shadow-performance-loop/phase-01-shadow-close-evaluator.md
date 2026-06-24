# Phase 01: Shadow Close Evaluator

## Context Links

- [Plan](./plan.md)
- [Performance Roadmap](../reports/260621-1029-performance-money-roadmap.md)
- Existing code: `E:\keo-moi-mail\trading-agent\shadow_trade_logger.py`
- Existing log: `E:\keo-moi-mail\trading-agent\state\agent_memory\shadow_trades.jsonl`
- Existing mixed log: `E:\keo-moi-mail\trading-agent\state\scalp_autotrader.jsonl`

## Overview

Priority: P1.
Status: Complete.

Create a read-only evaluator that turns shadow open records into deterministic shadow close records using candle data. This is the core missing feedback loop.

## Requirements

- Read shadow opens from `state/agent_memory/shadow_trades.jsonl` first.
- Fallback/read supplementary `shadow_open` events from `state/scalp_autotrader.jsonl` if needed.
- Deduplicate by `shadow_id`.
- Fetch Binance USD-M futures klines for symbol and time window.
- Handle Binance kline pagination and request limits.
- Evaluate TP/SL hit order using OHLC candles.
- Compute gross, fees, slippage, net.
- Write close records to `state/agent_memory/shadow_closes.jsonl`.
- Include stable schema metadata: `schema_version`, `run_id`, `assumption_hash`, `close_id`.
- Track unresolved, timeout, malformed, skipped, and API-error outcomes explicitly.
- Optionally cache raw kline responses for reproducible reruns.
- Never import or call live order placement code.

## Architecture

New module/script:

- `E:\keo-moi-mail\trading-agent\shadow_trade_evaluator.py`

Core functions:

```python
def read_shadow_opens(paths: list[Path]) -> list[dict]: ...
def fetch_klines(symbol: str, start_ms: int, end_ms: int, interval: str = "1m") -> list[dict]: ...
def evaluate_against_candles(shadow: dict, candles: list[dict], fee_rate: Decimal, slippage_bps: Decimal) -> dict: ...
def append_shadow_close(path: Path, row: dict) -> None: ...
```

CLI shape:

```powershell
venv\Scripts\python.exe shadow_trade_evaluator.py --max-age-hours 48 --interval 1m --fee-rate 0.0005 --slippage-bps 2 --max-hold-seconds 180 --ambiguity-policy sl_first
```

## Output Schema

Each `shadow_close` row must include at minimum:

```json
{
  "schema_version": 1,
  "event": "shadow_close",
  "run_id": "...",
  "assumption_hash": "...",
  "close_id": "sha256(shadow_id + assumptions)",
  "shadow_id": "...",
  "symbol": "BTCUSDT",
  "side": "LONG",
  "entry_ts": "...",
  "close_ts": "...",
  "entry": "100.0",
  "close": "101.0",
  "stop": "99.0",
  "take_profit": "102.0",
  "reason": "tp|sl|timeout|unresolved|ambiguous_sl_first|api_error|malformed",
  "status": "closed|open|skipped",
  "gross": "0",
  "fees": "0",
  "slippage": "0",
  "net": "0",
  "duration_seconds": 0,
  "data_quality": {
    "interval": "1m",
    "candle_count": 0,
    "ambiguous": false,
    "incomplete_last_candle": false,
    "source": "binance_usdm_klines"
  },
  "assumptions": {
    "fee_rate": "0.0005",
    "slippage_bps": "2",
    "ambiguity_policy": "sl_first",
    "max_hold_seconds": 180
  }
}
```

## TP/SL Rules

For LONG:

- TP hit if candle high >= take_profit.
- SL hit if candle low <= stop.
- If both hit in same candle: close as `ambiguous_sl_first` by default.

For SHORT:

- TP hit if candle low <= take_profit.
- SL hit if candle high >= stop.
- If both hit in same candle: close as `ambiguous_sl_first` by default.

Close price:

- TP close uses TP price adjusted by adverse slippage.
- SL close uses stop price adjusted by adverse slippage.
- Timeout close uses the close price of the first candle at/after `entry_ts + max_hold_seconds`, adjusted by adverse slippage.
- If `--max-hold-seconds 0`, leave no-hit trades `unresolved` instead of timeout-closing them.
- Unresolved rows count in data-quality stats but are excluded from win-rate/expectancy unless a report explicitly includes mark-to-market mode.

## Binance Fetch Rules

- Use USD-M futures market-data endpoint only.
- Convert ISO timestamps to UTC milliseconds.
- Request from entry time through `entry + max_hold_seconds` when max hold is enabled, otherwise through current completed candle or `--max-age-hours` window.
- Page through klines if requested range exceeds Binance limit.
- Skip current incomplete candle unless explicitly allowed.
- If symbol is delisted/unavailable/API returns error, emit skipped row with `reason=api_error` and do not fail the whole run.

## Idempotency Rules

- `assumption_hash` is hash of fee rate, slippage, interval, max hold, ambiguity policy, evaluator schema version.
- `close_id` is hash of `shadow_id + assumption_hash`.
- Before append, read existing close IDs from output and skip duplicates.
- A rerun with different assumptions is allowed to produce a different close ID and should be visible as a different run/model.

## Related Code Files

Create:

- `E:\keo-moi-mail\trading-agent\shadow_trade_evaluator.py`
- `E:\keo-moi-mail\trading-agent\tests\test_shadow_trade_evaluator.py`

Read-only inputs:

- `E:\keo-moi-mail\trading-agent\state\agent_memory\shadow_trades.jsonl`
- `E:\keo-moi-mail\trading-agent\state\scalp_autotrader.jsonl`

Write outputs:

- `E:\keo-moi-mail\trading-agent\state\agent_memory\shadow_closes.jsonl`
- Optional cache: `E:\keo-moi-mail\trading-agent\state\market_data_cache\klines\`

## Implementation Steps

1. Add pure parsing helpers for timestamps, decimals, and Binance kline rows.
2. Add log reader with malformed-line tolerance.
3. Add duplicate-close protection by reading existing close IDs.
4. Add candle evaluator as pure function with no network.
5. Add Binance HTTP fetch wrapper with timeout and user-agent.
6. Add CLI options for dry-run, max trades, max age, fee/slippage, and output path.
7. Add CLI options for max hold, ambiguity policy, cache on/off, and incomplete-candle behavior.
8. Add `safe_append_event` event type `shadow_close` if consistent with current event store use.

## Todo List

- [ ] Implement pure candle close evaluator.
- [ ] Implement Binance kline fetcher.
- [ ] Implement JSONL reader/writer.
- [ ] Implement CLI dry-run mode.
- [ ] Implement deterministic `assumption_hash` and `close_id`.
- [ ] Implement timeout/unresolved handling.
- [ ] Implement API error skip rows.
- [ ] Implement optional candle cache.
- [ ] Add unit tests for long/short TP/SL and ambiguous candle.
- [ ] Add malformed input tests.
- [ ] Add idempotency and rerun tests.

## Success Criteria

- Offline tests pass without network.
- Running dry-run prints candidate close counts without writing files.
- Real run writes close records only for trades with enough candle data.
- Same `shadow_id` is not closed twice.

## Risk Assessment

| Risk | Mitigation |
| --- | --- |
| Same candle TP/SL unknown order | Conservative SL-first label. |
| Binance rate limit | `--max-trades`, request batching, timeout, skip on error. |
| Bad shadow records | Ignore malformed with counted warning. |
| Future leakage | Only query candles after entry timestamp and before evaluation time. |
| Selection bias from only closed trades | Report unresolved/timeout rows and exclude/include them explicitly by metric mode. |
| Duplicate metrics after rerun | Deterministic `close_id` and existing-ID skip. |
| Non-reproducible API data | Persist assumptions and optionally cache raw klines. |

## Security Considerations

- No `.env` loading required.
- No Binance signed endpoints.
- No order functions imported.
- Output is local JSONL only.

## Completion Notes

- Created `shadow_trade_evaluator.py`.
- Added deterministic `assumption_hash` and `close_id`.
- Added conservative same-candle ambiguity handling and entry-partial candle skip.
- Added timeout/unresolved/malformed/api-error outputs.
- Added Binance public kline pagination and local cache.
- Added HTTP 418/429 guard so future runs stop fetching after rate-limit instead of continuing to hammer Binance.
- Added tests in `tests/test_shadow_trade_evaluator.py`.
