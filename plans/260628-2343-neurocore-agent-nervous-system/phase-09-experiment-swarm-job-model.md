# Phase 09: Experiment Swarm Job Model

## Overview

Turn replays/backtests into high-throughput, idempotent experiment jobs.

## Related Code

- `experiment_registry.py`
- `agent_work_queue.py`
- `counterfactual_replay_agent.py`
- `backtest_harness.py`

## Job Schema

```json
{
  "experiment_id": "...",
  "experiment_family_id": "...",
  "schema_version": "experiment_job.v1",
  "created_at": "UTC ISO",
  "source_event_ids": [],
  "provenance_id": "...",
  "variant_hash": "...",
  "data_window_hash": "...",
  "config_hash": "...",
  "setup_id": "...",
  "hypothesis": "...",
  "alpha_budget": 0.05,
  "preregistered_pass_rule": "...",
  "status": "queued|running|passed|failed|retired"
}
```

Every job also records `actor_id`, `client_id`, `capability_id`, `parent_call_id`, `chain_depth`, `max_effect`, `root_budget_id`, resource budget, timeout, artifact quota, priority budget, estimated/actual cost, API calls, LLM tokens, and budget reservation id.

`setup_id` in jobs is not free text. It must reference `setup_contract_id`, `setup_version`, and `setup_contract_hash`.

## Implementation Steps

1. Define experiment job and variant spec.
2. Use queue workers with idempotent claim/result.
3. Add priority: current weak setup, high-confidence hypothesis, daily exam gaps.
4. Add result store indexed by experiment/variant/window.
5. Add throughput metrics: jobs/day, fail rate, coverage.
6. Track all proposed, failed, abandoned, and unselected hypotheses to prevent selection bias.
7. Define experiment family and alpha budget before variants run.
8. Add worker/resource caps: max parallelism by CPU/RAM/IO, per-job timeout, artifact size cap, low-priority scheduling, and pause on bus lag/disk/WAL thresholds.
9. Add per-actor/token/family quotas and validation-budget locks so job spam cannot starve core daemons or overfit one family.
10. Add result-store indexes, materialized summaries, and p95 query/insert budgets.
11. Add exploration/exploitation bands for experiments: exploit, explore, shadow-only, cold-start; each has loss/trade budget, priority cap, and propensity logging.
12. Require preflight resource/cost reservation before queue admission. Replace raw jobs/day optimization with useful passed experiments within spend/resource cap.
13. Enforce root-scoped budget: max descendants, max chain depth, cumulative cost/quota, and kill switch at queue admission.
14. Add CI/runtime resource guardrails for experiment tests: max test duration, per-job timeout, memory ceiling, artifact size, DB/tmp cleanup, and parallel worker cap.

## Tests

- Duplicate variant not re-run unless data window changes.
- Failed worker retries then DLQs.
- Same experiment can have multiple variants without collision.
- Queue survives restart.
- Best variant cannot pass unless family-level correction passes.
- Abandoned hypothesis remains in registry.
- Experiment burst cannot push core bus lag beyond SLA.
- Over-quota actor/family is rejected with signed audit event.
- Oversized/timeout job is killed and does not leave partial result.
- Job with unknown setup contract hash is rejected.
- Explore/cold-start job cannot consume exploit budget or normal risk.
- Job without cost reservation is rejected.
- Descendant job chain stops when root budget or max chain depth is exceeded.
- CI/test resource cap kills oversized experiment jobs and records clean partial-artifact cleanup.

## Done Gate

Experiment Swarm runs batches without JSONL full-scan bottleneck.

## Audit Questions

- Are we testing many real variants or repeating the same one?
- Can a crashed worker corrupt results?
- Can swarm throughput starve observers/paper execution?
- Who caused this job and what maximum effect can it have?
- Is this job useful within spend cap or only inflating jobs/day?
