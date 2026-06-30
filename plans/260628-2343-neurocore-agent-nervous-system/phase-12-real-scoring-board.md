# Phase 12: Real Scoring Board

## Overview

Replace promotion checklist with canonical edge scoring.

## Related Code

- `promotion_board.py`
- `daily_exam_agent.py`
- `setup_ranker.py`
- `risk_of_ruin_model.py`
- `portfolio_correlation_guard.py`

## Hard Metrics

- PF after fees/funding/slippage.
- Expectancy after fees.
- Expectancy lower bound / uncertainty.
- 95% lower confidence bound by block bootstrap or Bayesian method; method and parameters must be persisted.
- Max drawdown and drawdown path.
- Fee drag percentage.
- MAE/MFE coverage and quality.
- Sample size by setup, side, symbol, regime, source.
- Risk of ruin and correlation/concentration.
- Source trust weighted coverage.
- Effective sample size by setup/regime/source after serial correlation and clustering.
- Portfolio beta/correlation exposure, cluster concentration, and stressed loss.
- Decision-data capability coverage by setup/source: required present, optional stale, blind trades, size-capped trades.
- As-of scoring snapshot: `as_of`, `included_event_seq_max`, `outcome_known_at_max`, report cutoff, immutable snapshot hash.
- Candidate census metrics: seen/ranked/skipped/expired/selected/missed/closed, missed-candidate rate, no-trade opportunity cost.
- Paper-shadow concordance metrics with pre-registered tolerances: fill bps error, timing lag, side/setup/regime parity, feature parity, unmatched fail rate, per-stratum confusion matrix.
- `win_rate` is diagnostic-only. Any display/report must pair it with N, effective N, average win, average loss, payoff ratio, expectancy, expectancy lower bound, invalid opens, and fee/funding/slippage/liquidation completeness.
- Operating-cost-adjusted metrics: paper PnL after execution costs, plus separate spend-adjusted expectancy after LLM/API/data/compute costs. Reports cannot imply economic profitability when spend-adjusted result is negative.
- Universe coverage metrics: excluded/unavailable/not-scanned share, delisted/non-trading participation, scheduled-evaluation census gaps, and survivorship-bias flags.
- Capital-event handling: PF/DD/expectancy windows split at deposits, withdrawals, resets, corrections, and manual rebalances.

## Implementation Steps

1. Create `real_scoring_board.py`.
2. Build trial-partition, rolling 10/25/50/100, and all-time windows from immutable ledger-genesis partitions.
3. Score by setup/regime/source, not only global.
4. Feed promotion board from Real Scoring Board hard gates.
5. Make daily exam average truly rolling, not latest-only.
6. Separate monitoring windows (10/25/50/100) from promotion windows; small windows cannot promote.
7. Add paper-shadow concordance: fill/slippage error, timing lag, symbol/regime parity, feature parity.
8. Add golden scoring fixture from known trade series with expected PF, expectancy, drawdown, CI, effective N.
9. Score by risk buckets: market beta, sector, liquidity tier, symbol, setup, source, side, and capability mask.
10. Add stress-score pack: BTC/ETH crash, alt beta shock, correlation=1, spread x5, funding shock, source outage, margin-asset peg shock.
11. Freeze daily scoring snapshots; late close/backfill creates a new correction snapshot, not silent rewrite.
12. Add incremental rollups keyed by event seq/window/dimension with correction invalidation, query id, and p95 recompute budget.
13. Record metric manifest: formulas, denominators, CI/bootstrap params, inclusion/exclusion rules, severity taxonomy, stress params, and scoring code/config digest.
14. Add setup-version-safe scoring: group by `setup_contract_hash`; cross-version aggregation requires explicit compatibility declaration.
15. Add propensity/off-policy fields so score-driven feeder changes cannot self-confirm by starving alternatives.
16. Readiness metrics use effective N, cluster weights, inverse propensity or conservative off-policy penalty, source-trust weights, and per-day/per-symbol caps; raw counts are diagnostics only.
17. Final score snapshot must wait for `max_horizon + source_lag + funding_settlement_lag`; unresolved labels are censored/failing, not silently omitted.
18. Scoring snapshots must cite `skill_promotion_manifest` ids when a promoted skill affects a window.
19. Readiness/pass summaries must include hidden-cost, universe, capital-event, scheduled-eval, and invalid-open diagnostics beside edge metrics.

## Tests

- High WR but negative expectancy fails.
- PF 1.01 with tiny sample fails uncertainty gate.
- Good global PF but bad leading setup/regime fails.
- Fee/funding omission fails score completeness.
- Lower confidence bound cannot be skipped because sample is small; small sample fails promotion.
- Paper and shadow disagreement blocks readiness.
- Golden trade series produces exact expected scoring payload.
- Leading setup fails if profit only exists in blind/missing-capability trades.
- Stress-score breach blocks readiness even if realized 14-day PF/DD pass.
- Late outcome cannot rewrite prior daily exam/scoring snapshot without correction event.
- Multidimensional scoring uses rollups and avoids O(N) all-history recompute in dashboard/trial paths.
- Metric manifest change invalidates active trial/readiness proof.
- Concordance fails on unmatched/late/shadow-backfilled rows or fill/timing errors above tolerance.
- Old setup version is not scored under a newer contract.
- Feeder policy shift reports propensity and alternative starvation.
- Raw PF/trade count cannot pass readiness when effective N or cluster-weighted uncertainty fails.
- Late funding/close/backfill after final maturity creates correction and can invalidate prior pass.
- Win-rate-only green/pass output fails; WR without payoff/expectancy/effective N/LCB/cost completeness is invalid.
- Spend-adjusted expectancy negative prevents any wording that implies profitable economics.
- Reset/deposit/capital event splits windows and cannot bridge equity/DD curves.
- Historical-universe and scheduled-evaluation gaps are present in readiness scoring output.
- Skill promotion manifest hash mismatch invalidates skill-attributed scoring.

## Done Gate

Readiness cannot pass without positive evidence after costs and uncertainty.

## Audit Questions

- Is the agent actually improving or just trading noise?
- Which setup/regime earns money after all costs?
- Is this edge robust after beta/correlation/capability/stress segmentation?
- Was this score known at the report cutoff or recomputed with late outcomes?
- Are misses/no-trades and shadow mismatches in the denominator?
- Are setup versions comparable or accidentally mixed?
- Are raw counts hiding low effective N or immature labels?
