# Phase 05: Inner Critic Gate

## Context Links

- [Plan](./plan.md)
- Depends on phases 01-04
- Existing: `scalp_autotrader.py`, `dream_cycle.py`, `event_store.py`

## Overview

Priority: P1. Add a pre-paper-entry critic that blocks weak or unexplained trades.

Status: Complete.

## Requirements

- Create `inner_critic.py`.
- Critic verdicts: `allow_paper`, `tighten`, `block`.
- Critic must return reasons and references: setup id, hypothesis id, stale data status, risk flags.
- Wire critic before PAPER entry in `scalp_autotrader.py`.

## Related Code Files

- Create: `E:\keo-moi-mail\trading-agent\inner_critic.py`
- Create: `E:\keo-moi-mail\trading-agent\tests\test_inner_critic.py`
- Modify: `E:\keo-moi-mail\trading-agent\scalp_autotrader.py`
- Modify: `E:\keo-moi-mail\trading-agent\tests\test_scalp_autotrader.py`

## Implementation Steps

1. Define `CriticInput` and `CriticVerdict` dictionaries or dataclasses.
2. Check data freshness: market, derivatives when available, dream, belief ledger.
3. Check setup match and hypothesis support.
4. Check memory bias: sleep, blocked symbols/sides, min score.
5. Check recent paper losses and poor setup expectancy.
6. Return `block` if any hard invalidation fires.
7. Append `inner_critic` event to event store.
8. Add critic record to paper open event.

## Success Criteria

- Paper entry is impossible without critic verdict.
- Blocked entries log exact reasons.
- Existing tests still pass.
- Live behavior remains unchanged or stricter.

## Tests

- Allows clean high-score paper signal with setup and supporting hypothesis.
- Blocks stale market data when timestamp is older than the configured freshness window.
- Blocks unsupported setup.
- Blocks symbol/side during memory sleep/block.
- Tightens paper entry when setup matches but no supporting hypothesis exists.
- Verifies `scalp_autotrader.open_paper` cannot create a paper position without a critic verdict.

## Completion Notes

- Added `inner_critic.py` with `allow_paper`, `tighten`, and `block` verdicts.
- Wired `scalp_autotrader.open_paper` so blocked critic verdicts log `inner_critic_block` and do not open a paper position.
- Added critic payload to `paper_open` logs for explainability.
- Added unit and integration coverage in `tests/test_inner_critic.py` and `tests/test_scalp_autotrader.py`.
- Verification: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 venv\Scripts\python.exe -m pytest tests -q` -> `98 passed, 3 warnings`.

## Risks

- Risk: critic blocks everything. Mitigation: log counters and tune only in PAPER.
- Risk: circular reasoning from same signal. Mitigation: require independent evidence fields.
