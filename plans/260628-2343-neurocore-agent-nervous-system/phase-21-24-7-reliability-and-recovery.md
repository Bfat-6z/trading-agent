# Phase 21: 24-7 Reliability And Recovery

## Overview

Make the agent survive overnight and make failures visible.

## Status

Complete on 2026-06-30.

Implementation report: `reports/260630-phase-21-implementation-report.md`

## Related Code

- `host_runtime_monitor.py`
- `agent_process_supervisor.py`
- `agent_health_monitor.py`
- `scalp_watchdog.py`
- `backup_restore.py`
- `recovery_drill.py`
- `paper_account_reconciler.py`
- `operator_control.py`
- `state_reconciler.py`
- `agent_status_dashboard.py`

## Implementation Steps

1. Verify Windows Task Scheduler or service autostart, not just env flag.
2. Detect last boot, sleep/resume gaps, power plan, missed runtime window.
3. Add `/healthz` dashboard probe and port conflict fallback/preflight.
4. Add restart backoff, max restart window, incident event.
5. Add log rotation by size/time and disk pressure alert.
6. Add scheduled backup manifest and checksum restore drill.
7. Add dashboard fail-to-fetch incident event.
8. Autostart proof must include trigger, working dir, venv path, user context, env/secrets source, "run whether user is logged on" setting, and post-reboot assertion.
9. Add per-daemon restart circuit breaker: persisted crash counter, jitter, quarantine on config/port/preflight failure, and process-count cap.
10. Add sleep/resume policy: pause paper opens, quarantine stale windows, run catch-up/replay, mark affected period invalid for promotion.
11. Define backup RPO/RTO/retention/scope, atomic snapshot method, restore-to-clean-dir drill, and off-host option.
12. Capture Task Scheduler/service stdout/stderr, Windows Event Log/task history, per-daemon log path, redaction tests, and disk-full behavior.
13. Add external local probe outside dashboard server, so fail-to-fetch can be detected when dashboard API is down.
14. Add incident schema: incident_id, severity, status, owner, opened_at, acked_at, resolved_at, closed_at, action_required, runbook_id.
15. Add SLI/SLO/error budgets for bus lag, DLQ age, heartbeat freshness, dashboard healthz, trace ingestion, backup restore.
16. Add alert catalog with signal, threshold, severity, dedupe key, cooldown, owner, escalation, auto-close criteria.
17. Link incident timeline to daemon, alert, restart attempt, DLQ/event ids, affected decisions/trades.
18. Add postmortem/RCA trigger for Sev1/Sev2/repeated incidents with owner, due date, regression test, recurrence check.
19. Add checked-in Windows runner scripts using `Set-StrictMode`, `$ErrorActionPreference='Stop'`, `Set-Location -LiteralPath`, absolute quoted venv Python path, sanitized `PATH/PATHEXT/PYTHONPATH`, `PYTHONUTF8=1`, and `exit $LASTEXITCODE`.
20. Launch scheduled tasks/services hidden and non-interactive: `-NoProfile`, hidden window/service wrapper, redirected logs, explicit session/user behavior, no focus-stealing console loops.
21. Track and clean process trees using Windows Job Objects or explicit child PID trees; verify descendant shutdown, port release, and file-handle release before restart.
22. Add canonical port registry/latest with bound port, pid, build id, token scope, and server identity. All probes read registry and verify identity.
23. Backups must exclude `.env` and token files by default, encrypt off-host copies, store key separately, and run restore-time sentinel secret scan.
24. Define daily report boundary: UTC event storage, configured report timezone/cutoff, monotonic SLA timers, `w32tm`/clock drift check, and sleep/midnight replay tests.
25. Add Windows path policy: quote every path, use `-LiteralPath`, shorten state root, test spaces and >260 char paths, and avoid shell string composition for process launch.
26. Add `paper_account_reconciler`: rebuild account from genesis ledger/events and compare latest portfolio, positions, scoring totals, and backup restore output.
27. Add storage economics: free-space preflight, incremental/diff compressed backups, tmp-space budget, WAL cap, cache exclusion, prune TTL/count, archive restore drill.
28. Add handle/lock leak probe before restart/backup and incident on unreleased SQLite/file handles.
29. Add historical secret/privacy scrub before backup and after restore across DB, JSONL, FTS, vault, logs, screenshots/OCR, and prompt traces; rotate credentials on hit and block serving until clean.
30. Restore must replay erasure ledger before any dashboard/agent reads restored data.
31. Add adversarial recovery drill matrix: power loss during SQLite commit, WAL checkpoint, manifest write, backup copy, restore copy, ledger append, dashboard read, and proof-bundle export. Each inject records invariant, detection signal, recovery action, owner, and artifact.
32. Add cold-start-from-crash drill: delete all derived/latest files, keep canonical ledgers/events/payload store, boot services in dependency order, and prove no stale latest state is trusted.
33. Add zombie/split-brain drill: scheduler plus manual instance, stale PID/port registry, orphan writer lease, reused PID, stale build id, and duplicate dashboard port must quarantine duplicates before any writer starts.
34. Replace or quarantine legacy `state_reconciler.py`; any reconciler must use `source_mode=paper_ledger|shadow_readonly`, carry `environment/account_scope/credential_fingerprint/source_ledger_id`, and forbid live account snapshots in paper readiness.
35. Define numeric SLO/error budgets: bus lag, DLQ age, heartbeat freshness, dashboard liveness/readiness, trace ingestion, backup restore, restart rate, and alert noise. Store burn-rate windows and actions for budget exhaustion.
36. Add severity taxonomy: Sev1 safety/live/secret/data-loss/trial-corruption, Sev2 trial invalidation/core outage, Sev3 degraded/needs daily review, Sev4 informational.
37. Add alert anti-fatigue rules: hysteresis, min duration, multi-window burn-rate, flap detection, N-bad-probes rule, dedupe by daemon/root cause/resource/window, reopen window, silence expiry, and false-positive budget.
38. Paging policy: Sev1/Sev2 page only, Sev3 daily digest, max pages/hour, maintenance/sleep/resume suppression, expected supervised restart suppression, and successful-recovery downgrade.
39. Add operational RACI: primary operator, backup operator, incident commander, dashboard/security owner, backup/key custodian, trial owner, hotfix approver, abort authority, final-report approver.
40. Add checked-in runbook catalog. Each runbook includes command syntax, expected output, validation, rollback, owner, escalation, and postmortem trigger.
41. Add operator CLI contract: status, pause opens, resume opens, cancel pending, reduce exposure, kill switch on, backup now, restore drill, rotate token, quarantine daemon, acknowledge incident, close incident. Every command is idempotent and signed.
42. Add escalation ladder: ack SLA, escalation timeout, backup owner takeover, after-hours rule, contact mechanism, and unresolved incident auto-escalation.
43. Add postmortem template: timeline, evidence ids, root cause, impact, owner, action SLA, regression test, recurrence check, reopen rule, and overdue escalation.

## Tests

- Port 8090 busy does not create restart storm.
- Stale daemon backoff records incident.
- Sleep/resume gap appears in health latest.
- Backup restore drill verifies checksum.
- Dashboard `/healthz` fails when payload builder throws.
- Post-reboot autostart proof passes from clean Windows session.
- Sleep/resume pauses paper opens and marks affected window invalid.
- Restart storm trips circuit breaker instead of looping.
- Restore drill reconstructs usable state in clean directory within RTO.
- Critical incident cannot be considered handled without ack/resolution/close fields.
- Task Scheduler runs from `C:\Windows\System32` and still executes the correct repo/venv with nonzero exit propagation.
- Scheduled restart does not open visible PowerShell windows.
- Supervisor kill/restart removes descendants and releases dashboard port/files before respawn.
- Probes follow canonical port registry and reject wrong owner/build id.
- Backup does not include `.env` or tokens and restore scan catches sentinel leaks.
- Account reconciler fails if latest equity differs from ledger-derived equity.
- Backup refuses to start without enough free/tmp space and excludes rebuildable caches.
- Handle leak test catches dashboard/worker keeping DB/log handles open.
- Historical leaked secret in JSONL/vault/log is detected before backup and forces rotation/redaction event.
- Deleted payload remains absent after restore because erasure ledger replay ran first.
- Recovery drill corrupts/truncates WAL, manifest, latest snapshot, and backup copy; restore either fails closed with incident or rebuilds from canonical ledgers.
- Cold start after simulated crash rebuilds latest/dashboard/scoring from ledgers and refuses pre-crash latest files.
- Duplicate scheduler/manual process cannot acquire writer lease or serve dashboard under stale pid/build identity.
- Restore cannot serve until event bus replay, account reconciliation, erasure replay, secret scan, owner approval, and identity checks pass.
- Legacy/live reconciler mode cannot feed paper readiness; missing/mixed state namespace fields fail.
- SLO burn-rate tests create Sev1/Sev2/Sev3 incidents with correct paging/digest behavior and no alert storm.
- Restart burst is deduped; expected supervised restart does not page, but repeated quarantine opens a single incident.
- Operator command set records signed idempotent audit events and rejects unauthorized/unknown commands.
- Runbook/postmortem validation requires owner, timeline, evidence, rollback/verification, regression test, and due dates.

## Done Gate

Agent can run unattended with visible incidents and recovery proof.

## Audit Questions

- What happened while user slept?
- Did the dashboard die or only the browser tab?
- Did Windows launch the right hidden runner with sanitized env and path?
- Can account state be rebuilt after reboot/backup restore without equity drift?
- Can backup/checkpoint fill the disk or race a leaked Windows handle?
- Can old backups or JSONL resurrect a secret/private payload?
- Can alert noise hide a real Sev1/Sev2 or wake the operator for every restart?
- Who owns restore, backup keys, incident command, trial abort, and public dashboard exposure?
