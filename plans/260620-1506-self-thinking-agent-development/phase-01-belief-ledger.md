# Phase 01: Belief Ledger

## Context Links

- [Plan](./plan.md)
- [Research](../reports/260620-1502-self-thinking-agent-roadmap.md)
- Existing: `event_store.py`, `market_learner.py`, `reflection_agent.py`

## Overview

Priority: P0. Status: complete. Create persistent structured beliefs so the agent can know which ideas are strengthening, weakening, or rejected.

## Requirements

- Create `belief_ledger.py`.
- Store beliefs in `state/agent_memory/belief_ledger.json` and mirror updates into SQLite events/snapshots.
- Belief schema: `belief_id`, `statement`, `scope`, `topic`, `confidence`, `evidence_for`, `evidence_against`, `status`, `created_at`, `updated_at`, `last_tested_at`.
- Status values: `candidate`, `active`, `weakened`, `rejected`.
- Confidence must be bounded `0.0 <= confidence <= 1.0`.

## Related Code Files

- Create: `E:\keo-moi-mail\trading-agent\belief_ledger.py`
- Create: `E:\keo-moi-mail\trading-agent\tests\test_belief_ledger.py`
- Modify later: `reflection_agent.py`, `dream_cycle.py`

## Implementation Steps

1. Define pure functions: `default_ledger`, `load_ledger`, `save_ledger`, `upsert_belief`, `add_evidence`, `decay_stale_beliefs`.
2. Use deterministic ids: hash of normalized statement + scope.
3. Add evidence records with `source`, `event_id`, `weight`, `summary`, `ts`.
4. Update confidence using bounded weighted delta, not LLM free text.
5. Write CLI: `--status`, `--add-belief`, `--add-evidence`, `--decay`.
6. Append `belief_update` events via `safe_append_event`.

## Success Criteria

- Can create/update/reject beliefs without duplicate records.
- Confidence never leaves bounds.
- Corrupt/missing JSON returns safe empty ledger.
- SQLite event write failures do not crash the agent.

## Completion Notes

- Implemented `belief_ledger.py`.
- Implemented `tests/test_belief_ledger.py`.
- Verified with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 venv\Scripts\python.exe -m pytest tests -q`.
- Result: 65 passed, 3 warnings.

## Tests

- New belief creation.
- Duplicate upsert merges.
- Evidence for raises confidence.
- Evidence against lowers confidence.
- Stale belief decay works.
- Malformed store handled safely.

## Risks

- Risk: belief text becomes vague. Mitigation: require `scope` and `topic`.
- Risk: confidence becomes fake precision. Mitigation: only update from counted evidence.
