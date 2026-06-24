# Trading Agent Incident Runbook

## RUNBOOK-KILL-SWITCH

Use when autonomous paper learning is stale, corrupted, overtrading, leaking secrets, or unsafe.

1. Activate `kill_switch.activate_kill_switch(reason)`.
2. Keep dashboard readable; do not remove state files manually.
3. Check `state/incident_history.jsonl` and `state/alerts_latest.json`.
4. Run quality gate: `venv\Scripts\python.exe test_harness.py --run-tests tests -q`.
5. Run `recovery_drill.run_noop_drill()` or restore from a specific archive manifest.
6. Clear kill switch only after root cause is documented.

## RUNBOOK-CORRUPTED-STATE

1. Run `data_hygiene_auditor.audit_learning_state()`.
2. Archive the corrupted file before editing.
3. Restore from `state/archive_manifests/*.json` if available.
4. Rebuild derived indexes such as `memory_retrieval.rebuild_index()`.

## RUNBOOK-SOURCE-OUTAGE

1. Mark source degraded in `data_source_registry.py`.
2. Do not substitute stale latest data as fresh evidence.
3. Counterfactual/replay rows must be `unresolved` until coverage is restored.
