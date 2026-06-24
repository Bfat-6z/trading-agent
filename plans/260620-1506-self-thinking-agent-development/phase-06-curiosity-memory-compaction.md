# Phase 06: Curiosity And Memory Compaction

## Context Links

- [Plan](./plan.md)
- Depends on phases 01-05
- Existing: `reflection_agent.py`, `dream_cycle.py`, `event_store.py`

## Overview

Priority: P1. Make sleep/dream useful by choosing what to study and compressing raw logs into long-term memory.

Status: Complete.

## Requirements

- Create `curiosity_scheduler.py`.
- Create `memory_compactor.py`.
- Curiosity picks one focus per cycle: weakest setup, confusing loss, under-sampled regime, contradictory belief.
- Compactor writes semantic memory summaries under `state/agent_memory/semantic_memory.json`.

## Related Code Files

- Create: `E:\keo-moi-mail\trading-agent\curiosity_scheduler.py`
- Create: `E:\keo-moi-mail\trading-agent\memory_compactor.py`
- Create: `E:\keo-moi-mail\trading-agent\tests\test_curiosity_scheduler.py`
- Create: `E:\keo-moi-mail\trading-agent\tests\test_memory_compactor.py`
- Modify: `dream_cycle.py`, `reflection_agent.py`

## Implementation Steps

1. Query recent events from SQLite.
2. Compute uncertainty scores for setups/regimes/beliefs.
3. Pick one focus with reason and expected learning value.
4. Feed focus into `dream_cycle.py` simulations.
5. Summarize raw events into compact daily memory.
6. Promote repeated lessons into belief ledger candidates.
7. Decay or reject beliefs contradicted by evidence.

## Success Criteria

- Dream cycle receives explicit focus.
- Memory summaries remain small and queryable.
- Repeated lessons create candidate beliefs.
- Contradicted beliefs weaken automatically.

## Tests

- Chooses confusing loss over random hot symbol.
- Chooses under-sampled setup when no losses.
- Chooses contradictory belief when belief evidence is conflicted.
- Compacts event rows into deterministic summary.
- Does not duplicate promoted beliefs.

## Completion Notes

- Added `curiosity_scheduler.py` to select one learning focus per cycle from confusing losses, weak/under-sampled setups, under-sampled regimes, and contradictory beliefs.
- Added `memory_compactor.py` to write compact semantic memory to `state/agent_memory/semantic_memory.json`.
- Repeated lessons are promoted through `belief_ledger.upsert_belief`, so duplicate lessons map to the same belief id instead of creating copies.
- `dream_cycle.py` now embeds and persists the curiosity focus; current runtime focus is visible through `curiosity_scheduler.py --status`.
- `reflection_agent.py` now calls memory compaction after each reflection cycle.
- Verification: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 venv\Scripts\python.exe -m pytest tests -q` -> `103 passed, 3 warnings`.

## Risks

- Risk: compaction loses detail. Mitigation: raw SQLite/JSONL remains source of truth.
- Risk: curiosity overfits rare events. Mitigation: require sample counts in scoring.
