---
title: "Test-To-Memory Learning Loop"
description: "Turn failed exams, weak shadow evidence, low replay coverage, and benchmark results into auditable learning tasks for the agent."
status: in-progress
priority: P0
effort: 8h
issue:
branch: none
tags: [trading-agent, learning, daily-exam, benchmark, self-model, curriculum]
blockedBy: []
blocks: []
relatedPlans:
  - 260624-1226-performance-recovery-sprint
created: 2026-06-25
---

# Test-To-Memory Learning Loop

## Objective

The existing pytest suite protects code safety, but it does not directly teach the trading agent. This plan adds a deterministic bridge that converts runtime exam/test failures into memory, curriculum, and measurable benchmark tasks.

The system remains paper/shadow only.

## Principles

- Test results may create lessons and curriculum.
- Test results may not loosen risk.
- Benchmark scenarios must be deterministic and replayable.
- LLM may later summarize lessons, but deterministic code owns the failure classification.
- Every learning item must cite source evidence.

## Phase 1: Test Result Memory Agent

Create `test_result_memory_agent.py`.

Inputs:

- `daily_exam_latest.json`
- `counterfactual_latest.json`
- `shadow_performance_latest.json`
- `walk_forward_latest.json`
- `promotion_board_latest.json`
- `learning_exam_benchmark_latest.json`

Outputs:

- `test_result_memory_latest.json`
- `test_result_memory.jsonl`
- `test_result_memory_heartbeat.json`
- optional episodes in `episodes.jsonl`

Acceptance criteria:

- Low counterfactual coverage creates a high-priority curriculum item.
- Negative fresh shadow expectancy creates a high-priority curriculum item.
- Running/failed walk-forward creates a validation curriculum item.
- Promotion blockers are recorded as non-live learning constraints.
- Output always has `can_place_live_orders=false`.

## Phase 2: Learning Exam Benchmark

Create `learning_exam_benchmark.py`.

Benchmark scenarios:

- `funding_squeeze_negative_funding_reversal`
- `funding_squeeze_negative_funding_fail`
- `exhaustion_fade_overextended_short`
- `thin_liquidity_no_trade`
- `btc_regime_conflict_no_trade`

Each scenario supplies only current/past context and an expected safe action.

Acceptance criteria:

- Benchmark writes latest/history.
- Each failed scenario produces a lesson and next action.
- Benchmark score is machine-readable.
- It never opens paper/live orders.

## Phase 3: Self-Model And Dashboard Visibility

Self-model should consume `test_result_memory_latest.json`:

- append known gaps
- append curriculum items
- expose benchmark score

Dashboard should show compact test-memory status in the Learning tab.

## Phase 4: Supervision And Verification

Add supervised loop for `test_result_memory_agent.py`.

Verification:

```powershell
venv\Scripts\python.exe -m py_compile test_result_memory_agent.py learning_exam_benchmark.py self_model.py agent_process_supervisor.py agent_status_dashboard.py
venv\Scripts\python.exe -m pytest tests\test_test_result_memory_agent.py tests\test_learning_exam_benchmark.py tests\test_phase_c_memory_system.py tests\test_agent_process_supervisor.py tests\test_agent_status_dashboard.py -q
```

Runtime smoke:

```powershell
venv\Scripts\python.exe learning_exam_benchmark.py --once
venv\Scripts\python.exe test_result_memory_agent.py --once
venv\Scripts\python.exe self_model.py --once
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8090/api/status -TimeoutSec 15
```

## Done

- The agent can say what tests/exams it failed.
- The agent can convert those failures into learning tasks.
- The self-model can see these tasks.
- No live order permission is introduced.

## Progress Log

### 2026-06-25 Phase 1-3 Active

Implemented:

- `learning_exam_benchmark.py`
  - deterministic no-execution market scenario benchmark
  - latest/history/heartbeat outputs
  - default scenarios for funding squeeze, exhaustion fade, liquidity, and BTC regime conflict
- `test_result_memory_agent.py`
  - reads daily exam, counterfactual, shadow, walk-forward, promotion, and benchmark latest files
  - converts weak evidence into lessons, known gaps, curriculum, and episode rows
  - always outputs `can_place_live_orders=false` and `can_loosen_risk=false`
- `self_model.py`
  - consumes `test_result_memory_latest.json` and `learning_exam_benchmark_latest.json`
  - appends known gaps and curriculum into the agent self-model
- `agent_process_supervisor.py`
  - supervises `learning_exam_benchmark` and `test_result_memory_agent`
- `agent_status_dashboard.py`
  - tracks new heartbeats and exposes benchmark/test-memory payloads under `ops`

Runtime smoke:

```powershell
venv\Scripts\python.exe learning_exam_benchmark.py --once
venv\Scripts\python.exe test_result_memory_agent.py --once
venv\Scripts\python.exe self_model.py --once
venv\Scripts\python.exe agent_status_dashboard.py --once
```

Observed:

- benchmark score: `1.0`
- benchmark failed scenarios: `0`
- test-memory lessons: `4`
- high severity lessons: `2`
- known gaps:
  - `counterfactual_coverage_low`
  - `promotion_blocked`
  - `shadow_edge_weak`
  - `walk_forward_not_done`
- self-model consumed `test_result_lessons=4`
- live order permission remained false

Verification:

```powershell
venv\Scripts\python.exe -m py_compile learning_exam_benchmark.py test_result_memory_agent.py self_model.py agent_process_supervisor.py agent_status_dashboard.py
venv\Scripts\python.exe -m pytest tests\test_learning_exam_benchmark.py tests\test_test_result_memory_agent.py tests\test_phase_c_memory_system.py tests\test_agent_process_supervisor.py tests\test_agent_status_dashboard.py tests\test_runtime_integration_batch.py tests\test_daily_exam_agent.py -q
```

Focused result: `80 passed`.

Full suite result:

```powershell
venv\Scripts\python.exe -m pytest -q
```

Result: `582 passed, 1 skipped, 11 warnings, 75 subtests passed`.
