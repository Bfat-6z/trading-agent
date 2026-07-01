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

## Iteration 2 (stamped 2026-07-01T18:00Z) — KILL (evolved from iter-1 learnings)

Layer 3 -> Layer 2 feedback: `evolve_pools_from_stats` pruned proven-bad
components BY NUMBER (drop_below -0.15):
- **Dropped:** sweep_reversal (-0.259), funding_zscore_fade, cvd_reversal (flow
  filters), AND the EMA-location trigger (see caveat).
- **Kept triggers:** ts_momentum, bb_reversion, breakout_retest.
- **Kept filters:** none, volume_min_ratio (a NON-flow confirmation).
- **Kept gates:** none, regime_adx_min, trend_ema_stack.

Result: 96 specs, 0 guarded-out, **192 tested**. **Global cumulative trial count
2704.** Both cells KILL: meta_1h best +0.080R over **800 trades**; meta_4h +0.057R
over 326 — neither DSR-significant after the 2704-trial correction. Holdout never
peeked. **Dry streak 2/3** (one more dry -> auto-STOP).

### Honest caveat (evolve heuristic limitation)
The owner asked to explore around `location_reject_ema_from_below` (+0.05R, iter-1
least-bad). The auto-evolve DROPPED the EMA-location trigger because it is
direction-specific — SHORT=reject (+0.05) but LONG=reclaim (-0.204) — and the
"all directions must pass" rule killed the whole trigger on the bad LONG mirror.
So location_reject-SHORT was not re-tested this round. This is a real heuristic
gap (a good one-direction trigger shouldn't be dropped for its bad mirror).
HOWEVER: +0.05R at n=18 is economically marginal and cannot clear a 2704-trial
DSR bar regardless, so this is not a missed edge — just an incomplete exploration.
Fix (direction-aware evolve) noted for future; NOT chased now (KILL criterion:
don't grind sub-iterations).

### Cumulative picture (2 iterations + prior families)
Trend (ts_momentum), mean-reversion (bb/vwap), breakout-retest, volume/ADX/trend
gates — all KILL with adequate sample after removing the proven-bad components.
Consistent with the family verdict: public TA + order-flow = no edge. One more dry
iteration triggers auto-STOP -> propose a new source/angle (or lean on the
forward-test order-book channel), not more combos.

## Iteration 3 (stamped 2026-07-01T19:00Z) — KILL -> AUTO-STOP (dry 3/3)

Fresh exploration of NEW parameterizations of the surviving components (longer
momentum lookbacks 30/60, wider BB 30/2.5, looser breakout 40/0.5, milder ADX,
strong-volume filter) — genuinely new spec_ids, not re-tests. 96 specs, 0
guarded-out, 192 tested. **Global cumulative trial count 2896.** Both cells KILL:
meta_1h best +0.124R over 811 trades; meta_4h +0.120R over 395 — the highest
in-sample expectancy seen, still NOT DSR-significant after the 2896-trial
correction. Holdout never peeked.

**Dry streak 3/3 -> AUTO-STOP fired.** The system declared the family exhausted by
the PRE-SET KILL criterion — a disciplined, criterion-driven close, not giving up.

See research-findings.md for the OFFICIAL family-level verdict + the two remaining
(not-yet-run) angles.
