# Phase 02: Setup Skill Library

## Context Links

- [Plan](./plan.md)
- Depends on [Belief Ledger](./phase-01-belief-ledger.md)
- Existing: `market_learner.py`, `dream_cycle.py`, `scalp_autotrader.py`

## Overview

Priority: P0. Status: complete. Replace vague `A+` wording with named setup skills that can be measured and versioned.

## Requirements

- Create `setup_skill_library.py`.
- Store skills in `state/agent_memory/setup_skills.json`.
- Initial setup ids: `momentum_continuation`, `exhaustion_fade`, `liquidation_snapback`, `funding_squeeze`, `range_breakout`, `false_breakout`, `news_catalyst_chase`.
- Each skill has prerequisites, invalidations, entry pattern, stop template, target template, expected hold time, enabled flag, evidence counters.

## Related Code Files

- Create: `E:\keo-moi-mail\trading-agent\setup_skill_library.py`
- Create: `E:\keo-moi-mail\trading-agent\tests\test_setup_skill_library.py`
- Modify later: `scalp_autotrader.py`, `dream_cycle.py`, `market_learner.py`

## Implementation Steps

1. Define static defaults for setup skills.
2. Add loader that merges defaults with persisted learned stats.
3. Implement `match_setup(snapshot, signal, context)` returning candidate setup ids and reasons.
4. Implement `record_setup_outcome(setup_id, outcome)` for paper close events.
5. Expose `skill_summary()` for dashboard/supervisor.
6. Keep rules deterministic; LLM may suggest edits but cannot change persisted rules without validation.

## Success Criteria

- Every setup has stable id and version.
- Missing file auto-creates defaults.
- Setup matching returns reasons, not just labels.
- Paper outcome updates counters by setup/regime.

## Completion Notes

- Implemented `setup_skill_library.py`.
- Implemented `tests/test_setup_skill_library.py`.
- Verified with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 venv\Scripts\python.exe -m pytest tests\test_setup_skill_library.py -q`.
- Result: 8 passed.

## Tests

- Default skill initialization.
- Merge persisted stats with defaults.
- Match exhaustion candidate from range/extreme inputs.
- Reject disabled setup.
- Outcome update changes counters and expectancy fields.

## Risks

- Risk: too many setups too early. Mitigation: start seven, disable low-evidence skills.
- Risk: setup labels contaminate results. Mitigation: labels recorded at decision time only.
