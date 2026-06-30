# Phase 23: 7-Day Burn-In Trial

## Overview

Run NeuroCore paper-only for 7 days to verify uptime, data quality, and learning throughput.

## Trial Gates

- Signed trial manifest exists before first trial event: `trial_id`, exact UTC start/end, report timezone/cutoff, code/config/schema/metric digest, hotfix policy, abort handling, and failed-attempt retention.
- Trial family registry exists before first attempt: `trial_family_id`, attempt ordinal, abort/fail/pass/inconclusive status, reason, all metrics, all incidents, starting signed root checkpoint, and `attempts_passed/attempts_started` headline.
- Uptime/degraded minutes tracked; critical degraded minutes <= 30/day.
- No live permission violations.
- Event bus lag p95 <= 60 seconds for core events.
- Feature missing rate <= 10% for core features, <= 25% for optional microstructure.
- Paper lifecycle completeness >= 99%.
- Counterfactual eligible coverage >= 70%.
- Experiment jobs/day >= 100 after the swarm is enabled; if swarm not enabled, Phase 23 cannot start.
- Experiment throughput must stay within resource/spend caps; raw jobs/day alone cannot pass burn-in.
- Daily exam uses rolling average over at least 7 daily rows.
- Dashboard healthz success rate >= 99% over local probes.
- Ops noise gates: pages/day, duplicate-alert ratio, false-positive rate, restart incidents/day, unacked incident age, and SLO burn-rate stay below frozen thresholds.
- Trial cost report includes cost per valid trade, cost per accepted skill patch, cost per useful experiment, cache hit/miss, and hidden/unreserved spend count.
- Hotfix policy includes branch/SHA pin, allowed-file allowlist, signed reason, mandatory rerun gates, and abort/continue decision matrix.

## Implementation Steps

1. Freeze schema versions for trial.
1. Freeze config, schema, metric definitions, and code digest for Stage 1/trial gates.
2. Record daily trial report.
3. Run nightly exam and learning homework.
4. Review incidents each morning.
5. Fix only trial-blocking bugs; no risky feature churn.
6. For every incident review, create RCA/action item if Sev1/Sev2 or repeated.
7. Use consecutive calendar window only. Abort/restart creates a retained failed-attempt ledger entry; no deleting bad attempts.
8. Pre-register daily exam rubric, seed, grader version, one signed attempt/day, missed/invalid day = zero, and no same-window outcome evidence.
9. Assign trial roles: trial owner, daily reviewer, hotfix approver, abort authority, incident commander, backup operator, and final report approver.
10. Record starting signed root checkpoint and prove final root chain is monotonic from it.
11. Trial report must show all same-family failed/aborted/inconclusive attempts before any passing attempt summary.

## Tests

- Trial report can be generated from state files.
- Missing day is detected.
- Incident count and degraded minutes included.
- Trial cannot start without signed manifest and fixed UTC window.
- Aborted trial attempt remains visible and counted as failed/incomplete.
- Daily exam rerun is rejected; missed/invalid day scores zero.
- Burn-in report includes spend, quota, cache hit/miss, and useful experiment count within budget.
- Trial report headlines attempt census and cannot hide prior failed/aborted same-family attempts.
- Hotfix outside allowlist or without gate rerun aborts/fails the attempt.
- Starting checkpoint root chains to final root; regenerated or missing roots fail.
- Ops noise/SLO burn gates fail even if uptime and dashboard healthz pass.
- Missing owner/reviewer/approver/abort role invalidates trial manifest.

## Done Gate

System proves it can stay alive, collect clean data, and learn for 7 days.

## Audit Questions

- Did it learn or only run?
- Were paper trades realistic enough to trust?
- Was this one uninterrupted consecutive trial or a cherry-picked attempt?
