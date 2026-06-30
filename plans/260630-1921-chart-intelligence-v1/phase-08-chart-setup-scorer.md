# Phase 08: Chart Setup Scorer

Status: Complete

Completed: 2026-06-30

Validation:

- `python -m pytest -q tests/test_chart_setup_scorer.py` -> 8 passed.
- `python -m pytest -q tests/test_chart_setup_scorer.py tests/test_chart_liquidity_detector.py tests/test_chart_structure_detector.py tests/test_chart_trendline_detector.py tests/test_chart_pivots_zones.py tests/test_chart_trend_regime.py tests/test_chart_indicator_engine.py tests/test_chart_candle_service.py tests/test_chart_contracts.py tests/test_phase_05_feature_factory_core.py tests/test_paper_execution_lifecycle_loop.py tests/test_agent_status_dashboard.py` -> 130 passed.
- `python -m py_compile agent_data_contracts.py paper_candidate_feeder.py chart_setup_scorer.py chart_liquidity_detector.py chart_structure_detector.py chart_trendline_detector.py chart_pivot_detector.py chart_zone_detector.py chart_candle_service.py chart_indicator_engine.py chart_trend_regime.py` -> passed.
- `git diff --check -- agent_data_contracts.py paper_candidate_feeder.py chart_setup_scorer.py chart_liquidity_detector.py chart_structure_detector.py chart_trendline_detector.py chart_pivot_detector.py chart_zone_detector.py tests/test_chart_setup_scorer.py tests/test_chart_liquidity_detector.py tests/test_chart_structure_detector.py tests/test_chart_trendline_detector.py tests/test_chart_pivots_zones.py plans/260630-1921-chart-intelligence-v1` -> passed with LF/CRLF warnings only.
- Major gate `python -m pytest -q` -> blocked during collection by missing optional/external deps already required by unrelated legacy tests: `tradingagents`, `binance`, `yfinance`, `langchain_*`, `langgraph`, `questionary`.

## Overview

Convert chart evidence into deterministic candidate scores and blockers.

## Related Code

- `paper_candidate_feeder.py`
- `autonomous_paper_trading_brain.py`
- `setup_skill_library.py`
- `inner_critic.py`

## Requirements

- Setup families: trend continuation, breakout retest, range fade, liquidity sweep reversal, MA pullback, structure reversal.
- Score components: trend, structure, zone, liquidity, volume, volatility, freshness, multi-timeframe alignment.
- Output side, score, confidence, tier, blockers, reason codes, evidence ids.
- No score can use PnL outcome.
- A+/5A+ is deterministic and pre-entry.

## Implementation Steps

1. Add `chart_setup_scorer.py`.
2. Define weighted scoring config in code with version.
3. Add strict blockers: stale candles, no SL level, too wide spread/ATR, inside messy zone, overextended, conflicting HTF.
4. Add evidence resolver for dashboard.
5. Feed score into paper candidates as `chart_score`.
6. Add `chart_intelligence_id` and capability mask to `paper_candidate_feeder.build_candidate_payload()`.
7. Reject chart-required candidates when only synthetic ticker proxy candles exist.

## Tests

- Perfect fixture gets high score and reason codes.
- Stale data cannot pass.
- Conflicting higher timeframe caps score.
- Missing invalidation level blocks paper open.
- Same inputs produce same score id.
- Missing `chart_intelligence_id` blocks chart-required candidates.

## Done Gate

Paper candidates can include chart score without LLM interpretation.

## Audit Questions

- Can the scorer explain every point added/subtracted?
- Can it say no-trade clearly?
