# Meta-loop — iteration log (durable, in-repo)

The live learnings (`reports/meta_learnings.md`) + sweep logs are gitignored; this
is the tracked record of each meta-loop iteration's honest verdict + what it
LEARNED.

## Iteration 1 (stamped 2026-07-01T17:00Z) — KILL

- **Specs generated:** 156 multi-factor (trigger × regime-gate × filter, each with
  a hypothesis). **Guarded-out by no-lookahead:** 0 (all composed blocks causal).
  **Tested:** 312 (156 × 1h/4h).
- **Global cumulative trial count:** 2510 (baseline 2196 + this iteration's new
  specs). DSR penalty is cumulative — the bar keeps rising, no reset.
- **Verdict: KILL both cells.** meta_1h best +0.090R over 305 trades; meta_4h best
  +0.057R over 326 trades — neither DSR-significant after the 2510-trial
  correction. Sealed holdout never peeked (nothing cleared the in-sample gate).
- **Dry streak:** 1/3.

### LEARNED (Layer 3 — per-component mean in-sample expectancy, ≥100-trade samples)
| component | mean_exp_R | n | note |
|---|---|---|---|
| location_reject_ema_from_below | **+0.050** | 18 | only positive component (short EMA rejection) |
| trend_ema_stack | −0.073 | 60 | |
| regime_adx_min | −0.085 | 38 | trend gate doesn't help |
| ts_momentum | −0.106 | 72 | T3 momentum: negative here |
| vwap_reversion | −0.113 | 80 | |
| bb_reversion | −0.113 | 74 | |
| funding_zscore_fade | −0.165 | 2 | (tiny n) |
| cvd_reversal | −0.211 | 120 | flow filter hurts |
| buy_frac_extreme | −0.227 | 216 | flow filter hurts |
| funding_extreme_contrarian | −0.253 | 286 | flow filter hurts |
| sweep_reversal | **−0.259** | 496 | worst component |

**Data-driven takeaways for the next iteration** (this is the loop learning, not
theatre): sweep_reversal and all order-flow FILTERS systematically drag
expectancy down — drop or de-prioritize them. Trend gates (adx/ema) and momentum
don't rescue anything. The single least-bad building block is
location_reject_ema_from_below (short rejection of the EMA cluster), but at +0.05R
it's economically marginal and under-sampled. No component is close to surviving
the DSR bar.

Consistent with the family verdict: public TA + order-flow has no edge; the
meta-loop now quantifies WHICH components are least-bad, but none pass.

Paper-only; live_guard intact; ALLOW_LIVE_ORDERS never set. 1 pass would be a
CANDIDATE (forward-paper required) — none passed.
