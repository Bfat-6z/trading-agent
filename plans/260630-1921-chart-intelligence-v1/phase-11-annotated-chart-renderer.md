# Phase 11: Annotated Chart Renderer

Status: Complete

Completed: 2026-06-30

Validation:

- `python -m pytest -q tests/test_chart_snapshot_renderer.py` -> 7 passed.
- `python -m pytest -q tests/test_chart_snapshot_renderer.py tests/test_chart_no_lookahead_replay.py tests/test_chart_risk_model.py tests/test_chart_setup_scorer.py tests/test_chart_liquidity_detector.py tests/test_chart_structure_detector.py tests/test_chart_trendline_detector.py tests/test_chart_pivots_zones.py tests/test_chart_trend_regime.py tests/test_chart_indicator_engine.py tests/test_chart_candle_service.py tests/test_chart_contracts.py tests/test_phase_05_feature_factory_core.py tests/test_paper_execution_lifecycle_loop.py tests/test_agent_status_dashboard.py` -> 152 passed.
- `python -m py_compile agent_data_contracts.py paper_candidate_feeder.py chart_snapshot_renderer.py chart_no_lookahead_replay.py chart_risk_model.py chart_setup_scorer.py chart_liquidity_detector.py chart_structure_detector.py chart_trendline_detector.py chart_pivot_detector.py chart_zone_detector.py chart_candle_service.py chart_indicator_engine.py chart_trend_regime.py` -> passed.
- `git diff --check -- agent_data_contracts.py paper_candidate_feeder.py chart_snapshot_renderer.py chart_no_lookahead_replay.py chart_risk_model.py chart_setup_scorer.py chart_liquidity_detector.py chart_structure_detector.py chart_trendline_detector.py chart_pivot_detector.py chart_zone_detector.py tests/test_chart_snapshot_renderer.py tests/test_chart_no_lookahead_replay.py tests/test_chart_risk_model.py tests/test_chart_setup_scorer.py tests/test_chart_liquidity_detector.py tests/test_chart_structure_detector.py tests/test_chart_trendline_detector.py tests/test_chart_pivots_zones.py plans/260630-1921-chart-intelligence-v1` -> passed with LF/CRLF warnings only.

## Overview

Upgrade chart rendering from simple PNG to evidence snapshots with overlays and matching metadata.

## Related Code

- `render_chart.py`
- `agent_status_dashboard.py`

## Requirements

- Render candlesticks, volume, EMA/MA ribbon, VWAP, RSI/MACD/ADX optional panels.
- Overlay S/R zones, trendlines/channels, BOS/CHOCH, liquidity sweeps, entry/SL/TP.
- Produce image plus metadata JSON.
- Use same point ids as dashboard tooltip/table/export.
- Handle missing/stale data visibly.
- Use content-addressed output names and enforce max image size, allowed extensions, and safe relative serving paths.
- Add retention/pruning policy that can delete large PNGs while preserving digest metadata.

## Implementation Steps

1. Add `chart_snapshot_renderer.py`.
2. Refactor reusable drawing from `render_chart.py`.
3. Add overlay schema support.
4. Add deterministic output path under `state/chart/snapshots/`.
5. Add safe path resolver for chart artifacts.
6. Add retention/pruning metadata.
7. Add renderer smoke tests with non-empty PNG and metadata.

## Tests

- Snapshot exists and non-empty.
- Metadata hash matches input bundle.
- Overlay ids match score/risk ids.
- Missing data creates warning overlay.
- Renderer works without display server.
- Path traversal attempts cannot serve files outside chart artifact directory.
- Retention pruning preserves metadata hashes and does not break paper trade audit.

## Done Gate

Every chart decision can produce an annotated evidence image.

## Audit Questions

- Does the image show exactly what the scorer saw?
- Can UI/tooltips map back to the same points?
