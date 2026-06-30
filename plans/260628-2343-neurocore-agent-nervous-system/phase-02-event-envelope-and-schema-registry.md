# Phase 02: Event Envelope And Schema Registry

## Overview

Create a canonical event envelope so every market/news/trade/memory/test item is typed, timestamped, traceable, and replayable.

## Related Code

- `event_store.py`
- `agent_data_contracts.py`
- `timebase.py`
- `source_provenance.py`
- `reasoning_trace.py`

## Event Envelope

```json
{
  "event_id": "evt_...",
  "event_type": "paper.close",
  "schema_version": "neurocore.v1",
  "schema_digest": "sha256:...",
  "producer_id": "paper_execution_lifecycle",
  "producer_version": "git_or_file_hash",
  "idempotency_key": "source:event_type:correlation:sequence",
  "payload_hash": "sha256:...",
  "occurred_at": "UTC ISO",
  "available_at": "UTC ISO",
  "known_at": "UTC ISO",
  "effective_at": "UTC ISO or null",
  "ingested_at": "UTC ISO",
  "source_id": "paper_execution_lifecycle",
  "correlation_id": "trade_id or run_id",
  "causation_id": "previous event id",
  "priority": 50,
  "provenance_id": "prov_...",
  "payload": {}
}
```

All persisted text/JSON/Markdown state must be UTF-8 without BOM. Hashes are computed over canonical UTF-8 JSON with sorted keys and explicit decimal string fields.

## Implementation Steps

1. Define envelope dataclass/helpers.
2. Extend schema registry for event types with per-type versions.
3. Add compatibility classes: additive, breaking, deprecated.
4. Add upcaster/downcaster hooks for old replay rows.
5. Store registry digest in events and replay manifests.
6. Add validation before `append_event`.
7. Add idempotency key separate from event hash with documented uniqueness scope.
8. Standardize `occurred_at`, `available_at`, `known_at`, `effective_at`, `ingested_at`, `processed_at`.
9. Add producer ACL: which agent can append which event types.
10. Add append-only integrity hash chain for critical learning/scoring events.
11. Inventory existing JSONL/latest/history files and define old-row to event-envelope mapping.
12. Generate dry-run backfill counts, checksums, and a backfill manifest without deleting old state.
13. Define trading lifecycle event schemas: `paper.order`, `paper.fill`, `paper.position_update`, `paper.close`, `paper.liquidation`, and `funding.settlement`.
14. Add ledger transaction idempotency separate from envelope idempotency, keyed by `{venue, account_mode, order_id, fill_id/execution_id, side, qty, price}`.
15. Add signed append-only audit ledger events for safety flags, kill switch, config/risk threshold changes, approvals, MCP capability changes, dashboard readiness outputs, and promotion/readiness decisions.
16. Add canonical time policy: UTC storage, configured report timezone/cutoff, monotonic elapsed fields for SLAs, and clock-drift event when wall clock changes.
17. Define social/news event schemas: `social.post.created`, `social.post.edited`, `social.post.deleted`, `social.claim.retracted`, `news.snapshot.captured`, and dependent-feature invalidation events.
18. Add cutoff proof object for decisions/replay/score/memory/retrieval: `decision_cutoff`, `max_known_at`, `max_available_at`, `max_ingested_at`, `max_finalized_at`, `latency_buffer_ms`, and input event ids.
19. Separate immutable envelope metadata from encrypted deletable payload blobs. Hash redacted metadata, store payload key id, and support crypto-shred with append-only erasure receipt.
20. Add candidate lifecycle event schemas: `scan_universe.snapshot`, `scan_universe.gap`, `scan_universe.rate_limited`, `candidate.generated`, `candidate.prefiltered_out`, `candidate.not_evaluated`, `candidate.ranked`, `candidate.skipped`, `candidate.expired`, `candidate.selected`, and `candidate.missed`.
21. Add operator/human event schemas: `operator_command.applied`, `operator_command.denied`, `legacy_script_blocked`, `operator_intervention.applied`, `human_feedback.imported`, and `human_feedback.reviewed`.
22. Add central approval schema: approver identity, role, key id, quorum/dual-control class, proposer id, approver != proposer, TTL, nonce, revocation, denial event, conflict handling, emergency break-glass scope, and replay validation.
23. Add daily signed root checkpoint: previous root, event seq range, wall-clock timestamp, machine/user id, git/config/schema/metric digests, and external/checkpoint digest where available.
24. Add test determinism fields to manifests: fixed seed, temp state root, timezone, clock mode, fixture ids, network mode, and provider fixture version.

## Tests

- Missing schema version rejects event.
- Same idempotency key dedupes.
- Same idempotency key with different payload hash rejects as conflict.
- Source/provenance missing rejects high-value data events.
- Time ordering stores UTC consistently.
- Feature builder rejects sources where `available_at` is after decision cutoff.
- V1 fixture replay still loads after V2 schema exists.
- Unauthorized producer cannot append scoring/memory events.
- Existing runtime state can be mapped or explicitly quarantined with reason.
- Duplicate fill/execution transaction is not applied twice even if envelope id differs.
- Signed audit ledger hash chain fails on tampered safety/risk/config/approval event.
- UTF-8 no-BOM fixture hashes match under Windows PowerShell 5.1 and PowerShell 7.
- Daily report cutoff uses configured timezone while event storage remains UTC.
- Edited/deleted Telegram/news item emits immutable new version/tombstone and invalidates dependent features.
- Replay/scoring/memory query fails if any input violates cutoff proof.
- Crypto-shredded payload is unrecoverable while audit hash chain still validates redacted metadata.
- Candidate lifecycle and operator intervention events are replayable and causally linked to trades.
- Candidate denominator includes prefilter rejects, not-evaluated rows, scanner gaps, and rate-limited skips.
- Approval event replay enforces signer role/quorum/TTL/revocation and rejects proposer-self-approval.
- Daily signed root checkpoints are monotonic and fail if regenerated or missing in a trial window.
- Tests fail if shared `state/`, ambient `.env`, nondeterministic clock, or unmocked network is used.

## Done Gate

New events cannot enter the nervous system without envelope validation.

## Audit Questions

- Can a raw JSONL row bypass contracts?
- Can two events collide by hash because payload omitted fields?
- Can a duplicate fill mutate account state twice?
- Can safety/config/risk changes be edited without breaking the audit ledger?
- Can edited/deleted social/news content rewrite history?
- Does every downstream learner enforce the same cutoff proof?
- Can privacy erasure coexist with immutable audit/replay metadata?
