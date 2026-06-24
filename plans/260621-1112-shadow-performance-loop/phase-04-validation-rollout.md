# Phase 04: Validation And Rollout

## Context Links

- [Plan](./plan.md)
- Existing test command: `venv\Scripts\python.exe -m pytest tests -q`
- Existing dashboard: `http://127.0.0.1:8090/`

## Overview

Priority: P1.
Status: Complete.

Validate the evaluator, run it safely on current data, and document the first real shadow performance result.

## Requirements

- Run full tests.
- Run evaluator in dry-run before writing close records.
- Run evaluator with limited batch first.
- Generate report and verify dashboard status once output exists.
- Back up any existing shadow close/performance files before first real write.
- Confirm the evaluator imports no live trading modules.
- Do not stop or modify user-owned `unified_monitor.py`.

## Validation Commands

```powershell
venv\Scripts\python.exe -m pytest tests -q
venv\Scripts\python.exe -c "import shadow_trade_evaluator as s; print('loaded', s.__name__)"
venv\Scripts\python.exe shadow_trade_evaluator.py --dry-run --max-trades 20
venv\Scripts\python.exe shadow_trade_evaluator.py --max-trades 50 --fee-rate 0.0005 --slippage-bps 2 --max-hold-seconds 180 --ambiguity-policy sl_first
venv\Scripts\python.exe agent_status_dashboard.py --once
```

If first 50 succeed:

```powershell
venv\Scripts\python.exe shadow_trade_evaluator.py --max-age-hours 72 --fee-rate 0.0005 --slippage-bps 2 --max-hold-seconds 180 --ambiguity-policy sl_first
```

## Rollout Steps

1. Confirm tests pass.
2. Confirm import graph does not pull Binance signed client or live order helpers.
3. Back up old output files if they exist.
4. Confirm dry-run counts open/evaluable/skip rows.
5. Write limited close batch.
6. Re-run same limited batch and verify duplicate count is zero.
7. Inspect `shadow_performance_latest.json`.
8. Verify dashboard JSON includes shadow panel data.
9. Generate and summarize markdown report.
10. Decide whether scheduler/supervisor should run evaluator periodically in a later plan.

## Todo List

- [ ] Full test suite passes.
- [ ] Dry-run works.
- [ ] Limited batch works.
- [ ] Idempotent rerun verified.
- [ ] Backup/rollback path verified.
- [ ] Dashboard status verifies output.
- [ ] First shadow performance report created.
- [ ] Follow-up tasks listed for replay/walk-forward if metrics justify it.

## Success Criteria

- No live order code path is touched.
- Existing monitor remains running if it was running.
- User can see first evidence-backed answer: which shadow setups would have won/lost.
- If data/API fails, failure is explicit and non-destructive.

## Risk Assessment

| Risk | Mitigation |
| --- | --- |
| Network/API failure | Dry-run and skip rows with clear error counts. |
| Large run slow | Use batch limit and max-age filters. |
| Existing tests reveal unrelated failures | Report clearly; do not hide failures. |
| Misleading first sample | Mark low sample count and do not promote until thresholds pass. |
| Bad output write | Back up existing outputs and use atomic writes for latest JSON. |
| Live-code import regression | Explicit import-graph check and tests. |

## Security Considerations

- Market data endpoints only.
- No signed requests.
- No account data.
- No write outside `state/agent_memory` and `plans/reports`.

## Completion Notes

- Targeted tests passed: `tests/test_shadow_trade_evaluator.py` and `tests/test_agent_status_dashboard.py`.
- Full suite passed: `160 passed, 3 warnings`.
- Import safety check passed; evaluator imports no live order client/helper.
- Dry-run and limited batch succeeded.
- Idempotent rerun verified: same 50 rows produced `new_rows=0`, `duplicate_rows=50`.
- Full 72h run completed non-destructively with explicit data-quality failure for Binance HTTP 418 rate-limit.
