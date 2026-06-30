# Phase 03: MA Ribbon And Trend Regime

Status: Complete

Completed: 2026-06-30

Validation:

- `python -m pytest -q tests/test_chart_trend_regime.py tests/test_chart_indicator_engine.py tests/test_chart_candle_service.py tests/test_chart_contracts.py tests/test_phase_05_feature_factory_core.py tests/test_paper_execution_lifecycle_loop.py tests/test_agent_status_dashboard.py` -> 91 passed.
- `python -m py_compile agent_data_contracts.py paper_candidate_feeder.py chart_candle_service.py chart_indicator_engine.py chart_trend_regime.py` -> passed.
- `git diff --check -- agent_data_contracts.py paper_candidate_feeder.py chart_candle_service.py chart_indicator_engine.py chart_trend_regime.py tests/test_chart_contracts.py tests/test_chart_candle_service.py tests/test_chart_indicator_engine.py tests/test_chart_trend_regime.py tests/fixtures/chart_contracts_v1 plans/260630-1921-chart-intelligence-v1` -> passed with LF/CRLF warnings only.

## Overview

Add chart trend context from EMA/MA ribbon alignment, slope, compression/expansion, and multi-timeframe agreement.

## Related Code

- `chart_indicator_engine.py`
- `regime_labeler.py`
- `market_feature_store.py`

## Requirements

- Detect bullish/bearish/neutral EMA ribbon.
- Measure slope and distance from EMA20/50/200.
- Detect overextension vs ATR.
- Detect ribbon compression/expansion.
- Output trend bias by timeframe and aggregate chart regime.

## Implementation Steps

1. Add `chart_trend_regime.py`.
2. Define deterministic thresholds for alignment, slope, distance, compression.
3. Add multi-timeframe agreement score.
4. Add blockers: `too_far_from_ema`, `ribbon_flat`, `mixed_timeframes`.
5. Add reason codes for scorer.

## Tests

- Uptrend fixture returns bull bias.
- Downtrend fixture returns bear bias.
- Flat/chop fixture returns neutral.
- Price far from EMA marks overextended.
- Mixed 1h/4h/1D lowers confidence.

## Done Gate

Paper candidate chart context has trend bias and overextension flags.

## Audit Questions

- Does trend detector avoid long after parabolic extension?
- Does it avoid forcing a side in chop?
