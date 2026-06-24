# Phase 02: Performance Aggregation

## Context Links

- [Plan](./plan.md)
- Depends on [Shadow Close Evaluator](./phase-01-shadow-close-evaluator.md)
- Existing learning files under `E:\keo-moi-mail\trading-agent\state\agent_memory\`

## Overview

Priority: P1.
Status: Complete.

Turn raw `shadow_close` rows into strategy metrics the agent can actually learn from.

## Requirements

- Read `state/agent_memory/shadow_closes.jsonl`.
- Produce `state/agent_memory/shadow_performance_latest.json`.
- Produce a timestamped markdown report under `plans/reports/`.
- Segment by symbol, side, score bucket, block reason, setup id when present, and market regime when present.
- Compute net stats after fee/slippage.
- Separate closed, unresolved, timeout, skipped, and ambiguous rows.
- Include `assumption_hash` and `schema_version` in summary so dashboard knows which model produced the metrics.

## Metrics

Overall and per segment:

- trades
- wins/losses
- win_rate
- gross
- fees
- slippage
- net
- avg_win
- avg_loss
- expectancy
- profit_factor
- max_drawdown
- avg_time_to_exit_seconds
- ambiguous_count
- malformed_count
- skipped_count
- unresolved_count
- timeout_count
- api_error_count
- candle_coverage_pct
- assumption_hash
- metric_mode: `closed_only` by default

## Related Code Files

Modify/create:

- `E:\keo-moi-mail\trading-agent\shadow_trade_evaluator.py` or separate `shadow_performance.py` only if evaluator becomes too large.
- `E:\keo-moi-mail\trading-agent\tests\test_shadow_performance.py`

Write outputs:

- `E:\keo-moi-mail\trading-agent\state\agent_memory\shadow_performance_latest.json`
- `E:\keo-moi-mail\trading-agent\plans\reports\{timestamp}-shadow-performance.md`

## Summary Schema

`shadow_performance_latest.json` must include:

```json
{
  "schema_version": 1,
  "updated_at": "...",
  "run_id": "...",
  "assumption_hash": "...",
  "metric_mode": "closed_only",
  "overall": {},
  "segments": {
    "by_symbol": [],
    "by_side": [],
    "by_score_bucket": [],
    "by_block_reason": [],
    "by_setup": [],
    "by_regime": []
  },
  "data_quality": {},
  "kill_candidates": [],
  "promotion_candidates": []
}
```

## Implementation Steps

1. Add aggregate function over close rows.
2. Add segment key helpers:
   - `symbol`
   - `side`
   - `score_bucket`: `0-5`, `6`, `7`, `8+`
   - `block_reason`
   - `setup_id` if available from signal/order/critic payload
   - `regime` if available
3. Add profit factor and drawdown functions.
4. Add JSON writer with stable schema.
5. Add markdown report writer with top winners/losers and kill-list candidates.
6. Add data-quality section with unresolved/skipped/ambiguous counts.
7. Add assumption mismatch handling if close rows contain multiple assumption hashes.

## Kill-List Logic

Initial conservative rules:

- Segment trades >= 20 and expectancy < 0 => candidate block.
- Segment trades >= 20 and profit_factor < 1 => candidate block.
- Segment trades >= 20 and win_rate < 0.45 => candidate block.
- Segment trades < 20 => under-sampled, do not promote or kill solely by stats.
- Any segment with unresolved_count > closed_count should be marked low confidence.
- Any segment with ambiguous_count / closed_count > 0.25 should be marked low confidence.

Promotion candidate output is informational only in this plan:

- Segment trades >= 50.
- Expectancy > 0 after fees/slippage.
- Profit factor >= 1.5.
- Max drawdown bounded.
- Ambiguous and unresolved rates acceptable.
- Still requires future paper validation before any live use.

## Todo List

- [ ] Implement aggregate stats.
- [ ] Implement segment stats.
- [ ] Implement kill-list candidate output.
- [ ] Implement markdown report.
- [ ] Implement data-quality confidence labels.
- [ ] Implement multiple-assumption handling.
- [ ] Add tests for expectancy, PF, drawdown, and segmentation.

## Success Criteria

- `shadow_performance_latest.json` is stable and dashboard-readable.
- Report clearly names what is working, failing, and under-sampled.
- Math tests cover zero-loss, zero-win, all-loss, and mixed cases.

## Risk Assessment

| Risk | Mitigation |
| --- | --- |
| False precision from low sample size | Segment outputs include sample count and under-sampled status. |
| Win-rate obsession | Primary ranking uses expectancy/PF/net, not WR alone. |
| Bad report decisions | Kill-list outputs are candidates first, not execution changes in this phase. |
| Mixed assumptions in one summary | Group by `assumption_hash` or use latest run only. |
| Unresolved trade bias | Report unresolved separately and warn when unresolved rate is high. |

## Security Considerations

- Aggregation is local-only.
- No API keys.
- No live order path.

## Completion Notes

- Aggregation implemented in `shadow_trade_evaluator.py` to keep the first pass simple.
- Writes `state/agent_memory/shadow_performance_latest.json` atomically.
- Writes timestamped markdown reports under `plans/reports/`.
- Reports data-quality counts and separates skipped/api-error rows from closed metrics.
- Current full run has 505 API-error rows due Binance HTTP 418 rate-limit; this is surfaced instead of hidden.
