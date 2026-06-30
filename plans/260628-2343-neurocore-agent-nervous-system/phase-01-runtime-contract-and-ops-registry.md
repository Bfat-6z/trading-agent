# Phase 01: Runtime Contract And Ops Registry

## Overview

Normalize every daemon's heartbeat/latest/history contract before central event routing.

## Related Code

- `agent_runtime_contract.py`
- `agent_process_supervisor.py`
- `agent_health_monitor.py`
- `agent_job_registry.py`
- `state/agent_memory/*latest.json`

## Implementation Steps

1. Create a registry mapping agent name -> heartbeat path -> latest path -> history path -> stale SLA.
2. Stop hard-coded `agent_memory/{name}_latest.json` assumptions.
3. Validate latest freshness, schema, history append, pid, and last error.
4. Add runtime contract failures to event store and dashboard.
5. Add duplicate writer detection per output path.
6. Add per-output single-writer lease: canonical absolute path key, PID/build id owner, writer epoch, startup fail-closed on active conflict, and stale lease recovery only after supervisor kill proof.
7. Link latest to durable history/bus: `latest.last_history_seq`, `latest.event_seq_max`, latest checksum, monotonic writer epoch, and health fail when latest seq is absent from durable history.
8. Require every state/latest/reconciler output to carry `environment`, `account_scope`, `credential_fingerprint`, `source_ledger_id`, `producer_build_id`, and `writer_epoch`; missing or mixed scope is unhealthy.
9. Add daemon test harness: fake clock, fake supervisor/process table, temp PID/port registry, bounded wait helpers, subprocess log capture, no arbitrary sleeps, and cleanup assertions.
10. Add stale PID/build-id defense: PID reuse, orphaned writer lease, duplicate manual/scheduler instance, and stale port owner must fail before any writer starts.

## Tests

- Non-standard latest path like `paper_execution_lifecycle_latest.json` passes when registered.
- Missing history fails contract.
- Stale heartbeat marks degraded.
- Duplicate writer for same latest path is flagged.
- Duplicate writer conflict blocks startup until lease is released or killed by supervisor.
- Fresh latest without matching durable history/bus seq is unhealthy.
- Latest/reconciler output missing `environment/account_scope/credential_fingerprint/source_ledger_id` is rejected.
- PID reuse, stale build id, duplicate manual instance, and orphaned single-writer lease cannot acquire write ownership.
- Daemon tests use fake clock/temp state and produce subprocess logs without fixed sleeps.

## Done Gate

All supervised NeuroCore agents have explicit runtime contracts and dashboard status.

## Audit Questions

- Does every agent have heartbeat/latest/history?
- Can stale latest still show green?
- Can two processes write the same state file?
- Can latest look fresh while replay/history lost the event?
