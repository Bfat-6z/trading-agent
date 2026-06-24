# Phase 03: Hypothesis Engine

## Context Links

- [Plan](./plan.md)
- Depends on [Belief Ledger](./phase-01-belief-ledger.md) and [Setup Skill Library](./phase-02-setup-skill-library.md)
- Existing: `market_learner.py`, `dream_cycle.py`

## Overview

Priority: P0. Status: complete. Generate falsifiable hypotheses from market context, beliefs, and setup stats.

## Requirements

- Create `hypothesis_engine.py`.
- Output hypotheses to `state/agent_memory/hypotheses_latest.json` and JSONL history.
- Hypothesis schema: `hypothesis_id`, `statement`, `setup_id`, `regime`, `symbols`, `prediction`, `invalidation`, `metrics`, `confidence_prior`, `status`.
- No hypothesis can affect execution unless status is `testable` or `validated`.

## Related Code Files

- Create: `E:\keo-moi-mail\trading-agent\hypothesis_engine.py`
- Create: `E:\keo-moi-mail\trading-agent\tests\test_hypothesis_engine.py`
- Modify later: `dream_cycle.py`, `cognitive_supervisor.py`

## Implementation Steps

1. Read latest market model, setup library, belief ledger, and dream results.
2. Build deterministic hypothesis templates for common regimes.
3. Generate top N hypotheses ranked by expected information gain.
4. Attach metric definitions: win-rate, expectancy, TP-before-SL, MAE, MFE, slippage proxy.
5. Attach invalidation: data stale, contradictory regime, poor setup history.
6. Persist latest and append history.

## Success Criteria

- Hypotheses are deterministic for same inputs.
- Each hypothesis is falsifiable.
- Each has metric and invalidation.
- Missing inputs degrade to observe-only hypotheses.

## Completion Notes

- Implemented `hypothesis_engine.py`.
- Implemented `tests/test_hypothesis_engine.py`.
- Added manual thesis ingestion for chart/operator ideas without placing trades.
- Encoded BTC TradingView thesis as manual hypothesis: entry 63683, stop 60728, targets 67255/74427/78059.
- Verified with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 venv\Scripts\python.exe -m pytest tests -q`.
- Result: 83 passed, 3 warnings.

## Tests

- Risk-on snapshot creates continuation hypothesis.
- Crowded funding creates anti-chase/fade hypothesis.
- Missing setup library still returns safe empty list.
- Duplicate hypotheses dedupe by id.

## Risks

- Risk: hypothesis spam. Mitigation: rank and cap top 10.
- Risk: LLM hallucinated rules. Mitigation: deterministic templates first.
