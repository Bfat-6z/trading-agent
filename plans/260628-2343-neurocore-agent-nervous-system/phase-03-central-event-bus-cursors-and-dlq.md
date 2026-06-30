# Phase 03: Central Event Bus Cursors And DLQ

## Overview

Upgrade `event_store.py` + `agent_work_queue.py` into a central bus with consumer offsets, replay cursors, priorities, retries, and dead-letter queue.

## Related Code

- `event_store.py`
- `agent_work_queue.py`
- `agent_job_registry.py`
- `atomic_state.py`

## Implementation Steps

1. Add immutable event sequence separate from wall-clock time.
2. Add consumer subscriptions: consumer, event types, last seen sequence, unacked event ids, acked_at, lag.
3. Add `read_events(subscription, limit)` and CAS `ack_events(subscription_id, event_ids, lease_token, attempt_id, expected_seq)`; do not use one scalar cursor for filtered reads.
3. Add retry count and DLQ records for failed processing.
4. Add lease/visibility timeout so crashed consumers do not permanently own events.
5. Add producer/consumer ACL checks for append/read/ack.
6. Emit job lifecycle events from `agent_work_queue`.
7. Add replay API by event id/time/type/source.
8. Add replay manifest requirement: schema digest, code/config version, feature builder version, source snapshot hashes, model/prompt versions where relevant.
9. Add bus health latest/history.
10. Add feature flags for dual-write and dual-read during cutover.
11. Add cutover checklist: snapshot backup, drain/unacked checkpoint, compatibility adapter status, rollback command.
12. Specify SQLite/FTS concurrency rules: WAL mode, `busy_timeout`, short write transactions, single-writer queue, checkpoint policy, and retry/backoff on lock errors.
13. Add backup quiesce protocol: pause writers, drain/flush queue, checkpoint WAL, copy with temp+atomic rename, resume writers, and record snapshot manifest.
14. Add event attempt table: unique active lease, attempt state, retry cause hash, terminal `processed|dlq` CAS, success/DLQ exclusivity, and idempotent result/outbox key.
15. Add retention/scale policy: hot/archive partitions, per-event TTL, payload pruning to hashes, WAL/page/DB size gates, JSONL seal/compress cutover exit, and FTS optimize/vacuum schedule.
16. Add query-plan contract: composite/covering indexes for event/time/type/source/subscription/drilldown, `EXPLAIN QUERY PLAN` fixtures, and p95 query budgets.
17. Add bus-wide backpressure: bounded queues, producer quotas, max unacked, priority budgets, DLQ retention, and pause low-priority producers on lag/WAL/disk thresholds.
18. Define Windows IO lock policy: share mode, lock order, retry deadline, handle leak detection, readers close per request, and incident on stuck lock.
19. Add retention matrix by data class: raw social text, screenshot original/OCR, user note, strategy note, prompt/trace, feature, event envelope, payload blob, vault note, backup, and restore copy.
20. Append erasure/tombstone receipts and propagate deletes/crypto-shred to payload store, FTS, vault, backups, and restored copies without breaking envelope hash chain.
21. Add restore-replay contract: restored event store replays from genesis or snapshot seq to target seq, verifies manifest digests, resets leases safely, reconstructs cursors, validates DLQ terminal states, and preserves outbox idempotency.
22. Define historical backup erasure semantics: backups older than an erasure either crypto-shred affected payload keys or must replay erasure overlays before any read/export/LLM/dashboard access.
23. Add backup custody/authority fields: backup owner, checker, restore approver, key custodian, off-host custody, restore go/no-go authority, and runbook id.
24. Define golden fixture corpus `golden_event_bus_replay_v1/`: mixed event types/priorities/sources, seq vs wall-clock skew, filtered/unfiltered subscriptions, unacked/expired leases, stale acks, retry-to-DLQ, missing manifest variants, query-plan expectations, and DB+WAL live-backup equality.
25. If vector indexes are added later, they must follow the retention/rebuild contract: embedding model/version, dimension, dedupe hash, re-embed policy, stale vector delete, ANN/FTS parity, disk cap, rebuild digest, and erasure propagation.

## Tests

- Consumer reads only unacked events.
- Replay from cursor is deterministic.
- Failed event retries then DLQs.
- Priority events are claimed before low priority.
- Job claimed/completed/failed emits bus events.
- Filtered subscription cannot skip event types not yet acked.
- Replay fails with `non_replayable_reason` when manifest data is incomplete.
- Dual-write old/new stores produce matching counts during shadow cutover.
- Rollback can restore readers to old JSONL/latest paths.
- Concurrent readers/dashboard/replay cannot starve writer or produce `database is locked` loops.
- Live backup taken during active writes restores to a replayable, untorn state.
- Stale ack after lease expiry is rejected.
- Same event cannot end as both processed and DLQ.
- Query plans for core reads do not full-scan hot event tables.
- Backpressure pauses experiment/backfill producers before core bus lag breaches SLA.
- Retention matrix test verifies TTL/archive/delete behavior per data class.
- Restore replay applies erasures before serving restored data.
- Restored backup replays to identical event seq, cursor state, DLQ state, outbox ids, checkpoint root, and bus health digest.
- Replay with duplicated, missing, reordered, or forked event seq fails before consumers start.
- Historical backup containing erased payload restores only redacted metadata after erasure overlay replay.
- Golden bus fixture validates read order, ack CAS, retry/DLQ exclusivity, filtered cursor behavior, and hot query plans with no full scans.
- Restore cannot start serving until replay, erasure overlay, secret scan, owner/build identity, and restore approval pass.

## Done Gate

Learning agents can consume by event type without scanning random JSONL files.

## Audit Questions

- Can an agent miss an event during restart?
- Can bad events block all consumers forever?
- Can Windows file locking corrupt or stall bus/FTS state?
- Can backup copy a torn DB/JSONL while daemons are writing?
- Can a stale worker ack or DLQ an event after another attempt owns it?
- Can storage growth or O(N) queries kill 24/7 runtime?
- Does archived/restored state resurrect data that should be erased?
