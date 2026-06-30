# Phase 09: Chart Risk Model

Status: Complete

Completed: 2026-06-30

Validation:

- `python -m pytest -q tests/test_chart_risk_model.py` -> 8 passed.
- `python -m pytest -q tests/test_chart_risk_model.py tests/test_chart_setup_scorer.py tests/test_chart_liquidity_detector.py tests/test_chart_structure_detector.py tests/test_chart_trendline_detector.py tests/test_chart_pivots_zones.py tests/test_chart_trend_regime.py tests/test_chart_indicator_engine.py tests/test_chart_candle_service.py tests/test_chart_contracts.py tests/test_phase_05_feature_factory_core.py tests/test_paper_execution_lifecycle_loop.py tests/test_agent_status_dashboard.py` -> 138 passed.
- `python -m py_compile agent_data_contracts.py paper_candidate_feeder.py chart_risk_model.py chart_setup_scorer.py chart_liquidity_detector.py chart_structure_detector.py chart_trendline_detector.py chart_pivot_detector.py chart_zone_detector.py chart_candle_service.py chart_indicator_engine.py chart_trend_regime.py` -> passed.
- `git diff --check -- agent_data_contracts.py paper_candidate_feeder.py chart_risk_model.py chart_setup_scorer.py chart_liquidity_detector.py chart_structure_detector.py chart_trendline_detector.py chart_pivot_detector.py chart_zone_detector.py tests/test_chart_risk_model.py tests/test_chart_setup_scorer.py tests/test_chart_liquidity_detector.py tests/test_chart_structure_detector.py tests/test_chart_trendline_detector.py tests/test_chart_pivots_zones.py plans/260630-1921-chart-intelligence-v1` -> passed with LF/CRLF warnings only.

## Overview

Generate paper-only SL/TP and leverage hints from chart structure.

## Related Code

- `paper_portfolio_manager.py`
- `autonomous_paper_trading_brain.py`
- `paper_execution_simulator.py`
- `risk_of_ruin_model.py`

## Requirements

- Invalidation level from structure/zone/sweep.
- SL distance must respect ATR, exchange filters, and liquidation safety.
- TP ladder from nearest zones, measured move, ATR, and R:R.
- Leverage hint capped by portfolio/risk engine; chart cannot override caps.
- Reject if R:R too poor after fee/funding/slippage assumptions.
- Risk plan must cite exchange filters, tick size, step size, min notional, leverage bracket, maintenance margin tier, and mark/index price basis for liquidation checks.
- Correlation/portfolio exposure can cap chart-approved trades, especially same-direction BTC/ETH/alt clusters.

## Implementation Steps

1. Add `chart_risk_model.py`.
2. Build `ChartRiskPlan.v1`.
3. Add `risk_hint`, not final size.
4. Connect to paper brain as one input to existing deterministic sizing.
5. Add fee/funding/slippage-aware expected move check.
6. Add price-basis guard: entry candles, mark price, index price, liquidation reference cannot be mixed silently.
7. Add portfolio-correlation cap hook before leverage hint can increase size.

## Tests

- Support-based long SL below support with buffer.
- Short SL above resistance with buffer.
- Too wide SL reduces leverage hint.
- No valid TP/R:R blocks.
- Liquidation proximity blocks high leverage.
- Tick/step/min-notional/bracket constraints are honored.
- Same-direction correlated exposure caps leverage/margin hint.
- Mark/index price basis is cited for liquidation distance.

## Done Gate

Every chart-approved paper candidate has a structure-based risk plan.

## Audit Questions

- Does chart risk model ever bypass portfolio caps?
- Does high leverage require tight valid invalidation?
