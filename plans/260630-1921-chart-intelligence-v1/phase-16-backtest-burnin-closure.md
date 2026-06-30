# Phase 16: Backtest Burn-In And Closure

## Overview

Validate chart intelligence before it influences broader paper sizing or readiness scoring.

## Related Code

- `backtest_harness.py`
- `counterfactual_replay_agent.py`
- `real_scoring_board.py`
- `agent_status_dashboard.py`

## Requirements

- Backtest chart setups over frozen fixture windows.
- Walk-forward split by time, not random.
- Compare chart-enabled vs chart-disabled paper decisions.
- Track PF, expectancy, max DD, fee/funding/slippage drag, effective N, lower confidence bound.
- Require burn-in before chart score can increase paper sizing hints.
- Backtests must include delisted/unavailable/newly listed symbols in universe-at-time manifests.
- Trial report must show chart data outages/rate-limit gaps and how many candidates were degraded or skipped.

## Implementation Steps

1. Add chart backtest scenario pack.
2. Add `chart_enabled` experiment variant.
3. Add no-lookahead and cost-complete scoring gates.
4. Add burn-in dashboard card.
5. Add closure report with pass/fail and next work.

## Tests

- Walk-forward rejects train/test leakage.
- Scoreboard includes cost completeness vector.
- Low sample size cannot pass.
- Chart-enabled regression below baseline blocks promotion.
- Closure report includes evidence ids and failed attempts.
- Survivorship-bias fixture fails if only current active symbols are used.
- Outage/rate-limit gap is counted as missing capability, not ignored.

## Done Gate

Chart layer is either promoted for paper influence or explicitly kept diagnostic-only with reasons.

## Audit Questions

- Did chart signals improve expectancy after costs?
- Is any improvement just overfit?
