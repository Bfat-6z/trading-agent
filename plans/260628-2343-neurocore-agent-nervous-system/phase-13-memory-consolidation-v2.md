# Phase 13: Memory Consolidation V2

## Overview

Fix memory source bugs and make OpenClaw-style Light/REM/Deep meaningful.

## Related Code

- `memory_consolidation_agent.py`
- `episodic_task_ledger.py`
- `counterfactual_replay_agent.py`
- `belief_ledger.py`
- `dont_do_memory.py`

## Implementation Steps

1. Fix input paths: `episodes.jsonl`, `counterfactual_replays.jsonl`, post-trade reviews, exams, LLM critiques.
2. Extract rich lesson text: setup, symbol, side, regime, cost, MFE/MAE, failure reason, counterfactual conclusion.
3. Light: dedupe/stage candidates.
4. REM: cluster repeated patterns and contradictions.
5. Deep: promote only with sample/context/recall gates.
6. Add consumers: promoted memory -> belief ledger, DONT_DO miner, skill forge, retrieval index.
7. Define numeric Deep gates: minimum evidence count, unique days, unique symbols/regimes where relevant, source quorum, recency window, contradiction cap, TTL.
8. Add evidence resolver: every evidence id must exist, match type/window/candidate, and have immutable payload hash.
9. Add DONT_DO lifecycle: severity, scope, expiry, revalidation cadence, counter-evidence, suppress, retire.
10. Add time-safety fields: `evidence_outcome_known_at`, `memory_created_at`, `memory_promoted_at`, source cutoff proof, and trial partition id.
11. Memory promotion cannot use outcome evidence not known before the promotion cutoff or evidence from a frozen readiness holdout.
12. Separate human/user preference partition with allowed effects `ui_only|research_only`; deterministic metrics dominate LLM/user preference.
13. Human feedback reviewed lifecycle: classes, allowed effects, max weight, evidence resolver, reviewer, expiry, decay, and zero learning weight for praise/blame without evidence.
14. Define Light/REM/Deep schedule: Light frequency, REM batch window, Deep promotion window, catch-up/backfill after downtime, idempotent watermarks, and no promotion during active trial freeze.
15. Bound Light/REM storage: max staged candidates, per-source caps, age buckets, merge/dedupe policy, overflow quarantine, compaction metrics, and disk budget.
16. Add contradiction graph: claim key, scope, evidence polarity, conflict severity, resolver rule, supersede/quarantine/retire status, downstream invalidation, and audit trail.
17. Add forgetting/decay: confidence decay curve, revalidation cadence, stale-belief retirement, downgrade triggers, and pruning when not recalled for N qualified opportunities.
18. Add invalidation events when setup contract, source trust, regime taxonomy, feature schema, scoring formula, instrument metadata, or metric manifest changes; affected memories stop influencing decisions until revalidated.
19. Add memory-specific budget reservations for summarization, clustering, embeddings, rebuilds, eval replay, and vault export. Degraded mode: FTS-only, no embeddings/LLM summarization, and no new promotion.
20. Add `learning_claim` contract: claim requires changed memory/skill ids, evidence ids, and deterministic consumer impact. Otherwise it is stored as `hypothesis_only`.

## Tests

- Runtime state with existing episodes produces meaningful candidates.
- One anecdote cannot promote.
- Contradicted lesson is rejected/staged.
- Promoted DONT_DO reaches inner critic.
- Fake/stale/wrong-window evidence id rejects memory promotion.
- Expired or contradicted DONT_DO stops blocking decisions.
- Outcome evidence after promotion cutoff rejects memory promotion.
- Readiness-holdout evidence cannot be promoted into memory during the same trial.
- User preference or praise/blame cannot promote memory/skill without objective evidence.
- Light/REM caps prune or compact without losing audit metadata; overflow does not promote.
- Contradiction graph retires, quarantines, or supersedes memories and invalidates affected consumers.
- Stale belief decays/retires and cannot block decisions after revalidation fails.
- Setup/source/regime/schema/scoring/instrument change invalidates scoped memories until revalidated.
- Memory budget exhaustion follows degraded mode and blocks promotion instead of silently skipping checks.
- `learning_claim` without deterministic consumer impact is classified `hypothesis_only`.

## Done Gate

Promoted memories change future paper decisions through explicit consumers.

## Audit Questions

- Did memory promote a label like `bad_loss`, or a real market lesson?
- Where is the promoted memory used?
- Was this memory known before the decision/trial using it?
- Is this lesson from market evidence or user preference/LLM appeasement?
- Can memory grow without bounds or keep stale wrong beliefs active?
- Did a claimed lesson change a deterministic consumer?
