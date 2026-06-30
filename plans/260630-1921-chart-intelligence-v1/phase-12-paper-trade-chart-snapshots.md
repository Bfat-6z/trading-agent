# Phase 12: Paper Trade Chart Snapshots

Status: Complete

Completed: 2026-07-01

Validation:

- `python -m pytest -q tests/test_paper_execution_lifecycle_loop.py tests/test_chart_snapshot_renderer.py tests/test_chart_risk_model.py tests/test_chart_setup_scorer.py tests/test_agent_status_dashboard.py` -> 78 passed.
- `python -m pytest -q tests/test_chart_snapshot_renderer.py tests/test_chart_no_lookahead_replay.py tests/test_chart_risk_model.py tests/test_chart_setup_scorer.py tests/test_chart_liquidity_detector.py tests/test_chart_structure_detector.py tests/test_chart_trendline_detector.py tests/test_chart_pivots_zones.py tests/test_chart_trend_regime.py tests/test_chart_indicator_engine.py tests/test_chart_candle_service.py tests/test_chart_contracts.py tests/test_phase_05_feature_factory_core.py tests/test_paper_execution_lifecycle_loop.py tests/test_agent_status_dashboard.py` -> 156 passed.
- `python -m py_compile agent_data_contracts.py paper_candidate_feeder.py chart_snapshot_renderer.py chart_no_lookahead_replay.py chart_risk_model.py chart_setup_scorer.py chart_liquidity_detector.py chart_structure_detector.py chart_trendline_detector.py chart_pivot_detector.py chart_zone_detector.py chart_candle_service.py chart_indicator_engine.py chart_trend_regime.py paper_execution_lifecycle_loop.py chart_paper_snapshot_backfill.py agent_status_dashboard.py` -> passed.
- `python -m pytest -q` -> timed out after ~129s with no captured output.
- `python -m pytest --collect-only -q --continue-on-collection-errors` -> timed out after ~129s with no captured output.

## Overview

Attach chart evidence to paper trade lifecycle.

## Related Code

- `paper_execution_lifecycle_loop.py`
- `chart_paper_snapshot_backfill.py`
- `paper_candidate_feeder.py`
- `autonomous_paper_trading_loop.py`
- `autonomous_paper_trading_brain.py`
- `market_learner.py`

## Requirements

- Candidate snapshot id at score time.
- Open snapshot id at paper fill.
- Close snapshot id at exit.
- Store `chart_score_id`, `chart_risk_plan_id`, `chart_snapshot_ids` on paper open/close rows.
- Persist immutable `paper_position_snapshot_v2` on open with candidate, feature, chart, risk, preflight, account, and source digests; copy it into close event.
- Recheck chart feature/cutoff/freshness in lifecycle open, not only in paper brain.
- If snapshot fails, trade can still be logged but chart evidence marked degraded and ineligible for chart learning.

## Implementation Notes

- Paper open now rechecks chart cutoff/freshness/capability before open.
- Chart evidence is carried on paper open/close as `chart_score_id`, `chart_risk_plan_id`, `chart_intelligence_id`, and `chart_snapshot_ids`.
- `paper_position_snapshot_v2` is captured on open with candidate, feature, chart, risk, preflight, account, and source digests, then copied into close event.
- Missing render source does not drop the trade; chart evidence becomes `degraded` and `chart_learning_eligible=false`.
- Historical snapshot backfill is diagnostic-only through `chart_paper_snapshot_backfill.py`; backfilled rows are not readiness or learning evidence.
- Dashboard paper report now includes a compact `chart_evidence` summary and compact close rows keep chart ids/status.

## Implementation Steps

1. Add chart fields to paper candidate/open/close payloads.
2. Generate snapshot before open when chart score is used.
3. Generate close snapshot in lifecycle close path.
4. Add backfill tool for existing paper trades as `diagnostic_only`, not readiness evidence.
5. Update dashboard summary data loader.
6. Move feature/cutoff/freshness preflight into lifecycle open path.
7. Carry open snapshot digests through close path for attribution.

## Tests

- Paper open has chart snapshot id when chart score used.
- Paper close links open and close snapshots.
- Snapshot failure does not lose trade close.
- Backfilled old snapshots marked diagnostic-only.
- Reconciler ignores missing diagnostic snapshots but flags missing required snapshots.
- Close event retains open feature id, chart score id, chart risk id, and source snapshot hashes.
- Lifecycle open rejects expired/stale chart candidates.

## Done Gate

Paper trade review can inspect chart state at entry and exit.

## Audit Questions

- Can chart evidence disappear after a trade closes?
- Are old backfills kept out of readiness metrics?
