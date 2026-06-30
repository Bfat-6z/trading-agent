# Trading Agent Incident Runbook

## Catalog Contract

Every runbook section must state:

- Owner: role responsible for execution.
- Escalation: next role and timeout.
- Expected output: observable success signal.
- Rollback: exact safe revert command or action.
- Validation: command or file that proves recovery.
- Postmortem trigger: when RCA is required.

## RUNBOOK-KILL-SWITCH

Use when autonomous paper learning is stale, corrupted, overtrading, leaking secrets, or unsafe.

Owner: primary_operator.
Escalation: incident_commander after 15 minutes unacked.
Expected output: kill switch active event, dashboard still readable, no new paper opens.
Rollback: clear kill switch only after validation and owner approval.
Validation: `state/incidents_latest.json`, `state/alerts_latest.json`, and Phase 21 pytest pass.
Postmortem trigger: any Sev1/Sev2, repeated activation, data loss, or secret leak.

1. Activate `kill_switch.activate_kill_switch(reason)`.
2. Keep dashboard readable; do not remove state files manually.
3. Check `state/incidents_history.jsonl`, `state/incidents_latest.json`, and `state/alerts_latest.json`.
4. Run quality gate: `venv\Scripts\python.exe test_harness.py --run-tests tests -q`.
5. For restore, use `backup_restore.restore_backup()` from a known backup file; restore is blocked if the secret scan detects API keys, tokens, passwords, or private keys.
6. Clear kill switch only after root cause is documented.

## RUNBOOK-CORRUPTED-STATE

Owner: trial_owner.
Escalation: backup_key_custodian after restore failure or checksum mismatch.
Expected output: corrupted files archived, restored state passes secret scan, dashboard serves rebuilt latest files.
Rollback: restore original archive only if secret scan and checksum pass.
Validation: `backup_restore.restore_backup()` result `ok=true`, `agent_process_supervisor.py --status`, and targeted pytest pass.
Postmortem trigger: any restore failure, equity drift, erasure violation, or repeated corrupted-state incident.

1. Run `data_hygiene_auditor.audit_learning_state()`.
2. Archive the corrupted file before editing.
3. Restore from `state/backups/*` with `backup_restore.restore_backup()` if available; keep the secret scan enabled.
4. Rebuild derived indexes such as `memory_retrieval.rebuild_index()`.

## RUNBOOK-SOURCE-OUTAGE

Owner: data_source_owner.
Escalation: primary_operator after 30 minutes stale or if paper opens would depend on missing required data.
Expected output: source marked degraded, required capability gaps block paper opens, replay rows stay unresolved.
Rollback: mark source healthy only after fresh provenance-bearing data arrives.
Validation: Feature Factory latest shows degraded source, dashboard freshness shows stale/degraded, targeted source tests pass.
Postmortem trigger: outage invalidates a trial window, causes stale decision data, or repeats within 24h.

1. Mark source degraded in `data_source_registry.py`.
2. Do not substitute stale latest data as fresh evidence.
3. Counterfactual/replay rows must be `unresolved` until coverage is restored.
