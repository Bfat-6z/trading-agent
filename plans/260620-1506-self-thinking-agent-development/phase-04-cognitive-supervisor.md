# Phase 04: Cognitive Supervisor

## Context Links

- [Plan](./plan.md)
- Depends on phases 01-03
- Existing: `reflection_agent.py`, `dream_cycle.py`, `event_store.py`

## Overview

Priority: P0/P1. Status: complete. Add a scheduler that coordinates observe, retrieve, hypothesize, simulate, critique, and update.

## Requirements

- Create `cognitive_supervisor.py`.
- Run every 15-30 minutes in PAPER mode.
- Write `state/cognitive_supervisor_heartbeat.json`.
- Write `state/agent_memory/cognitive_state_latest.json`.
- Never place live trades.

## Related Code Files

- Create: `E:\keo-moi-mail\trading-agent\cognitive_supervisor.py`
- Create: `E:\keo-moi-mail\trading-agent\tests\test_cognitive_supervisor.py`
- Modify: `scalp_watchdog.py` or add separate launcher later.

## Implementation Steps

1. Load market snapshot, bias, dream latest, paper events, beliefs, setup skills, hypotheses.
2. Select focus: weakest belief, highest-risk setup, or newest market anomaly.
3. Ask hypothesis engine for candidate hypotheses.
4. Request dream/replay/paper experiment plans.
5. Update belief ledger from completed experiments only.
6. Publish conservative `bias_proposal`, separate from `execution_bias.json` at first.
7. After tests, allow supervisor to tighten existing bias, never loosen.

## Success Criteria

- Supervisor can run once and loop.
- Heartbeat updates.
- Produces focus item, hypothesis list, and bias proposal.
- Does not alter live config or execute orders.

## Completion Notes

- Implemented `cognitive_supervisor.py`.
- Implemented `tests/test_cognitive_supervisor.py`.
- Supervisor reads market, bias, dream, hypotheses, belief ledger, setup skills, and paper logs.
- Produces `state/agent_memory/cognitive_state_latest.json`, markdown report, history JSONL, and heartbeat.
- Bias proposal is tighten-only and cannot loosen controls.
- Ran once on live state: focus=`dream_high_risk`, hypotheses=5.
- Verified with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 venv\Scripts\python.exe -m pytest tests -q`.
- Result: 88 passed, 3 warnings.

## Tests

- Run once with empty state.
- Run once with sample market/belief/setup state.
- Bias proposal can tighten min score/block symbols.
- Invalid LLM response ignored if LLM support is later added.

## Risks

- Risk: supervisor fights reflection/dream cycle. Mitigation: one-way tighten-only merge.
- Risk: too much LLM cost/latency. Mitigation: deterministic core, LLM optional.
