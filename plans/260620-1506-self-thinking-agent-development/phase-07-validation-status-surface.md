# Phase 07: Validation And Status Surface

## Context Links

- [Plan](./plan.md)
- Depends on phases 01-06
- Existing: tests, `event_store.py`, state heartbeats

## Overview

Priority: P1. Status: partial. Make the cognition loop auditable and prevent fake win-rate confidence.

## Requirements

- Create `validation_lab.py` for paper/replay metrics by setup/regime.
- Create `agent_status_dashboard.py` as CLI/markdown status first.
- Metrics: closed paper count, win-rate, net, expectancy, setup/regime breakdown, critic block counts, stale-data alerts.
- Do not build a web dashboard yet unless CLI status is insufficient.

## Related Code Files

- Create: `E:\keo-moi-mail\trading-agent\validation_lab.py`
- Create: `E:\keo-moi-mail\trading-agent\agent_status_dashboard.py`
- Create: `E:\keo-moi-mail\trading-agent\tests\test_validation_lab.py`
- Create: `E:\keo-moi-mail\trading-agent\tests\test_agent_status_dashboard.py`

## Implementation Steps

1. Read paper close events from SQLite.
2. Segment results by setup id, hypothesis id, regime, side, symbol.
3. Compute sample-size-aware metrics.
4. Add warning if global win-rate hides poor setup/regime segment.
5. Render markdown status file: `state/agent_status_latest.md`.
6. Include heartbeats and stale file warnings.
7. Include live gate status but do not enable live.

## Success Criteria

- User can see what the agent is thinking and why it blocks.
- Win-rate is segmented, not one misleading global number.
- Stale observers are obvious.
- Tests pass across empty and populated event stores.

## Partial Completion Notes

- Implemented `agent_status_dashboard.py` as a single-page read-only UI.
- Added `tests/test_agent_status_dashboard.py`.
- Dashboard aggregates execution bias, market regime, dream risk, paper stats, beliefs, setup skills, heartbeats, and logs.
- Started local dashboard at `http://127.0.0.1:8090/` without auto-opening browser tabs.
- Verified HTTP `/` and `/api/status` endpoints.
- Full suite result after this change: 77 passed, 3 warnings.
- Remaining from this phase: `validation_lab.py` and full live-readiness metrics integration.

## Tests

- Empty store status renders safely.
- Mixed paper outcomes segment correctly.
- Stale heartbeat warning appears.
- Live gate remains closed when requirements not met.

## Risks

- Risk: dashboard becomes another source of truth. Mitigation: read-only from state/SQLite.
- Risk: metric misuse. Mitigation: show sample counts beside every win-rate.
