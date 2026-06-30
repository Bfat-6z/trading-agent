# Phase 10: No-Lookahead Replay Proof

Status: Complete

Completed: 2026-06-30

Validation:

- `python -m pytest -q tests/test_chart_no_lookahead_replay.py` -> 7 passed.
- `python -m pytest -q tests/test_chart_no_lookahead_replay.py tests/test_chart_risk_model.py tests/test_chart_setup_scorer.py tests/test_chart_liquidity_detector.py tests/test_chart_structure_detector.py tests/test_chart_trendline_detector.py tests/test_chart_pivots_zones.py tests/test_chart_trend_regime.py tests/test_chart_indicator_engine.py tests/test_chart_candle_service.py tests/test_chart_contracts.py tests/test_phase_05_feature_factory_core.py tests/test_paper_execution_lifecycle_loop.py tests/test_agent_status_dashboard.py` -> 145 passed.
- `python -m py_compile agent_data_contracts.py paper_candidate_feeder.py chart_no_lookahead_replay.py chart_risk_model.py chart_setup_scorer.py chart_liquidity_detector.py chart_structure_detector.py chart_trendline_detector.py chart_pivot_detector.py chart_zone_detector.py chart_candle_service.py chart_indicator_engine.py chart_trend_regime.py` -> passed.
- `git diff --check -- agent_data_contracts.py paper_candidate_feeder.py chart_no_lookahead_replay.py chart_risk_model.py chart_setup_scorer.py chart_liquidity_detector.py chart_structure_detector.py chart_trendline_detector.py chart_pivot_detector.py chart_zone_detector.py tests/test_chart_no_lookahead_replay.py tests/test_chart_risk_model.py tests/test_chart_setup_scorer.py tests/test_chart_liquidity_detector.py tests/test_chart_structure_detector.py tests/test_chart_trendline_detector.py tests/test_chart_pivots_zones.py plans/260630-1921-chart-intelligence-v1` -> passed with LF/CRLF warnings only.
- Major gate `python -m pytest -q` remains blocked by unrelated missing optional/external deps noted in Phase 08 validation.

## Overview

Prove chart decisions use only data available at decision time.

## Related Code

- `counterfactual_replay_agent.py`
- `backtest_harness.py`
- `market_feature_store.py`
- `tests/test_phase_05_feature_factory_core.py`

## Requirements

- Golden chart replay fixtures.
- Rebuild chart features by historical cutoff.
- Hash inputs and outputs.
- Fail if any `available_at`, `known_at`, `ingested_at`, or `finalized_at` exceeds cutoff.
- Fail if any required finality timestamp is missing in paper-eligible chart data.
- Fail if future pivot confirmation changes historical score.
- Fail if native/resampled candle source or price basis differs between decision and replay.

## Implementation Steps

1. Add `tests/fixtures/chart_no_lookahead_v1/`.
2. Add replay helper to rebuild chart bundle at cutoff.
3. Add tests that append future candles and prove old score unchanged.
4. Add shuffled-input determinism test.
5. Add chart replay summary artifact.

## Tests

- Future candles appended do not alter old decision hash.
- Pivot needing right candles appears only after confirmation.
- Forming candle cannot be used for decision.
- Replayed score equals stored score.
- Source timestamp violation quarantines output.
- Permissive timestamp fallback cannot pass strict chart fixtures.
- Replay with later cache and same cutoff preserves source policy, price basis, and native/resampled status.

## Done Gate

No chart feature can affect paper decisions without no-lookahead proof.

## Audit Questions

- Can old chart score change when cache updates?
- Are pivot confirmations handled honestly?
