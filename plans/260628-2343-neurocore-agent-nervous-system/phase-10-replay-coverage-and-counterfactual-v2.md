# Phase 10: Replay Coverage And Counterfactual V2

## Overview

Fix counterfactual coverage and variant quality so the agent learns from trades and skips objectively.

## Related Code

- `counterfactual_replay_agent.py`
- `market_data_lake.py`
- `paper_execution_simulator.py`
- `paper_trading_brain_history.jsonl`

## Implementation Steps

1. Define eligible universe: closed trades + skipped/blocked candidates with replayable data.
2. Compute coverage as `complete / eligible`, not `complete / replay_rows`.
3. Retry previously unresolved rows when candle/source data arrives.
4. Preserve raw base signal separately from generated 1R variants.
5. Add variants: entry +/- candles, SL/TP grid, trailing, time exit, lower/higher leverage, no-trade.
6. Validate no future/latest data use.
7. Include candidate census in eligible universe: all scanned candidates above prefilter, generated/ranked/skipped/expired/selected/missed, not only opens/closes.
8. Mark shadow rows as `shadow_online=true/false`, `first_computed_at`, `source_available_at_max`, `trial_seq_cutoff`; backfills are diagnostics only for readiness.
9. Eligible denominator starts at scheduled universe evaluations: scanner gaps, rate-limited skips, prefilter rejects, not-evaluated rows, expired candidates, and missed candidates all stay visible with reason.
10. Add replay fixture for missing candle window, late source arrival, unresolved-to-complete correction event, and shuffled raw input order.

## Tests

- Complete signal can be re-run if new candle cache appears.
- Coverage denominator includes eligible skipped candidates.
- Base variant equals original trade parameters.
- Replay fails closed on insufficient candle coverage.
- Candidate that was scanned but not selected remains in denominator with skip/no-trade reason.
- Backfilled shadow close cannot count as online readiness evidence.
- Scheduled evaluation gaps, rate-limited universe slices, and prefilter rejects cannot disappear from coverage denominator.
- Late candle/source arrival creates a correction event and updates replay status without rewriting original decision.

## Done Gate

Counterfactual insights can drive skill/risk patches without coverage lies.

## Audit Questions

- Did risk gate save losses or miss winners?
- Is coverage real or inflated by repeated rows?
- Which missed or expired candidates were never traded but matter for opportunity cost?
