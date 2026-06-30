# Phase 14: Retrieval And Active Recall

## Overview

Make memory searchable and injected into paper decisions, not just stored.

## Related Code

- `memory_retrieval.py`
- `autonomous_paper_trading_brain.py`
- `inner_critic.py`
- `llm_reasoning_agent.py`
- `daily_exam_agent.py`

## Implementation Steps

1. Extend FTS index: episodes, reviews, replays, exams, LLM critiques, skills, patch reviews, DONT_DO.
2. Add structured filters: setup, symbol, side, regime, source, date, status.
3. Add active recall call before paper open/skip.
4. Inner critic reads top DONT_DO and relevant memories.
5. LLM reasoning receives compact recall context with evidence ids.
6. Track `active_recall_hit_rate`, precision sample, decision delta, and A/B no-recall comparisons where safe.
7. Add as-of recall filtering: `memory_promoted_at`, `evidence_outcome_known_at`, and source events must be <= decision cutoff.
8. Exclude same-window trial outcomes, frozen holdout evidence, retired memories, and memories without valid cutoff proof from decision recall.
9. Add FTS lifecycle: index active summaries only, tombstone/delete retired rows, scheduled optimize/integrity check, rebuild command, retention policy, and p95 recall query budget.
10. Add labeled memory eval corpus: query -> expected memories, forbidden memories, contradiction cases, stale cases, allowed effect, precision@k, recall@k, false-block rate, latency, and cost budget.
11. If vector/embedding retrieval is added, require embedding model/version, dimension, dedupe hash, re-embed policy, stale vector delete, ANN/FTS parity, disk cap, erasure propagation, and rebuild integrity.
12. Add recall decay/pruning: memories not recalled for N qualified opportunities are downgraded or retired unless protected by fresh evidence.

## Tests

- Candidate retrieves relevant prior setup/regime failures.
- Retired/stale memory excluded by default.
- Empty DB degrades cleanly.
- Decision record includes memory ids used.
- Irrelevant/stale recall is penalized in recall quality metrics.
- Recall-caused block/action records decision delta.
- Replay at time T cannot retrieve a memory promoted after T.
- Same trial's future outcome memory cannot influence earlier decisions.
- Retired/stale memory is removed or tombstoned from active FTS and recall p95 stays within budget.
- Memory eval corpus passes expected/forbidden recall, stale exclusion, contradiction, false-block, latency, and cost thresholds.
- Vector/embedding index rebuild matches FTS ids, deletes stale vectors, enforces disk caps, and respects erasure.
- Non-recalled stale memories are pruned/downgraded and stop influencing active recall.

## Done Gate

Every paper decision can answer "what did it remember?"

## Audit Questions

- Is recall relevant or just keyword noise?
- Can old wrong memory keep blocking forever?
- Is recall time-safe or leaking future outcomes?
- Is FTS storing useful active summaries or unbounded stale text?
