# Phase 00: Safety Firewall Hardening

## Overview

Make paper-only safety system-wide before adding NeuroCore speed. This phase blocks hidden live paths, recursive live intent, secret leaks, and unsafe legacy scripts.

## Related Code

- `live_permission_firewall.py`
- `llm_output_quality_gate.py`
- `preflight_guard.py`
- `kill_switch.py`
- `security_import_guard.py`
- `runtime_config.py`
- `.env.example`
- `execute_*.py` legacy scripts
- `*live*.py`, `check_*live*.py`, and any authenticated exchange/private-account script

## Implementation Steps

1. Add recursive `sanitize_and_detect()` for dict/list/scalar.
2. Detect live-order words in nested keys and values.
3. Redact nested secret-looking keys/values while preserving object shape.
4. Make `preflight_guard` write only sanitized payloads.
5. Make `kill_switch_active()` a hard preflight fail.
6. Add repo-wide import guard mode for unsafe live execution imports.
7. Hard-quarantine old `execute_*.py`, `*live*.py`, and authenticated exchange/private-account scripts: deny direct `python script.py`, deny import/exec/subprocess, remove or wrap entrypoints, and emit signed `legacy_script_blocked` on every attempt.
8. Set `.env.example` safe defaults (`DRY_RUN=true`, no live-enabling defaults).
9. Add end-to-end test that NeuroCore Phase A cannot produce live order permission.
10. Add a typed `runtime_config` loader with explicit env allowlist, precedence, source fingerprint, and no implicit cwd `.env` load.
11. Split namespaces for paper-only data keys and any future live keys; NeuroCore must reject live-trading key fingerprints at startup.
12. Make live structurally impossible: one paper-only execution adapter, deny order endpoints, deny direct exchange SDK/REST/subprocess order calls, and require read-only exchange credentials.
13. Add global redaction sink for logs, stderr/stdout, tracebacks, HTTP debug, provider SDK logs, latest/history JSON, dashboard API, and Task Scheduler/service output.
14. Scrub child-process environment before supervisor launches workers; pass only approved keys and sentinel test values in test mode.
15. Add script classification manifest for every executable/manual script: `paper_safe`, `readonly_private`, `blocked_legacy_live`, `operator_command`, or `unknown`. Unknown scripts fail CI/preflight.
16. Ban generic `allowed: true` as a live-facing signal. Firewall outputs must use explicit fields: `can_place_live_orders=false`, `live_permission=false`, and `paper_action_allowed=<bool>`.
17. Add local/manual bypass guard: operator attempts that are not signed, classified, and role-authorized emit `operator_command.denied` and cannot mutate paper, risk, config, memory, skills, or live state.
18. Repo/state/export wording scan must quarantine legacy artifacts containing authoritative-looking `LIVE STATE`, `allowed:true`, `permission`, `eligible`, or live-review phrasing unless explicitly marked non-authoritative.

## Tests

- Nested `{"payload": {"action": "create_order"}}` blocked.
- Nested API keys redacted in latest/preflight outputs.
- Kill switch blocks preflight even when other checks pass.
- Repo-wide security scan catches legacy execute/live imports.
- LLM nested order recommendation is sanitized and recorded as violation.
- Direct Binance/SDK/REST/subprocess order attempt is blocked even if code bypasses normal paper adapter.
- Real-looking live key fingerprint makes NeuroCore fail closed.
- Sentinel secret never appears in logs, latest/history, dashboard payloads, stderr, or scheduler output.
- Child process cannot inherit unapproved env keys.
- `python execute_*.py`, `python *live*.py`, import, raw HTTP, SDK, and subprocess live-order/private-account attempts are blocked with signed denial events.
- `check_*live*.py` and read-private-account scripts are classified `readonly_private` or blocked; no paper/readiness path can use their output as account truth.
- CI fails on unclassified executable scripts and on positive generic `allowed: true` in live-facing payloads.
- Manual command without manifest role/signature/idempotency emits denial and changes no state.

## Done Gate

- `can_place_live_orders=false` everywhere.
- `live_permission=false` everywhere; `paper_action_allowed` is the only positive permission-like field and is never interpreted as live permission.
- No raw secrets in test snapshots.
- Legacy live/authenticated scripts cannot be called accidentally or manually by NeuroCore without fail-closed denial.
- No runtime path can reach a live order endpoint from NeuroCore; paper adapter is the only execution adapter.
- Script manifest inventory covers every executable script and every authenticated exchange client path.

## Audit Questions

- Can any model output nested JSON that bypasses firewall?
- Can any preflight latest file leak `.env` values?
- Can a legacy script place an order outside supervisor?
- Can a renamed script, imported SDK, raw HTTP call, or subprocess reach a live order endpoint?
- Can ambient user/system env override safe paper config?
- Can a manual script or old live checker become authoritative account/readiness state?
- Can any output wording imply live permission because it says `allowed`?
