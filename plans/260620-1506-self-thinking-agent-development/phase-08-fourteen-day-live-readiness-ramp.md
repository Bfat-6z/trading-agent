# Phase 08: Fourteen Day Live Readiness Ramp

## Context Links

- [Plan](./plan.md)
- Depends on [Validation And Status Surface](./phase-07-validation-status-surface.md)
- Existing: `scalp_autotrader.py`, `scalp_watchdog.py`, `event_store.py`, `reflection_agent.py`

## Overview

Priority: P0 for target discipline. Make the agent improve daily and define exactly when it can progress from PAPER to shadow-live and micro-live. This phase does not guarantee an 80 percent win-rate; it guarantees the system will measure honestly and refuse live if the target is not earned.

## Requirements

- Create `live_readiness.py`.
- Create `state/agent_memory/live_readiness_latest.json`.
- Create `state/agent_memory/live_readiness_report.md`.
- Add a CLI command or script mode to evaluate readiness on demand.
- Add promotion modes: `paper`, `shadow_live`, `micro_live_candidate`, `micro_live_active`, `blocked`.
- Existing live override must remain explicit and visible; no silent live activation.

## Related Code Files

- Create: `E:\keo-moi-mail\trading-agent\live_readiness.py`
- Create: `E:\keo-moi-mail\trading-agent\tests\test_live_readiness.py`
- Modify: `E:\keo-moi-mail\trading-agent\scalp_autotrader.py`
- Modify: `E:\keo-moi-mail\trading-agent\agent_status_dashboard.py`
- Modify: `E:\keo-moi-mail\trading-agent\tests\test_scalp_autotrader.py`

## Fourteen Day Schedule

| Day | Goal | Output |
| --- | --- | --- |
| 1 | Belief ledger + setup ids working | Every paper signal has setup/belief context |
| 2 | Hypothesis engine working | Top hypotheses generated from market state |
| 3 | Inner critic blocks weak entries | Block reasons logged to SQLite |
| 4 | Cognitive supervisor loop live | Heartbeat + cognitive state latest |
| 5 | Derivatives observer integrated if available | Crowding/OI/funding context used |
| 6 | Curiosity scheduler focuses dream cycle | Dream outputs tied to weak setup/loss |
| 7 | Midpoint review | Disable weak setups, choose leading setup |
| 8 | Validation lab segmented metrics | Metrics by setup/regime/side/symbol |
| 9 | Shadow-live dry run | Would-trade records, no orders |
| 10 | Shadow-live slippage model | Estimated fill quality and stale-data checks |
| 11 | Gate review | Pass/fail vs win-rate, expectancy, PF, DD |
| 12 | Micro-live candidate only if gate passes | Size/leverage caps locked |
| 13 | Micro-live active only if explicitly enabled | Kill-switch and loss cap monitored |
| 14 | Final readiness report | Continue paper, shadow-live, or keep micro-live |

## Promotion Gate

All must pass:

- Closed PAPER trades >= 80 total.
- Leading setup closed PAPER trades >= 20.
- Latest validated PAPER win-rate >= 80 percent.
- Net PnL after modeled fees/slippage > 0.
- Expectancy > 0.
- Profit factor >= 1.5.
- Max drawdown <= 10 percent of paper equity window.
- No hidden losing segment with >= 10 trades and negative expectancy.
- Observer, dream, supervisor, critic, and executor heartbeats fresh.
- No unhandled exception bursts in logs.

## Micro-Live Caps

- Use smallest feasible notional.
- Leverage capped conservatively; do not inherit user-requested high leverage from old sessions.
- Max one live position at a time.
- Stop after two consecutive live losses.
- Stop if daily live loss cap hit.
- Stop if slippage exceeds modeled threshold.
- Stop if order lacks setup id, hypothesis id, critic verdict.

## Implementation Steps

1. Implement `evaluate_live_readiness(events, heartbeats, config)` as a pure function.
2. Read events from SQLite first, JSONL fallback second.
3. Compute metrics by global, setup, regime, side, and symbol.
4. Compute pass/fail reasons for each gate.
5. Write JSON and markdown report.
6. Add `--status` and `--once` CLI modes.
7. Wire dashboard to show readiness mode and failing gates.
8. Wire executor so live can only proceed when `live_readiness` mode allows it plus explicit operator enablement.

## Success Criteria

- Readiness report is deterministic from same event set.
- Failing gates are specific and actionable.
- Day 14 cannot silently enable live when metrics fail.
- Micro-live stops on stale data, loss streak, slippage, or missing critic metadata.

## Tests

- Empty trade history blocks live.
- 80 percent win-rate but negative expectancy blocks live.
- Positive global metrics but one bad active setup blocks live.
- Stale heartbeat blocks live.
- Passing synthetic dataset returns `micro_live_candidate`, not direct unrestricted live.

## Risks

- Risk: target pressure causes overfitting. Mitigation: segmented metrics and sample-size gates.
- Risk: high win-rate hides tail loss. Mitigation: expectancy, profit factor, drawdown, slippage checks.
- Risk: live starts too aggressively. Mitigation: shadow-live first, micro-live caps, explicit enablement.
