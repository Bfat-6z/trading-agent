# Phase 24: 14-Day Paper Readiness Trial

## Overview

Run a stricter 14-day paper-only readiness trial. This still does not enable live trading.

## Readiness Gates

| Metric | Requirement |
| --- | --- |
| Paper trades | >= 300 validated, lifecycle >= 99% |
| Shadow closes | >= 1000 fresh and paper-shadow concordance must pass |
| Leading setup samples | >= 50 and effective sample size must pass Real Scoring Board power/MDE rule |
| PF after costs | >= 1.25 overall and > 1.1 in leading setup/regime after fees/funding/slippage/liquidation losses |
| Expectancy after costs | > 0 and 95% lower confidence bound > 0; small sample fails, not waives |
| Max drawdown | <= 15% |
| Daily exam rolling avg | >= 80 |
| Counterfactual coverage | >= 80% eligible |
| Walk-forward | no active patch stale/missing/failed |
| DONT_DO | no critical violation for 7 days |
| LLM council | no unresolved critical blindspot |
| LLM degraded/fallback | no safety-critical degraded route during readiness window |
| Runtime | no critical unhandled incidents |
| Ops SLO | no Sev1/Sev2 unresolved, no SLO burn breach, no paging-fatigue breach, no noisy restart storm |
| Account reconciliation | ledger-derived equity/positions match latest/scoring within decimal precision |
| Instrument registry | 100% paper trades cite fresh instrument snapshot, bracket, and price basis |
| Advice boundary | dashboard/report wording remains paper-only and disclaimer ledger is complete |
| Capability coverage | required setup data present; blind/missing-capability trades cannot prove readiness |
| Stress pack | crash/correlation/spread/funding/source/margin-asset shocks stay within risk limits |
| Shadow partition | readiness shadow set is frozen and not reused from patch discovery/tuning |
| Trial manifest | signed consecutive UTC window; all aborted/failed attempts retained |
| Trial attempt census | all same-family attempts visible; prior failed attempt prevents pass headline unless final status is `trial_inconclusive_continue_paper` with all attempts shown |
| Invalid opens | every paper open counts in PF/DD/expectancy; invalid opens above tolerance fail |
| Metric manifest | formulas, denominators, inclusion rules, CI params, severity taxonomy, stress params frozen |
| Shadow freshness | only online rows within trial window/SLA count; backfills are diagnostics |
| Candidate census | every policy evaluation counted; missed-candidate rate below frozen threshold |
| Setup ontology | all readiness trades cite setup contract hash, quality tier, capability contract, and attribution policy |
| Label maturity | all included labels mature after max horizon/source/funding lag or are censored/failing |
| Walk-forward spec | immutable rolling OOS windows and single-use audit holdout pass |
| Effective samples | raw counts pass only if effective N/cluster/propensity/source-weighted gates pass |
| Cost/quota | no required provider/LLM/data budget exhaustion or uncertified degraded route |
| Operating spend | cost per valid trade, cost per useful experiment, cost per accepted skill patch, and operating-cost-adjusted expectancy reported |
| Scheduled evaluations | full scheduled universe/candidate census present, including prefilter, gaps, rate limits, and not-evaluated rows |
| Signed roots | starting checkpoint root chains monotonically to final proof root |

## Implementation Steps

1. Generate daily readiness report.
2. Freeze promotion thresholds before the trial starts.
3. Lock holdout windows.
4. Reject score changes made after seeing trial outcomes.
5. Produce final pass/fail report.
6. Produce paper-only disclaimer and evidence appendix with every metric window, source snapshot, scoring digest, and approval/audit refs.
7. Freeze patch set, thresholds, shadow partition, scoring code/config digest, and candidate-policy digest before the trial starts.
8. Use immutable trial-genesis ledger; reset during trial fails or creates a separate invalid partition. Report all prior resets.
9. Freeze executable candidate policy and log every candidate/skip with policy hash. Any policy hash change aborts/fails unless a neutral hotfix proves identical decisions.
10. Produce signed proof bundle: trial manifest, event seq ranges, all candidate/open/close ids, inclusion/exclusion table, metric queries, raw hashes, code/env lock, and one-command recompute.
11. Freeze learners read-only for trial evidence: memory promotion, skill patching, threshold changes, scorer changes, and setup relabeling from trial ids are blocked until final report.
12. Pre-register paper-shadow concordance spec: matching keys, tolerances, unmatched policy, latency SLA, per-stratum parity thresholds, and statistical method.
13. Delay final report until label maturity window passes: max setup horizon, source lag, funding settlement lag, and backfill cutoff.
14. Use weighted readiness gates: effective N, cluster weights, inverse propensity/off-policy penalty, source-trust weights, per-day/per-symbol caps.
15. Use output enum only: `trial_failed`, `trial_inconclusive_continue_paper`, or `evidence_recorded_not_permission`. API schema forbids `ready`, `approved`, `eligible`, and `live`.
16. Add trial roles to signed manifest: trial owner, daily reviewer, hotfix approver, abort authority, incident commander, final report approver, backup/restore authority.
17. Freeze hotfix process: branch/SHA pin, allowed-file allowlist, signed reason, mandatory rerun gates, and abort/continue decision matrix.
18. Include all same-family attempts, invalid opens, resets/capital events, scheduled-eval gaps, hidden spend, and ops incidents in the headline summary.
19. Readiness pass is invalid if a same-family prior attempt failed, unless result is explicitly inconclusive with all attempts shown and no live/readiness wording.

## Tests

- Trial report includes fixed threshold snapshot from trial start.
- Score changes after seeing outcomes are rejected or marked invalid.
- Missing daily report fails readiness.
- Pass/fail output keeps live execution disabled.
- Any missing account reconciliation, instrument snapshot, or price basis fails readiness.
- Report cannot output live-eligible wording.
- Blind/missing required capability trades are excluded from readiness proof.
- Stress pack failure fails readiness even when realized PF/DD pass.
- Shadow close used for tuning cannot count toward readiness concordance.
- Reset/history deletion during trial fails readiness.
- Every open trade counts in PF/DD/expectancy even if invalid; invalid count is separate fail metric.
- Candidate policy hash change without neutral proof fails trial.
- Proof bundle recomputes PF/DD/expectancy from raw trial-genesis ledger.
- Shadow rows outside trial/SLA or computed by backfill do not count toward freshness gate.
- Readiness evidence cannot update memory/skills/thresholds before final report.
- Missing candidate census, setup contract hash, or concordance spec fails readiness.
- Immature/censored labels cannot be counted as winners.
- Effective/weighted sample gate can fail even when raw trade count/PF passes.
- Required cost/quota exhaustion invalidates or marks trial inconclusive per degraded-mode matrix.
- API/report schema rejects forbidden words `ready`, `approved`, `eligible`, and `live` in status fields.
- Prior failed/aborted same-family attempt prevents a pass headline and appears in attempt census.
- Scheduled-evaluation census gaps fail readiness.
- Hidden/local/unreserved spend fails or marks inconclusive; spend-adjusted metrics are reported.
- Starting signed checkpoint root and final proof root chain monotonically.
- Reset/deposit/capital event during trial splits or invalidates the partition and is visible in final report.
- Missing trial owner/reviewer/hotfix approver/abort authority/final approver invalidates the manifest.
- Paging fatigue, noisy restart, or SLO burn breach fails runtime gate.

## Done Gate

Output is `trial_failed`, `trial_inconclusive_continue_paper`, or `evidence_recorded_not_permission`. No live execution is enabled here, and no runtime may consume this status as a permission or live-trading trigger. A signed `live_permission=false` field is included in all outputs.

## Audit Questions

- Did it pass because edge improved, or because thresholds drifted?
- Does every passed metric cite data window and evidence ids?
- Could any downstream script interpret the readiness output as permission to trade live?
- Did the trial pass on calm data while failing crash/stress scenarios?
- Did any readiness evidence leak back into memory/skill during the trial?
- Were losers, invalid opens, resets, or aborted attempts excluded from the proof?
- Did shadow evidence reflect the same policy/universe/timing/fill assumptions as paper?
- Did trial evidence tune the learner before the report was sealed?
- Did final readiness wait for labels/costs/funding to mature?
- Is pass based on raw counts or effective weighted evidence?
- Did the final status use only the paper-only enum and avoid forbidden live/readiness wording?
- Were all same-family attempts, costs, scheduled-eval gaps, capital events, and ops pain visible?
