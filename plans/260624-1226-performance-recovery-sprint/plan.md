---
title: "Performance Recovery Sprint For Paper Learning Agent"
description: "Narrow, evidence-driven plan to turn the current paper/shadow agent from log collection into measurable trading improvement."
status: in-progress
priority: P0
effort: 45h
issue:
branch: none
tags: [trading-agent, paper-trading, performance, counterfactual, skill-forge, shadow, memory, dashboard]
blockedBy: []
blocks: []
relatedPlans:
  - 260621-1650-autonomous-paper-learning-masterplan
  - 260620-1506-self-thinking-agent-development
  - 260621-1112-shadow-performance-loop
created: 2026-06-24
---

# Performance Recovery Sprint For Paper Learning Agent

## Overview

This is the serious performance plan after the June 24 audit.

The old masterplan is directionally correct but too wide. The current problem is not "add more agents". The current problem is that the agent produces a lot of data but has not yet converted that data into validated edge.

This sprint focuses only on the loop that can improve performance:

```text
clean events -> realistic paper/shadow outcomes -> counterfactual learning
-> skill/risk changes with evidence -> walk-forward validation
-> paper allocation changes -> dashboard proof
```

No live trading changes are included. The goal is to improve paper/shadow performance and prove it with metrics.

## Audit Baseline

Measured on 2026-06-24:

| Area | Current evidence | Read |
| --- | ---: | --- |
| Paper total | 426 trades, net -31.320694 | Not profitable overall |
| Paper 2026-06-24 | 43 trades, WR 62.79%, net +7.529968 | Short-term improvement, not proven |
| Shadow latest | 280 closes, WR 11.43%, PF 0.068, net -0.830618 | Very weak edge |
| Daily exam | avg around 59, grade D | Not ready |
| Self-improvement score | 0.391 -> 0.3728 latest | Not improving |
| Episodes | 432 | Data exists |
| Post-trade reviews | 432 | Review exists but too shallow |
| Counterfactual replays | 0 | Critical missing loop |
| Promoted memories | 0 | No durable learning yet |
| Skill forge | 1 pending patch, 0 promoted | Skill evolution not functioning |
| Beliefs | 42 candidate, 0 promoted | Memory stuck at candidate stage |
| Promotion board | failed: paper trades, shadow closes, daily exam, trial days | Correctly blocked |

## Goal

Within this sprint, make the agent measurably better at paper trading by adding the missing feedback loops and preventing false progress.

Target outcomes:

| Metric | Target for sprint completion |
| --- | --- |
| Counterfactual coverage | >= 80% of closed paper trades and skipped candidates with candle coverage |
| Post-trade review quality | includes MFE/MAE, process score, reason class, fee/funding/slippage impact |
| Skill patch flow | at least one setup patch accepted to paper-only, or explicitly rejected with evidence |
| Shadow health | shadow evaluator no longer stuck behind old 418 gaps; fresh shadow closes accumulate |
| Paper allocation | sizes/leverage respond to setup expectancy and drawdown, not fixed defaults |
| Dashboard | shows "is it improving?" with pre/post reset and rolling windows |
| Promotion | remains blocked unless gates pass; no fake readiness |

## Non-Negotiables

- Paper/shadow only.
- No live order enablement.
- LLM can critique, propose, and summarize; deterministic gates decide.
- No metric can ignore fees, funding, slippage, open margin, or unresolved trades.
- No skill/memory promotion from one good streak.
- No "80% winrate" target without positive expectancy and bounded drawdown.
- Every performance claim must name the dataset window and reset boundary.

## Sprint Strategy

Max performance does not mean maximum trade frequency. It means maximum learning quality per trade.

The agent currently has three failure modes:

1. It trades or shadows weak setups too broadly.
2. It reviews outcomes but does not replay alternatives.
3. It stores candidate beliefs but does not promote/retire rules.

So the sprint attacks performance in this order:

1. Fix the truth layer.
2. Add counterfactual learning.
3. Convert learning into setup/risk changes.
4. Validate out-of-sample.
5. Only then tune allocation.

## Phase 1: Truth Layer And Performance Accounting

### Objective

Make performance numbers trustworthy before optimizing anything.

### Scope

- Split paper stats into:
  - all-time historical
  - current reset window
  - rolling 10 / 25 / 50 / 100 closes
  - open-position exposure
  - realized PnL
  - unrealized PnL if mark price exists
  - available cash
  - margin locked
- Add reset boundary awareness.
- Add account sanity checks:
  - closed trades in account must match canonical close events for the current account id.
  - equity display must not treat open margin as lost PnL.
  - fees/funding/slippage must be shown separately.
- Quarantine invalid trade rows from learning, not only from dashboard.

### Files Likely Touched

- `paper_portfolio_manager.py`
- `paper_execution_lifecycle_loop.py`
- `agent_status_dashboard.py`
- `trade_lifecycle_validator.py` if present, otherwise create it
- `tests/test_paper_execution_lifecycle_loop.py`
- `tests/test_agent_status_dashboard.py`

### Acceptance Criteria

- Dashboard can answer: "Since current reset, is the agent up or down?"
- Historical paper loss and current reset performance are not mixed.
- `promotion_board_latest.json` uses validated counts only.
- Test covers reset boundary, open margin, realized PnL, fees, and funding.

## Phase 2: Counterfactual Replay Engine

### Objective

Stop learning only from what actually happened. Learn from alternatives.

### Scope

For every closed paper trade and every skipped/blocked candidate with candle coverage, simulate:

- actual entry
- entry +1 candle
- entry -1 candle
- SL 0.5x / 1.0x / 1.5x
- TP 0.5R / 1.0R / 1.5R
- time-based exit
- lower leverage
- current leverage
- no trade

Output classifications:

- `entry_too_early`
- `entry_too_late`
- `sl_too_tight`
- `sl_too_wide`
- `tp_too_far`
- `tp_too_close`
- `risk_gate_saved_loss`
- `risk_gate_missed_winner`
- `setup_has_no_edge`
- `setup_edge_only_in_regime`

### Files

- Create `counterfactual_replay_agent.py`
- Create `counterfactual_replay.py` for pure deterministic functions
- Create `state/agent_memory/counterfactual_replays.jsonl`
- Create `state/agent_memory/counterfactual_latest.json`
- Add supervisor heartbeat if looped
- Add tests

### Acceptance Criteria

- `self_model.counterfactual_replays` becomes nonzero.
- >= 80% of eligible closed paper trades have replay rows.
- Replay never uses future/latest data outside the trade window.
- Replays include fee and slippage assumptions.
- Counterfactual outputs do not directly loosen live or paper risk.

## Phase 3: Post-Trade Review Upgrade

### Objective

Make post-trade learning useful enough to drive skill changes.

### Scope

Each review must include:

- MFE
- MAE
- R multiple
- fee impact
- funding impact
- slippage impact
- process quality score
- outcome quality score
- setup validity score
- market regime at entry
- news risk at entry
- counterfactual summary
- primary failure reason

Current classes are too coarse: `good_win`, `bad_loss`, `tp_too_far`.

Add more exact classes:

- `bad_win`
- `good_loss`
- `stop_too_tight`
- `late_entry`
- `early_entry`
- `news_conflict`
- `spread_slippage_issue`
- `regime_mismatch`
- `crowded_trade`
- `thin_liquidity`

### Files

- `post_trade_learning_agent.py`
- `post_trade_reviews.jsonl`
- tests for review classification

### Acceptance Criteria

- Every new close has a review with MFE/MAE/R where candle coverage exists.
- Review coverage is visible on dashboard.
- A winning trade that violated process is not rewarded blindly.
- A losing trade with good process does not kill a setup blindly.

## Phase 4: Skill Forge That Actually Changes Behavior

### Objective

Turn repeated evidence into paper-only setup changes.

### Scope

Implement a skill patch lifecycle:

```text
proposed -> schema_valid -> evidence_checked -> paper_only_applied -> monitored -> promoted_or_reverted
```

Patch types:

- regime filter
- SL/TP template
- entry timing rule
- symbol blacklist/graylist
- setup retirement
- setup split by regime
- leverage cap by setup
- min score adjustment by setup

Rules:

- Negative expectancy skill cannot promote.
- Positive streak cannot promote without enough sample.
- A patch must include evidence ids:
  - post-trade review ids
  - counterfactual ids
  - shadow ids when available
- Applied patches are paper-only.
- Every patch has rollback criteria.

### Files

- `skill_forge_agent.py`
- `setup_skill_library.py`
- `state/agent_memory/skill_patches_pending.jsonl`
- `state/agent_memory/skill_patches_applied.jsonl`
- `state/agent_memory/skill_patch_reviews.jsonl`
- dashboard skill panel

### Acceptance Criteria

- Current pending patch from 2026-06-22 is reviewed, accepted or rejected.
- At least one patch reaches `paper_only_applied`, or all are rejected with clear evidence.
- Paper brain reads only accepted paper-only patches.
- Dashboard shows pending/applied/reverted patches.

## Phase 5: Shadow Repair And Fresh Would-Trade Evaluation

### Objective

Make shadow useful again. Current shadow result is too old/too damaged by API errors.

### Scope

- Separate old shadow batch from fresh shadow batch.
- Add candle cache/backoff to avoid repeated 418/429.
- Start fresh shadow window from 2026-06-24 onward.
- Report:
  - fresh shadow closes
  - unresolved
  - API errors
  - expectancy after fees/slippage
  - by setup/symbol/regime
- Feed fresh shadow into skill forge as tightening evidence only until stable.

### Files

- `shadow_trade_evaluator.py`
- `shadow_trade_logger.py`
- `state/agent_memory/shadow_performance_latest.json`
- dashboard shadow panel

### Acceptance Criteria

- Fresh shadow window is shown separately from old 2026-06-20 batch.
- API error count no longer dominates latest metrics.
- Shadow performance can block weak skills but cannot promote live.

## Phase 6: Setup Ranker And Paper Capital Allocation

### Objective

Stop trading every candidate equally. Allocate paper risk toward the best validated setup/symbol/regime buckets.

### Inputs

- rolling paper expectancy
- counterfactual expectancy
- shadow fresh expectancy
- post-trade classifications
- drawdown
- market regime
- news risk
- liquidity/spread

### Output

For each candidate:

```json
{
  "allocation_decision": "skip|tiny|normal|reduced",
  "margin": "7.00",
  "leverage": "5",
  "reason": "setup positive today but all-time weak, use reduced risk",
  "evidence_ids": []
}
```

### Rules

- Downsize after drawdown.
- Downsize when shadow disagrees.
- Downsize when counterfactual says TP/SL parameters are unstable.
- Upsize only in paper after rolling expectancy and drawdown gates pass.
- Never use fixed leverage as default if symbol volatility is high.

### Files

- `setup_ranker.py`
- `paper_portfolio_manager.py`
- `autonomous_paper_trading_loop.py`
- `paper_trading_brain.py`
- `capital_allocation_latest.json`

### Acceptance Criteria

- Allocation changes are explainable.
- Risk per trade is tied to evidence, not hardcoded only.
- No bucket with negative expectancy receives normal allocation.

## Phase 7: Walk-Forward Validation And Anti-Overfit

### Objective

Prevent the agent from thinking one lucky window means a skill is good.

### Scope

Create experiment windows:

- train window: where a patch was discovered
- test window: future trades after patch
- holdout: shadow or paper window not used for patch proposal

Metrics:

- expectancy after fees
- profit factor
- drawdown
- winrate
- average win/loss
- sample size
- regime coverage

### Files

- `experiment_registry.py`
- `walk_forward_validator.py`
- `state/agent_memory/experiments.jsonl`
- dashboard experiment panel

### Acceptance Criteria

- Skill patches cannot be marked successful without future-window evidence.
- Dashboard shows which experiments are running, passed, failed, retired.
- Promotion board consumes walk-forward status.

## Phase 8: Performance Dashboard And Daily Exam Rewrite

### Objective

Make the UI and exam answer one question clearly: "Is it actually improving?"

### Dashboard Sections

- Current reset paper account
- Historical paper account
- Rolling performance windows
- Counterfactual coverage
- Post-trade review quality
- Skill patch lifecycle
- Fresh shadow window
- Setup ranker
- Experiment status
- Promotion blockers

### Daily Exam Changes

Daily exam should grade:

- Did paper expectancy improve?
- Did shadow fresh expectancy improve?
- Did counterfactual coverage improve?
- Did a weak setup get reduced/retired?
- Did a patch pass out-of-sample?
- Did the agent violate DONT_DO?
- Did the dashboard show stale or dirty data?

### Acceptance Criteria

- Daily exam score can rise only if data quality and performance improve.
- Grade D cannot pass as "good enough" just because exam_score is 100.
- Dashboard shows exact reason the agent is not ready.

## Implementation Order

Do this exact order:

1. Phase 1: Truth Layer
2. Phase 2: Counterfactual Replay
3. Phase 3: Post-Trade Review Upgrade
4. Phase 5: Shadow Repair
5. Phase 4: Skill Forge
6. Phase 6: Setup Ranker / Allocation
7. Phase 7: Walk-Forward
8. Phase 8: Dashboard / Daily Exam

Reason for this order:

- Skill forge before counterfactual would overfit.
- Allocation before clean accounting would optimize fake numbers.
- Dashboard before new metrics would only make bad data prettier.

## Progress Log

### 2026-06-24 Phase 1 Partial Complete

Implemented reset-aware paper performance accounting:

- `paper_report` top-level now reports the current `paper_account.created_at` reset window when available.
- `paper_report.historical` keeps all historical closes separate.
- `paper_report.account_alignment` compares account counters with validated current close events.
- `promotion_board.evaluate_from_state()` now uses validated paper closes after reset and exposes:
  - `account_paper_trades`
  - `validated_paper_trades`
  - gated `paper_trades`
- Dashboard API smoke after restart:
  - `window=current_reset`
  - current reset closes `9`
  - current reset net `+1.67442358`
  - historical closes `237`
  - historical net `-20.066156`
  - promotion `paper_trades=9`

Verification:

```powershell
venv\Scripts\python.exe -m py_compile agent_status_dashboard.py promotion_board.py
venv\Scripts\python.exe -m pytest tests\test_agent_status_dashboard.py tests\test_phase_f_autonomy_promotion.py -q -vv
```

Result: `27 passed`.

Remaining Phase 1 work:

- Add explicit unrealized PnL mark-to-market if reliable mark prices are available.
- Add dashboard labels for current reset vs historical instead of relying only on JSON fields.
- Consider adding an account id/reset id to new paper accounts so future reset boundaries are even cleaner than timestamp filtering.

### 2026-06-24 Phase 2 Replay Engine Active

Implemented the counterfactual replay service:

- `counterfactual_replay_agent.py` now has `--once`, `--status`, and daemon mode with PID/heartbeat/stop-file support.
- Supervisor now manages `counterfactual_replay_agent` every 300 seconds.
- Dashboard heartbeat list now includes `counterfactual_replay_agent`.
- Replay scans valid `paper_close` events from `state/agent_memory/paper_trades.jsonl`.
- Candle sourcing is explicit:
  - embedded candles
  - `candle_cache_id` / `replay_candle_cache_id`
  - nested position candle cache id
  - `mark_only_snapshot` fallback as one candle
  - missing source
- Missing candle coverage writes `status="unresolved"` with `reason="insufficient_candle_coverage"` instead of fake PnL.
- Complete replays include SL/TP grid, lower leverage, and `entry_plus_1` when candle coverage exists.
- Summary now reports `complete_count`, `unresolved_count`, and `coverage_pct`.

Runtime smoke:

- Supervisor reloaded and started `counterfactual_replay_agent`.
- Dashboard API returned `200`.
- Dashboard heartbeat showed `counterfactual_replay_agent` as `ok` and running.
- Current replay state after daemon start:
  - `replay_count=100`
  - `complete_count=0`
  - `unresolved_count=100`
  - `coverage_pct=0.0`

Verification:

```powershell
venv\Scripts\python.exe -m py_compile counterfactual_replay_agent.py agent_process_supervisor.py agent_status_dashboard.py
venv\Scripts\python.exe -m pytest tests\test_phase_b_objective_learning.py tests\test_agent_process_supervisor.py tests\test_agent_status_dashboard.py -q
```

Result: `58 passed`.

Important Phase 2 audit result:

- Replay engine is active, but current paper close events are mostly `data_quality="mark_only_snapshot"`.
- This means counterfactual learning cannot yet compute alternate SL/TP/entry PnL for those trades.
- The next performance-critical fix is to attach a replayable candle cache/window to new paper opens/closes, then backfill where possible.

### 2026-06-24 Phase 2 Audit Fixes Complete

Independent audit found several replay correctness gaps. Fixed before moving to Phase 3:

- Counterfactual replay no longer mutates `state/paper_orders.jsonl`.
  - `paper_execution_simulator.simulate_round_trip(..., append_order=False)` is used by replay.
- Unresolved replays are no longer terminal.
  - Only `status="complete"` blocks future retries.
  - A previously unresolved signal can complete later if a candle cache appears.
- Malformed replay signals no longer crash-loop the daemon.
  - They append `status="unresolved"`, `reason="invalid_replay_signal"`.
- Replay now windows embedded/cache candles by `open_ts` / `close_ts` when available.
  - Wrong-window caches fail coverage instead of being treated as complete.
- Batch replay now ingests blocked paper brain decisions from `paper_trading_brain_history.jsonl`, not only closed paper trades.
- Paper lifecycle now accumulates observed mark snapshots on open positions.
  - New closes with at least 3 observed snapshots write a replayable `candle_cache_id`.
  - The cache is explicitly tagged as `mark_sequence`, not full OHLCV.
- Dashboard learning tab now renders counterfactual replay health:
  - total replay count
  - complete/unresolved count
  - coverage percentage
  - recent replay rows
  - conclusion counts
- Paper runtime aggregate now includes `counterfactual_replay_agent` and `promotion_evaluator_loop`.

Verification:

```powershell
venv\Scripts\python.exe -m py_compile counterfactual_replay_agent.py paper_execution_simulator.py paper_execution_lifecycle_loop.py agent_process_supervisor.py agent_status_dashboard.py
venv\Scripts\python.exe -m pytest tests\test_phase_b_objective_learning.py tests\test_paper_execution_lifecycle_loop.py tests\test_agent_process_supervisor.py tests\test_agent_status_dashboard.py -q
```

Result: `67 passed`.

Full suite smoke:

```powershell
venv\Scripts\python.exe -m pytest -q
```

Pytest printed `539 passed, 1 skipped, 11 warnings, 75 subtests passed`, but the shell command hit the 180s wrapper timeout immediately after the summary. Treat as a pass with timeout caveat.

Runtime smoke:

- Reloaded `counterfactual_replay_agent`, `paper_execution_lifecycle_loop`, and dashboard via hidden `pythonw`.
- Dashboard API returned `200`.
- Supervisor status:
  - no duplicate supervised agents
  - `counterfactual_replay_agent` running
  - `paper_execution_lifecycle_loop` running
  - dashboard running
- Dashboard API after reload:
  - `paper_runtime=running`
  - tracked loops include `counterfactual_replay_agent` and `promotion_evaluator_loop`
  - `replay_count=104`
  - `complete_count=0`
  - `unresolved_count=104`
  - `coverage_pct=0.0`

Remaining Phase 2 gap:

- Existing historical paper closes are still mostly mark-only and cannot be fully replayed.
- New closes should begin producing complete replays only after each position has at least 3 observed mark snapshots before close.
- A later backfill pass can reconstruct candle caches from external klines, but that should be a separate data-source/backoff task so it does not fake coverage.

### 2026-06-24 Phase 3 Partial Complete

Upgraded post-trade review quality:

- Reviews now include `costs`:
  - entry fee
  - exit fee
  - total fees
  - funding payment
  - slippage
  - gross / net before funding / net
  - fee-to-gross and fee-to-margin ratios
- Reviews now include `primary_failure_reason`.
- Reviews now include `setup_validity_score`.
- Reviews attach latest matching counterfactual replay evidence when available.
- Reviews include `market_regime` and `data_quality`.
- Flags now include `fee_drag_high` and `funding_drag`.
- Summary now includes:
  - `by_primary_failure_reason`
  - average process/outcome/setup-validity scores
  - sample counts so missing historical fields do not become fake zeroes.

Verification:

```powershell
venv\Scripts\python.exe -m py_compile post_trade_learning_agent.py paper_execution_lifecycle_loop.py counterfactual_replay_agent.py agent_status_dashboard.py
venv\Scripts\python.exe -m pytest tests\test_phase_b_objective_learning.py tests\test_paper_execution_lifecycle_loop.py tests\test_agent_status_dashboard.py -q
venv\Scripts\python.exe -m pytest tests\test_phase_b_objective_learning.py tests\test_paper_execution_lifecycle_loop.py tests\test_agent_process_supervisor.py tests\test_agent_status_dashboard.py -q
```

Results:

- `47 passed`
- `69 passed`
- focused post-trade refresh after null-average fix: `25 passed`

Runtime:

- Reloaded `paper_execution_lifecycle_loop` so new closes use upgraded review schema.
- Refreshed `post_trade_learning_latest.json`.
- Current historical reviews still have `primary_failure_reason=unknown` because they were created before this schema.
- New reviews from this point forward will contain the richer fields.

## Quality Gates After Each Phase

Every phase must pass:

```powershell
venv\Scripts\python.exe -m py_compile <changed_files>
venv\Scripts\python.exe -m pytest <targeted_tests> -q
venv\Scripts\python.exe -m pytest tests\test_agent_process_supervisor.py tests\test_agent_status_dashboard.py -q
```

Runtime smoke where applicable:

```powershell
venv\Scripts\python.exe agent_process_supervisor.py --status
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8090/api/status -TimeoutSec 5
```

No phase is complete if dashboard fails, heartbeat is stale, or test data cannot prove the new metric.

## Stop Conditions

Stop and re-audit if any of these happen:

- paper net drops sharply after an allocation patch
- shadow fresh window remains unusable because of API errors
- counterfactual coverage stays below 50% after implementation
- skill forge promotes without evidence ids
- daily exam grade rises while paper/shadow metrics are still worsening
- dashboard mixes old reset and current reset again
- any code path enables live order permission

## Definition Of Done

This sprint is done only when:

- Counterfactual replay is active and visible.
- Post-trade reviews are materially more informative than win/loss labels.
- Skill forge has accepted/rejected patches based on evidence.
- Fresh shadow performance is separated from old broken shadow batch.
- Paper allocation is evidence-weighted.
- Daily exam grades actual improvement, not task completion.
- Dashboard makes performance trend obvious.
- Promotion remains blocked until objective gates pass.

## Expected Result

The realistic expected result is not immediate profit. The expected result is:

1. We can tell which setups are bad.
2. We can tell why trades lose.
3. We can tell whether SL/TP/timing is the issue.
4. We can reduce exposure to bad buckets.
5. We can promote only paper skills that survive future data.
6. We can prove whether the agent is improving day by day.

If this sprint works, the next target is a 7-day paper trial with:

- positive expectancy after fees
- profit factor above 1.15 first, then 1.25
- drawdown below 15%
- daily exam above 70 first, then 80
- counterfactual coverage above 80%
- fresh shadow no longer strongly negative
