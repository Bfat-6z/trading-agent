# Phase 04: Pivots And Support Resistance

Status: Complete

Completed: 2026-06-30

Validation:

- `python -m pytest -q tests/test_chart_pivots_zones.py` -> 11 passed.
- `python -m pytest -q tests/test_chart_pivots_zones.py tests/test_chart_trend_regime.py tests/test_chart_indicator_engine.py tests/test_chart_candle_service.py tests/test_chart_contracts.py tests/test_phase_05_feature_factory_core.py tests/test_paper_execution_lifecycle_loop.py tests/test_agent_status_dashboard.py` -> 102 passed.
- `python -m py_compile agent_data_contracts.py paper_candidate_feeder.py chart_candle_service.py chart_indicator_engine.py chart_trend_regime.py chart_pivot_detector.py chart_zone_detector.py` -> passed.
- `git diff --check -- agent_data_contracts.py chart_pivot_detector.py chart_zone_detector.py tests/test_chart_pivots_zones.py plans/260630-1921-chart-intelligence-v1` -> passed with LF/CRLF warning only.

## Overview

Detect swing pivots and convert them into support/resistance zones with strength and freshness.

## Related Code

- `chart_indicator_engine.py`
- `market_feature_store.py`

## Requirements

- Detect swing highs/lows using left/right windows without future leakage at decision time.
- Create zones by clustering pivots within ATR/percent tolerance.
- Score zones by touches, recency, rejection strength, volume, and timeframe weight.
- Mark current price relation: above support, below resistance, inside zone, breakout, rejection.

## Implementation Steps

1. Add `chart_pivot_detector.py`.
2. Add `chart_zone_detector.py`.
3. Use closed candles only; pivots requiring right-side confirmation are only known after confirmation candle.
4. Store zone ids with constituent pivot ids.
5. Add zone invalidation rules.

## Tests

- Pivot confirmation never uses future candles before decision cutoff.
- Repeated equal highs cluster into resistance zone.
- Old weak zones decay.
- Price inside zone blocks low-quality entries.
- Break/retest state is deterministic.

## Done Gate

Scorer can cite nearest support/resistance and zone strength.

## Audit Questions

- Is a pivot known at entry time?
- Are zones too wide for useful SL/TP?
