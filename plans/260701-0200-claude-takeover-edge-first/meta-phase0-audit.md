# META Phase 0 — Foundation Audit (before the meta-learning loop)

*4 adversarial lenses in parallel + synthesis (workflow wf_45ad65ef-018).*

## Verdict: **FIX_FIRST** — core mechanics sound, but 4 MAJOR fixes + 3 backbone builds required before the meta-loop.

### Core mechanics are SOUND (empirically proven, no action)
- **Fast path == slow path:** `backtest_with_mask` produces bit-identical trades
  to `backtest_symbol` (66 trades, both dirs, 3 split points, all IDENT).
- **No repaint:** combined multi-block masks (bb + cvd + funding + breakout +
  sweep) bit-identical when recomputed on the series truncated to 0..i (0
  mismatches / 133 checks).
- **Entry at bar i+1 open**, HTF join point-in-time (no in-progress bar leak,
  verified at 1ms boundary), EWM/rolling warmup causal.
- Implication: prior KILL verdicts are NOT invalidated — Family A used positional
  alignment from a single bar source (valid), and the SR0 bug made the gate TOO
  EASY yet everything still KILLed, so those conclusions only get stronger.

## MUST-FIX bugs (before meta-loop)
1. **Order-flow ts_ms misalignment** (`orderflow_data.py:25-27,54,118-142`).
   `_iso_ms(timespec='seconds')` truncates close_time → flow ts_ms (…499999) ≠
   indicator ts_ms (…499000). Length-mismatch join fallback matches nothing →
   CVD/funding all-NaN → spec silently scores "no edge". Positional path copies by
   position with no ts_ms assertion → silent lookahead vector. Fix: canonical
   ts_ms across both fetchers; always join on ts_ms; fail-closed on unmatched.
2. **SR0 uses wrong variance** (`overfit_gate.py:202-210`). `var_trial_sharpe` =
   variance of `expectancy_r` (mean-R), not variance of per-cell Sharpe (mean/std)
   → understates SR0 (~38% for high-win-rate shapes) → gate too easy. Fix: carry
   per-cell (mean,std), take variance of per-cell Sharpe.
3. **daily_exam self-score hard-gates promotion** (`promotion_board.py:30,184-185`).
   A diagnostic self-exam blocks paper→live. Diagnostic-only must not gate. Fix:
   remove `daily_exam_avg` from REQUIREMENTS (or demote to non-blocking).
4. **inner_critic vetoes trades** (`scalp_autotrader.py:787-791`). Diagnostic
   critic filters the trade population on an edge-eval surface. Fix: advisory-only.

## MUST-BUILD backbone (the anti-overfit spine; right after Phase 0)
1. **Global cumulative trial-N ledger auto-fed to DSR.** Sum `n_trials` over all
   `research_ledger.jsonl` rows into `deflated_sharpe_ratio`; retire hand-passed
   `n_trials_offset`; floor effective N>1 (fixes DSR=1.0 at n_trials=1).
2. **Holdout-budget tracker + spec dedup.** Persist which spec_ids peeked the
   holdout (keyed by spec_id + holdout digest); hard-block a second peek and
   near-duplicate specs; dedup on ledger append. (Port from
   `walk_forward_validator.py:387-402`.)
3. **Runtime no-lookahead guard on composed specs.** At `run_family`, auto-recompute
   the compiled mask on truncated 0..i vs full and reject any repainting spec
   BEFORE it counts as a trial. Add a fast==slow regression test + parametrize the
   order-flow block no-lookahead test on an ENRICHED df.
   Plus: Family-A enriched dataset builder as the ONLY path for cvd/funding specs;
   forbid the `precomputed=None` `backtest_symbol` fallback in `peek_holdout_once`
   for order-flow specs (fail-closed).

## Minor (non-blocking)
htf_bias_po3 warmup guard; embargo `break`→`continue`; TF-derived `bars_held`.

Paper-only; live_guard intact; ALLOW_LIVE_ORDERS never set.

---

## RESOLUTION (foundation now clean — ready for the meta-loop)

All 4 MAJOR fixes + all 3 backbone components landed, each with tests; full suite
**921 pass**.

- **Fix 1** — `orderflow_data.enrich_indicator_df` is fail-closed on ts_ms
  (canonical ms ISO; raises on any unmatched bar; positional shortcut removed).
- **Fix 2** — `overfit_gate` DSR SR0 uses per-cell **Sharpe** variance (metrics
  now returns std_r + sharpe); gate is stricter → prior KILLs stand.
- **Fix 3** — `promotion_board` no longer hard-gates on `daily_exam_avg`.
- **Fix 4** — `scalp_autotrader` inner_critic is advisory-only (no veto).
- **Backbone** — `research_governance.py`: (1) global cumulative trial count
  (BASELINE 2196 + Σ per-run `sweep_trials`, no feedback blowup) auto-feeds DSR;
  (2) holdout peek-once budget (`can_peek_holdout`/`record_holdout_peek`); (3)
  runtime no-lookahead/repaint guard on the winning composed spec (rejects a
  leaker before holdout) + fail-closed order-flow holdout. Wired into
  `run_family`.
- A smoke run confirmed the wired pipeline (global count fed DSR, guard ran,
  budget checked) and surfaced+fixed a cumulative-vs-per-run blowup.

**Verdict: CLEAN. Foundation is safe to build the meta-learning loop (Layers 1-4)
on.** Prior KILL verdicts are unaffected (stricter gate, valid alignment).
