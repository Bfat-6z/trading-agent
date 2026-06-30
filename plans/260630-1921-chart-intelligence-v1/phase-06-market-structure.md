# Phase 06: Market Structure

Status: Complete

Completed: 2026-06-30

Validation:

- `python -m pytest -q tests/test_chart_structure_detector.py` -> 6 passed.
- `python -m pytest -q tests/test_chart_structure_detector.py tests/test_chart_trendline_detector.py tests/test_chart_pivots_zones.py tests/test_chart_trend_regime.py tests/test_chart_indicator_engine.py tests/test_chart_candle_service.py tests/test_chart_contracts.py tests/test_phase_05_feature_factory_core.py tests/test_paper_execution_lifecycle_loop.py tests/test_agent_status_dashboard.py` -> 115 passed.
- `python -m py_compile agent_data_contracts.py chart_pivot_detector.py chart_zone_detector.py chart_trendline_detector.py chart_structure_detector.py chart_candle_service.py chart_indicator_engine.py chart_trend_regime.py` -> passed.
- `git diff --check -- agent_data_contracts.py chart_pivot_detector.py chart_zone_detector.py chart_trendline_detector.py chart_structure_detector.py tests/test_chart_pivots_zones.py tests/test_chart_trendline_detector.py tests/test_chart_structure_detector.py plans/260630-1921-chart-intelligence-v1` -> passed with LF/CRLF warning only.

## Overview

Detect HH/HL/LH/LL, BOS, CHOCH, range, and trend state from pivots/zones.

## Related Code

- `chart_pivot_detector.py`
- `chart_zone_detector.py`
- `chart_trendline_detector.py`

## Requirements

- Label swing sequence: higher high, higher low, lower high, lower low.
- Detect break of structure (BOS).
- Detect change of character (CHOCH).
- Detect range vs trend.
- Produce structure side bias and invalidation level.

## Implementation Steps

1. Add `chart_structure_detector.py`.
2. Define swing significance threshold using ATR and timeframe.
3. Add BOS/CHOCH rules using candle close, not wick only, unless explicitly configured.
4. Add range detector with high/low boundaries.
5. Add structure reason codes for scorer.

## Tests

- Uptrend fixture labels HH/HL.
- Downtrend fixture labels LH/LL.
- Close through prior swing triggers BOS.
- Wick-only sweep does not become BOS unless rule says sweep.
- CHOCH requires prior trend context.

## Done Gate

Chart score can distinguish continuation, reversal, and range setups.

## Audit Questions

- Is structure hindsight-free?
- Does BOS require close confirmation?
