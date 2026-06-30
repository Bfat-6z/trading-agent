# Phase 07: Liquidity Volume VWAP

Status: Complete

Completed: 2026-06-30

Validation:

- `python -m pytest -q tests/test_chart_liquidity_detector.py` -> 7 passed.
- `python -m pytest -q tests/test_chart_liquidity_detector.py tests/test_chart_structure_detector.py tests/test_chart_trendline_detector.py tests/test_chart_pivots_zones.py tests/test_chart_trend_regime.py tests/test_chart_indicator_engine.py tests/test_chart_candle_service.py tests/test_chart_contracts.py tests/test_phase_05_feature_factory_core.py tests/test_paper_execution_lifecycle_loop.py tests/test_agent_status_dashboard.py` -> 122 passed.
- `python -m py_compile agent_data_contracts.py chart_pivot_detector.py chart_zone_detector.py chart_trendline_detector.py chart_structure_detector.py chart_liquidity_detector.py chart_candle_service.py chart_indicator_engine.py chart_trend_regime.py` -> passed.
- `git diff --check -- agent_data_contracts.py chart_pivot_detector.py chart_zone_detector.py chart_trendline_detector.py chart_structure_detector.py chart_liquidity_detector.py tests/test_chart_pivots_zones.py tests/test_chart_trendline_detector.py tests/test_chart_structure_detector.py tests/test_chart_liquidity_detector.py plans/260630-1921-chart-intelligence-v1` -> passed with LF/CRLF warning only.

## Overview

Add liquidity and volume context so the agent avoids chasing poor futures entries.

## Related Code

- `chart_indicator_engine.py`
- `microstructure_observer_loop.py`
- `market_feature_store.py`

## Requirements

- Detect equal highs/lows and likely stop clusters.
- Detect wick sweeps and failed breakouts.
- Compute VWAP and price relation to VWAP.
- Detect volume expansion, exhaustion, divergence, and low-liquidity danger.
- Integrate optional orderbook/OI/funding if available without making them required.
- Detect RSI/MACD/volume divergence as weak context only; never as a standalone entry.
- Track liquidation/forced-move context if existing flow source provides it; stale data only caps confidence.

## Implementation Steps

1. Add `chart_liquidity_detector.py`.
2. Add equal high/low clustering with ATR tolerance.
3. Add wick sweep rules: wick beyond level + close back inside + volume context.
4. Add VWAP helper.
5. Add volume confirmation flags.
6. Add capability mask when optional microstructure is stale/missing.
7. Add divergence flags with explicit low-confidence weighting.

## Tests

- Equal highs create buy-side liquidity zone.
- Sweep above resistance and close below marks bearish sweep.
- Breakout with volume confirms; breakout without volume is weak.
- Missing volume disables volume confirmation.
- Optional OI/funding stale caps confidence, not hard fail.
- Divergence alone cannot pass setup score without structure/risk confirmation.

## Done Gate

Scorer can block obvious liquidity traps and weak breakouts.

## Audit Questions

- Is a sweep distinct from real breakout?
- Does missing optional data lower size/confidence?
