# Phase 11: Backtest And Walk-Forward Anti-Overfit

## Overview

Prevent skill patches from overfitting one paper window.

## Related Code

- `backtest_harness.py`
- `walk_forward_validator.py`
- `experiment_registry.py`
- `promotion_board.py`

## Implementation Steps

1. Create common adapter using market data lake + simulator assumptions.
2. Prevent lookahead: strategy sees only data up to decision time.
3. Add train/test/holdout windows with purge/embargo.
4. Add walk-forward daemon with heartbeat, stale SLA, review watermark.
5. Add minimum effect size, confidence interval, multiple-test penalty.
6. Define family-wise correction method and alpha budget per `experiment_family_id`.
7. Define embargo length by trade horizon and lock regime labels at decision time.
8. Limit holdout peeking: every holdout evaluation is logged and consumes validation budget.
9. Promotion fails if active patch walk-forward stale/missing/failed.
10. Use hard partitions: discovery/train, validation/test, and frozen audit holdout. Any test/holdout-derived hypothesis contaminates the family and requires a new untouched holdout.
11. Add grouped validation: leave-symbol-out, leave-sector-out, leave-beta-cluster-out, minimum unique days/symbols/regimes, and cluster bootstrap by market episode.
12. Store regime-label cutoff proof and forbid post-trade regime outcomes in decision-time backtests.
13. Freeze shadow partitions, code/config digest, candidate-policy digest, and watermark before readiness; evidence used to tune patches cannot also prove readiness.
14. Define `walk_forward_window_spec`: immutable `window_id`, train/test/audit-holdout start/end, step, mode (`rolling` default for promotion, expanding diagnostics only), calendar/event basis, decision-time basis, label-end basis.
15. Define label interval fields: `label_start_at`, `label_end_at`, `outcome_known_at`, `max_feature_lookback`, `max_source_lag`, and purge any sample whose feature/label interval intersects validation/test/holdout plus embargo.
16. Add per-setup horizon contract: `prediction_horizon`, `max_holding_period`, `label_maturity_delay`; unfinished labels are censored/failing until matured.
17. Add regime distribution manifest: required buckets, min effective N per bucket, max train/test distribution divergence, and explicit inconclusive status when regimes are absent.
18. Add single-use audit holdout registry hidden from dashboard/scoring until sealed; exhausted/peeked holdout pool fails audit use.
19. Default walk-forward/backtest universe is the historical universe-at-time manifest, including delisted/non-trading/unavailable/excluded symbols. Current-active-symbol backtests are diagnostic only.
20. Any scaler, regime labeler, source-trust model, symbol filter, or normalization transform fitted outside the train partition contaminates validation/holdout and invalidates the family.
21. Skill promotion manifests must cite walk-forward ids, window ids, frozen train/test/holdout partitions, and scorer/metric manifest digests used at evaluation time.
22. Rollback rehearsal and forward/backward compatibility proof are required before a migration-backed patch or schema-dependent skill can enter walk-forward.

## Tests

- Strategy cannot inspect future candles.
- Same data cannot both create and prove a skill.
- Stale walk-forward blocks readiness.
- Positive train but negative test fails patch.
- Multiple variants cannot pass by choosing the best false positive.
- Holdout reused repeatedly fails validation.
- Test/holdout-derived hypothesis invalidates that family for final holdout.
- Leave-symbol/sector/beta-cluster validation catches single-cluster overfit.
- Backtest fails if regime label input extends past decision cutoff.
- Shadow partition used for patch tuning cannot be reused as readiness proof.
- Walk-forward window ids and boundaries are immutable and reproducible.
- Train/test/holdout purge removes overlapping feature/label intervals.
- Readiness cannot include immature or unresolved labels as winners.
- Audit holdout cannot be peeked repeatedly or shown in dashboard before seal.
- Current-survivor-only universe cannot produce readiness or promotion evidence.
- Train-only fitted transforms are recomputed and compared; full-history fitted transforms fail leakage checks.
- Skill promotion fails if cited walk-forward ids or metric/scorer digests do not match the candidate artifact.

## Done Gate

Every promoted paper skill has out-of-sample evidence.

## Audit Questions

- Is this a real edge or curve-fit?
- Is walk-forward recent enough?
- Did any validation/audit data influence hypothesis discovery?
- Is this edge independent across symbols/sectors/beta clusters?
- Are windows defined by a spec or chosen after seeing outcomes?
- Are labels mature and embargoed against overlap?
