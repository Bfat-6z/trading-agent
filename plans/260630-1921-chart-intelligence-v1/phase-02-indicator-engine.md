# Phase 02: Indicator Engine

Status: Complete

Completed: 2026-06-30

Validation:

- `python -m pytest -q tests/test_chart_indicator_engine.py tests/test_chart_candle_service.py tests/test_chart_contracts.py tests/test_phase_05_feature_factory_core.py tests/test_paper_execution_lifecycle_loop.py tests/test_agent_status_dashboard.py` -> 86 passed.
- `python -m py_compile agent_data_contracts.py paper_candidate_feeder.py chart_candle_service.py chart_indicator_engine.py` -> passed.
- `git diff --check -- agent_data_contracts.py paper_candidate_feeder.py chart_candle_service.py chart_indicator_engine.py tests/test_chart_contracts.py tests/test_chart_candle_service.py tests/test_chart_indicator_engine.py tests/fixtures/chart_contracts_v1 plans/260630-1921-chart-intelligence-v1 plans/260628-2343-neurocore-agent-nervous-system/plan.md` -> passed with LF/CRLF warnings only.

## Overview

Compute deterministic indicators from closed candle batches. No TradingView-only black box for decision evidence.

## Related Code

- `market_feature_store.py`
- `chart_scan.py`
- `render_chart.py`
- `tradingagents_crypto_src/tradingagents/dataflows/crypto_indicators.py`

## Requirements

- Indicators: SMA/EMA 9/20/50/100/200, RSI14, MACD 12/26/9, ADX14, ATR14, Bollinger 20/2, VWAP session/window, volume ratio.
- Output `ChartIndicatorBundle.v1`.
- Store per-timeframe indicator values and last N series for rendering.
- Include `min_candle_count` and `warmup_complete`.
- Define session/timezone semantics for VWAP/daily pivots. Store UTC internally; display Asia/Bangkok only in UI.
- Distinguish native timeframe indicators from resampled indicators.

## Implementation Steps

1. Add `chart_indicator_engine.py`.
2. Prefer existing pure helpers in `crypto_indicators.py`; remove duplicate math only when tests prove parity.
3. Implement indicators using pure deterministic functions.
4. Add rounding policy and Decimal/float policy.
5. Add multi-timeframe bundle builder.
6. Add native/resampled provenance fields to each indicator.
7. Persist optional artifact rows under `state/chart/indicators`.

## Tests

- Fixture values match known expected outputs.
- Warmup incomplete does not produce fake confidence.
- Flat candles produce sane RSI/ATR/ADX.
- Missing volume disables VWAP/volume confirmations only.
- Indicator id changes when candle input changes.
- VWAP/session boundary uses declared timezone/session and is replayable.
- Resampled indicator cannot masquerade as native exchange timeframe.

## Done Gate

Chart scoring never recomputes indicators ad hoc.

## Audit Questions

- Are indicator warmup windows explicit?
- Are volume-based signals disabled when volume is missing?
