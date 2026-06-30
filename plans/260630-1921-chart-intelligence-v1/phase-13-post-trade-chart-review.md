# Phase 13: Post-Trade Chart Review

## Overview

Teach the agent from chart outcomes after paper trades close.

## Related Code

- `market_learner.py`
- `counterfactual_replay_agent.py`
- `paper_execution_lifecycle_loop.py`
- `memory_consolidation_agent.py`

## Requirements

- Classify chart process quality: `good_win`, `bad_win`, `good_loss`, `bad_loss`, `late_entry`, `early_entry`, `sl_too_tight`, `tp_too_far`, `liquidity_trap`, `structure_failed`, `valid_setup_bad_outcome`.
- Compute MFE/MAE relative to SL/TP/zones.
- Compare actual entry vs entry +/- N candles.
- Review whether exit respected structure.
- Emit lesson candidates with evidence ids.
- Run only after lifecycle validation. Mark review `learning_eligible=false` for mark-only snapshots, stale snapshots, incomplete replay, missing immutable open snapshot, or failed cutoff proof.

## Implementation Steps

1. Add `chart_post_trade_reviewer.py`.
2. Load entry/exit snapshots and replay candles.
3. Compute MFE/MAE and zone interactions.
4. Add counterfactual timing variants.
5. Emit `ChartPostTradeReview.v1`.
6. Feed memory and skill queues only when lifecycle validation passes and `learning_eligible=true`.

## Tests

- Win from bad chase is `bad_win`.
- Loss after valid setup and correct SL is `good_loss`.
- Stop before structure invalidation is `sl_too_tight`.
- Liquidity sweep trap is detected from candle path.
- Review without snapshots is degraded and not learned.
- Setup ranker ignores `learning_eligible=false` reviews.
- Review is not emitted to learning queues before lifecycle validation id exists.

## Done Gate

The agent learns chart quality, not just PnL.

## Audit Questions

- Does a lucky win get penalized?
- Does a good loss avoid killing a valid setup?
