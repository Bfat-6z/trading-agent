# Phase 25: Loop Audit And Closure

## Overview

Run final adversarial audit before claiming NeuroCore v1 complete.

## Audit Lenses

1. Safety/firewall.
2. Data contracts/schema.
3. Event bus/replay.
4. Feature correctness.
5. Paper execution realism.
6. Experiment anti-overfit.
7. Real scoring.
8. Memory/skill evolution.
9. LLM governance.
10. Dashboard usability.
11. 24/7 reliability.
12. Trial validity.
13. Supply-chain/env/secret handling.
14. Windows runner/process-tree behavior.
15. Paper account ledger reconciliation.
16. Instrument registry/exchange metadata.
17. Financial-advice boundary and live-output wording.
18. Obsidian/vault source-of-truth, injection, links, stale notes, and redaction.
19. Privacy/retention/erasure across event store, FTS, vault, backups, prompts, screenshots.
20. Setup ontology/A+ rubric/candidate lifecycle/shadow validity.
21. Cost/quota/resource-budget governance and degraded-mode behavior.
22. LLM prompt/model eval regression and golden trace replay.
23. Dashboard chart truth, tooltip/export consistency, and visual readiness semantics.
24. UI accessibility, mobile density, Vietnamese glossary, and browser realism.
25. Recovery/restore drill realism: backup, restore, replay, erasure replay, ledger consistency, crash cold-start, and zombie process recovery under adversarial fault injection.
26. Legacy live/authenticated script quarantine, manual script manifest, and paper/live state namespace separation.
27. Git/change management/release hygiene: branch/worktree, phase commit manifest, rollback rehearsal, hotfix freeze, generated artifact policy, and proof bundle secret scan.
28. Golden fixture adequacy: event bus, paper ledger, funding/fees, Binance instruments, social/news parser, replay determinism, and dashboard chart truth.
29. CI/test harness realism: deterministic seeds, temp state roots, socket deny, fake daemon clock, Binance fixture server, Playwright smoke, and CI budgets.
30. Memory bloat/forgetting: storage caps, contradiction graph, decay, stale-belief retirement, recall eval, vector/FTS cleanup, and memory-cost ledger.
31. Skill patch governance: promotion manifests, approval authority, judge-tamper ban, tests-before-promote, rollback drill, poisoned-evidence rejection, and semantic conflicts.
32. Incident SLO and alert fatigue: numeric SLOs, burn-rate, severity taxonomy, dedupe, paging policy, false positives, and restart noise.
33. Operational owner/runbook: RACI, command set, escalation ladder, tunnel exposure, backup authority, postmortems, and human approvals.
34. Anti-fake-progress: trial retry farming, WR greenwashing, hidden operating cost, skipped denominator, survivorship, sample leakage, regenerated hash chains, equity resets, hallucinated learning, and UI greenwashing.

## Required Evidence

- Full test suite result.
- Targeted test list per phase.
- Runtime supervisor status.
- Dashboard API/healthz status.
- Latest Real Scoring Board.
- Latest trial report.
- Safety report with live permissions false.
- Signed audit ledger digest for safety/risk/config/approval/readiness/dashboard events.
- Dependency lock/SBOM/vulnerability audit output.
- Paper account reconciliation report.
- Instrument registry freshness report.
- Advice-boundary/disclaimer ledger sample.
- Vault export manifest, link-check report, stale-note report, import/conflict ledger, and vault redaction/secret scan.
- Signed trial proof bundle with failed/aborted attempts and recompute command.
- Retention matrix, erasure ledger, restore-after-erasure proof, historical secret scrub report.
- Setup contract registry, quality-tier rubric, candidate census, and shadow concordance report.
- Resource budget ledger report with reservations/charges/exhaustion/degraded-mode events.
- Golden prompt trace replay report and model canary eval report.
- Dashboard chart identity/audit report: snapshot/query/series/point hashes and tooltip/export parity.
- UI evidence: screenshots at 360px/768px/desktop, keyboard-only drilldown, table fallback, contrast/color-not-sole-signal result, untranslated-key/glossary scan, and no-horizontal-overflow proof.
- Recovery drill artifact bundle: fault matrix, injected failures, restored seq ranges, replay digests, erasure replay proof, ledger reconciliation, cold-start proof, zombie/split-brain quarantine log.
- Script manifest/security report: every executable classified, legacy/authenticated scripts blocked or readonly-quarantined, signed denial samples, no authoritative `LIVE STATE` artifacts.
- Release/change report: branch/commit manifest, generated artifact manifest, rollback drill, migration gate report, hotfix log, changelog/release notes if applicable, and bundle-level secret scan.
- Golden fixture corpus manifest at `tests/fixtures/neurocore_golden/manifest.json`: fixture versions, source hashes, expected outputs, recompute commands, phase mapping, and CI fixture ids.
- CI artifacts: run URL or local artifact path, JUnit/coverage, flaky retry report, socket/network deny report, frozen time/TZ report, daemon harness logs, Playwright screenshots.
- Memory governance report: DB/FTS/vector size, prune report, contradiction ledger, stale-belief retirement, recall eval, memory budget/cost ledger.
- Skill governance bundle: skill registry snapshot, patch ledger, approval manifests, promotion manifests, rollback drill, semantic conflict report, poisoned-evidence rejection report.
- Incident/SLO report: MTTA/MTTR, page count, dedupe effectiveness, false positive rate, alert catalog coverage, restart incident rate, SLO/error-budget burn.
- Ops ownership/runbook bundle: RACI, runbook catalog, operator command audit, escalation evidence, tunnel exposure log, backup/restore authority log, postmortem queue.
- Anti-gaming bundle: trial attempt census, scheduled-eval/candidate census, universe-at-time coverage, operating-cost-adjusted expectancy, capital events, signed root checkpoints, train-only fitted artifact replay, forbidden wording/API-schema audit.
- Known residual risks.

## Tests

- Final audit script checks every phase has tests, done gate, and audit questions.
- Full suite result is attached or explicitly marked unable to run.
- Dashboard healthz and supervisor status are captured.
- Safety report proves live permissions false.
- Golden e2e corpus passes from raw source through memory/score.
- Incident postmortem queue has no overdue Sev1/Sev2 actions.
- Audit ledger hash chain validates and external/checkpoint digest matches.
- Account state rebuilt from ledger matches latest/scoring after restore drill.
- Dependency audit and secret sentinel scan pass.
- Final report contains no legacy live-review-candidate or live-eligible wording.
- Vault audit passes: no broken evidence links, no stale current notes, no imported generated edits, no vault secret leaks.
- Trial proof recomputes from immutable trial-genesis ledger and includes all opens/attempts.
- Deleted/private payloads are absent from restored DB/FTS/vault/backups.
- Every scored trade has setup contract hash, quality tier, candidate lifecycle, and shadow/paper parity where required.
- No global/root/user/provider budget violation is hidden or locally masked.
- Prompt/model/schema/sanitizer changes replay golden traces without regression.
- Dashboard charts cannot display promotion-ineligible/stale/low-N data as ready.
- UI accessibility/mobile/glossary checks pass and screenshots are attached.
- Restore path cannot serve dashboard/agent/LLM reads before replay, erasure overlay, ledger reconciliation, secret scan, and identity checks complete.
- Crash cold-start does not trust stale latest files over canonical ledgers/events.
- Duplicate/zombie process cannot hold writer lease, dashboard port, or file handle after supervisor recovery.
- Legacy live/authenticated scripts are non-executable or readonly-quarantined; unclassified scripts fail.
- Golden fixture corpus replay passes and every truth-critical phase test cites fixture ids.
- CI proves deterministic temp-state, socket-denied network, fake-clock daemon harness, and Playwright browser smoke.
- Release/proof bundle passes secret scan and generated artifact manifest check.
- Memory prune/contradiction/recall eval passes; no stale wrong memory influences decisions.
- Skill promotion manifests, approval manifests, rollback drills, semantic conflicts, and judge-tamper scans pass.
- Alert fatigue/SLO burn report passes; no unresolved Sev1/Sev2 or paging storm remains.
- Trial attempt census, scheduled-eval census, hidden cost ledger, capital events, and signed roots are complete.
- Win-rate is never shown without payoff, expectancy, effective N, LCB, and cost completeness.
- Dashboard/API schema contains no forbidden status words that can be interpreted as live permission.

## Stop/Fail Criteria

- Any live permission true.
- Any secret leak.
- Any promotion pass without edge metrics.
- Any memory/skill mutation without evidence ids.
- Any critical daemon stale/unhealthy.
- Any dashboard API failure.
- Any account ledger/snapshot/scoring reconciliation drift.
- Any stale/missing instrument snapshot on paper trades.
- Any unsigned risk/config/threshold/tool-capability change.
- Any report/dashboard/export wording implying financial advice or live eligibility.
- Any vault Markdown edit changes executable skill/risk behavior.
- Any failed/aborted trial attempt or invalid open is hidden from readiness proof.
- Any private/deleted payload appears after restore or in LLM/vault/export.
- Any canonical A+/setup label is subjective or post-outcome.
- Any shadow readiness row is stale/backfilled/mismatched without fail.
- Any budget exhaustion lacks degraded-mode decision or signed ledger event.
- Any LLM route lacks schema/canary certification after model drift.
- Any chart/tooltip/export mismatch or misleading readiness visualization.
- Any unbounded memory/vector/FTS growth, stale wrong memory still influencing decisions, or missing memory-cost ledger.
- Any promoted skill missing promotion manifest, judge-tamper scan, clean-worktree tests, approval authority, rollback drill, or conflict report.
- Any restore path serves data before replay/erasure/reconciliation/secret/identity checks.
- Any crash cold-start trusts stale latest files.
- Any duplicate/zombie process survives supervisor recovery with writer lease/port/file handle.
- Any phase proof bundle, vault export, or release artifact leaks secrets.
- Any trial report hides failed attempts, scheduled-evaluation gaps, invalid opens, capital events, operating spend, or resets.
- Any event hash-chain root is missing, non-monotonic, or regenerated.
- Any UI/report/API uses forbidden live/readiness terms in status fields or makes WR/green status imply edge without required context.

## Done Gate

NeuroCore v1 can be called complete only if all critical audits pass and residual risks are documented.

## Audit Questions

- What would make this plan fake progress?
- Which metric can still be gamed?
- What would fail after a reboot?
- Can the whole result be reproduced from ledger/events/audit digests without trusting latest files?
- Can Obsidian/Markdown become hidden source of truth?
- Can trial proof be recomputed by an independent script from immutable raw ids?
- Can deleted/private data still appear anywhere?
- Can setup or shadow evidence be gamed by labels, missing candidates, or mismatched fills?
- Can cost/quota exhaustion silently change behavior?
- Can UI make unready evidence look ready?
