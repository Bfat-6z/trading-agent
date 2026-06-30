# Phase 05: Trendlines And Channels

Status: Complete

Completed: 2026-06-30

Validation:

- `python -m pytest -q tests/test_chart_trendline_detector.py` -> 7 passed.
- `python -m pytest -q tests/test_chart_trendline_detector.py tests/test_chart_pivots_zones.py tests/test_chart_trend_regime.py tests/test_chart_indicator_engine.py tests/test_chart_candle_service.py tests/test_chart_contracts.py tests/test_phase_05_feature_factory_core.py tests/test_paper_execution_lifecycle_loop.py tests/test_agent_status_dashboard.py` -> 109 passed.
- `python -m py_compile agent_data_contracts.py chart_pivot_detector.py chart_zone_detector.py chart_trendline_detector.py chart_candle_service.py chart_indicator_engine.py chart_trend_regime.py` -> passed.
- `git diff --check -- agent_data_contracts.py chart_pivot_detector.py chart_zone_detector.py chart_trendline_detector.py tests/test_chart_pivots_zones.py tests/test_chart_trendline_detector.py plans/260630-1921-chart-intelligence-v1` -> passed with LF/CRLF warning only.

## Overview

Detect simple, explainable trendlines and channels from confirmed pivots.

## Related Code

- `chart_pivot_detector.py`
- `chart_zone_detector.py`

## Requirements

- Build candidate lines from confirmed pivot pairs.
- Validate with touch count, violation count, slope sanity, recency, and ATR tolerance.
- Detect rising/falling channels when parallel support/resistance lines exist.
- Output current relation: holding line, losing line, breakout, fakeout, mid-channel.

## Implementation Steps

1. Add `chart_trendline_detector.py`.
2. Generate limited candidate lines to avoid O(N^2) blowup.
3. Score line quality deterministically.
4. Add channel pairing logic.
5. Add overlay payload for renderer.

## Tests

- Two confirmed higher lows create rising support line.
- Violated line loses strength.
- Near-parallel channel detected.
- Extreme slope rejected.
- Future pivots are not used.

## Done Gate

Chart snapshots can draw trendlines/channels with ids and confidence.

## Audit Questions

- Are lines explainable enough to debug?
- Is line fitting over-optimized to past candles?
