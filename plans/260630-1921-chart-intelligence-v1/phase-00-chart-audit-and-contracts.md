# Phase 00: Chart Audit And Contracts

Status: Complete

Completed: 2026-06-30

Validation:

- `python -m pytest -q tests/test_chart_contracts.py tests/test_phase_05_feature_factory_core.py tests/test_paper_execution_lifecycle_loop.py tests/test_agent_status_dashboard.py` -> 69 passed.
- `python -m py_compile agent_data_contracts.py paper_candidate_feeder.py` -> passed.
- `git diff --check -- agent_data_contracts.py paper_candidate_feeder.py tests/test_chart_contracts.py tests/fixtures/chart_contracts_v1 plans/260630-1921-chart-intelligence-v1 plans/260628-2343-neurocore-agent-nervous-system/plan.md` -> passed with LF/CRLF warnings only.

## Overview

Freeze the chart domain contract before implementation. Prevent ad hoc indicators from leaking into paper decisions without schema, source, and cutoff proof.

## Related Code

- `chart_scan.py`
- `render_chart.py`
- `market_feature_store.py`
- `agent_data_contracts.py`
- `source_provenance.py`
- `paper_candidate_feeder.py`
- `tests/test_phase_05_feature_factory_core.py`

## Requirements

- Define `ChartCandleBatch.v1`, `ChartIndicatorBundle.v1`, `ChartStructureBundle.v1`, `ChartLiquidityBundle.v1`, `ChartSetupScore.v1`, `ChartRiskPlan.v1`, `ChartSnapshot.v1`, `ChartPostTradeReview.v1`, `ChartIntelligenceReport.v1`.
- Add version constants and validation helpers.
- Define allowed timeframes: `1D`, `4h`, `1h`, `15m`, `5m`, `1m`.
- Define `closed_only=true` default.
- Define freshness TTL by timeframe.
- Define degradation states: `ok`, `stale`, `partial`, `diagnostic_only`, `quarantined`.

## Implementation Steps

1. Add chart schema constants near existing data contracts.
2. Add typed validation helpers with strict required fields.
3. Add reason-code registry: `trend_aligned`, `ema_ribbon_bull`, `bos_up`, `liquidity_sweep_down`, `at_resistance`, `overextended`, `stale_candles`, etc.
4. Mark current synthetic 3-candle candidate features in `paper_candidate_feeder.py` as insufficient for chart decisions.
5. Add contract docs in this plan folder or source docstring.
6. Add minimal golden fixture skeleton with two symbols and two timeframes.

## Tests

- Missing cutoff proof rejects chart score.
- Unknown timeframe rejects chart bundle.
- Forming candle is `diagnostic_only`.
- Every contract has `schema_version`, `chart_model_version`, `source_ids`, `input_event_ids`.
- Reason codes are stable and unknown codes fail validation.
- Synthetic ticker-derived candles cannot satisfy `ChartCandleBatch.v1`.

## Done Gate

Chart contracts exist before any scoring code uses chart data.

## Audit Questions

- Can a chart signal be replayed later from exact inputs?
- Is every chart output clearly usable vs diagnostic-only?
