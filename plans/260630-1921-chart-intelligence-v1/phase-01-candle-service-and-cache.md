# Phase 01: Closed Candle Service And Cache

Status: Complete

Completed: 2026-06-30

Validation:

- `python -m pytest -q tests/test_chart_candle_service.py tests/test_chart_contracts.py tests/test_phase_05_feature_factory_core.py tests/test_paper_execution_lifecycle_loop.py tests/test_agent_status_dashboard.py` -> 79 passed.
- `python -m py_compile agent_data_contracts.py paper_candidate_feeder.py chart_candle_service.py` -> passed.
- `git diff --check -- agent_data_contracts.py paper_candidate_feeder.py chart_candle_service.py tests/test_chart_contracts.py tests/test_chart_candle_service.py tests/fixtures/chart_contracts_v1 plans/260630-1921-chart-intelligence-v1 plans/260628-2343-neurocore-agent-nervous-system/plan.md` -> passed with LF/CRLF warnings only.

## Overview

Create one canonical futures candle service. Stop random modules from fetching raw klines independently.

## Related Code

- `market_data_lake.py`
- `market_feature_store.py`
- `tradingagents/binance/client.py`
- `chart_scan.py`
- `render_chart.py`

## Requirements

- Fetch Binance USD-M futures OHLCV for all supported timeframes.
- Store price basis per batch: last-trade candle, mark price candle, or index price candle where available.
- Cache closed candles with `open_time`, `close_time`, `available_at`, `known_at`, `ingested_at`, `finalized_at`.
- Require explicit finality timestamps for paper-eligible chart data; missing values quarantine instead of fallback to close time.
- Exclude unclosed current candle from decision features.
- Support replay by cutoff timestamp.
- Include exchange server-time drift handling.
- Detect gaps, duplicate candles, out-of-order candles, provider outage, 429/rate-limit, symbol delisting, and newly listed insufficient history.
- Declare native vs resampled candle source. Resampling must be deterministic and never overwrite native batches.
- Emit event bus envelope for candle ingestion if bus is available; otherwise write canonical JSON.

## Implementation Steps

1. Add `chart_candle_service.py`.
2. Add cache path under `state/chart/candles/{symbol}/{timeframe}.jsonl`.
3. Normalize raw klines into `ChartCandleBatch.v1`.
4. Add cutoff query: `load_closed_candles(symbol, timeframe, cutoff, limit)`.
5. Add source/provenance ids and source manifest hashes.
6. Add stale/partial classification.
7. Add strict mode that rejects TV/provider batches without finality metadata.
8. Add `ChartSourcePolicy.v1` for source priority, price basis, rate-limit fallback, and native/resampled rules.
9. Add gap detector and exchange metadata hooks for listing status.
10. Refactor renderer/scanner later to use service, not direct klines.

## Tests

- Current forming candle excluded.
- Candle available after close time plus buffer.
- Replay cutoff returns same batch even if cache has newer candles.
- Missing cache degrades, not crashes.
- Malformed kline quarantined.
- Source ids stable for identical raw input.
- TV/latest batch with forming candle is rejected for paper decisions.
- Missing `available_at/known_at/ingested_at/finalized_at` rejects paper eligibility.
- Candle gap/duplicate/out-of-order rows quarantine affected batch.
- Last-trade vs mark/index basis is visible in batch and feature output.
- Rate-limit/provider outage degrades, not silently reuses stale data as fresh.

## Done Gate

Every downstream chart phase can call one service for closed candles.

## Audit Questions

- Can old decisions accidentally read new candles?
- Are timestamps UTC and comparable?
