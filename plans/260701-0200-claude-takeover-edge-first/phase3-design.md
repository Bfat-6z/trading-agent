# Phase 3 Design — Chart-based edge, prove-or-kill

Status: DRAFT for owner approval. Direction (owner): edge must come from CHART TA —
candlesticks + moving averages + volume. Built from workflow map+research+design+redteam
(wf_6545f53e-5ba). Red-team found 5 serious "backtest will lie" flaws — all folded in below.

## The setup (deliberately minimal — dodge overfit)

**EMA trend-continuation pullback-reclaim**, 5m closed candles, HTF-gated, ADX-gated, volume-confirmed. Symmetric LONG/SHORT.

**Entry (LONG; SHORT mirrors):**
- Trend: 5m EMA20 > EMA50, AND 1h EMA20 > EMA50 (higher-timeframe agrees).
- Regime: ADX(14) > 25 (only trade when actually trending — skips the ~60% chop where MA whipsaws).
- Pullback-reclaim: prior candle dipped to/below EMA20, current closed candle reclaims above EMA20 and is bullish.
- Volume confirm: volume_ratio ≥ 1.5 (breakout candle has real volume).
- Overextension block: skip if |close−EMA20|/ATR > 2 (don't buy exhausted tops).
- Fail-open: any missing data (volume status not ok, ADX/HTF unavailable) → NO trade.

**Exit:**
- SL = 1.5×ATR (or tighter swing low if valid); reject setup if RR < 1.5.
- TP = 3.0×ATR (forced ~2R so a 35–45% win rate can clear costs).
- Regime exit: EMA20 crosses back under EMA50, or ADX < 20 → exit at close.
- Time stop: 48 bars (4h) — matched to trend thesis (replaces blind 30-min timeout).
- Pessimistic bracket sim: if a candle spans both SL and TP, assume SL hit first.
- All exits costed via paper_cost_model (stop uses is_stop=True); funding charged on 8h crossings.

This reuses the ALREADY-BUILT-BUT-DISCONNECTED chart layer (chart_indicator_engine ema/atr/adx/volume_ratio, chart_trend_regime, chart_setup_scorer trend/HTF-conflict/overextended blockers). Today `chart_used=0` because score_chart_setup is never called from decide_action — Phase 3 fixes that IF it survives the backtest.

## Red-team fixes folded in (MUST do before any run)

1. **Symbol survivorship (fatal):** do NOT pick "hot microcaps" as of today (they survived → look-ahead). Freeze the symbol list from what was liquid at the START of the history window, and include some that later died/delisted. Otherwise the backtest is rigged positive.
2. **HTF 1h leak + harness limitation:** run_decision_time_backtest passes only ONE candle list. The 1h HTF gate must be pre-joined point-in-time (each 5m bar carries the LAST CLOSED 1h EMA state, never the in-progress 1h bar). Build this join carefully or the HTF filter leaks the future.
3. **CI is invalid as specified:** returns/trades are NOT iid (9 correlated symbols trend together; overlapping bars). A plain normal/bootstrap CI will be too narrow → false "lower-95>0". Use block bootstrap by time, and treat correlated simultaneous entries as one bet. Deflated-Sharpe / Bonferroni over the real trial count.
4. **Hidden in-sample trials:** freezing EMA20/50, ADX>25, vol≥1.5, SL1.5/TP3 by inspecting 5 months IS multiple testing. Count every threshold tried; correct for it. Pre-register the ONE parameter set before touching the holdout.
5. **INCONCLUSIVE ≠ re-peek loophole:** "extend history and try again" defeats the one-peek seal. If holdout < 400 trades → INCONCLUSIVE and STOP (no re-peek on the same holdout). Validation must test the SAME function that would deploy (no bait-and-switch between the 4-gate rule and score_chart_setup).

## Backtest protocol (frozen before any run)

- **Symbols:** 9 spanning cost tiers (3 major/3 mid/3 micro), chosen by liquidity AT WINDOW START, incl. some that later died (anti-survivorship).
- **History:** ≥9 months 5m closed candles + aligned 1h, pulled once, cached to a frozen file (deterministic replay).
- **Split:** oldest ~5 months = in-sample (confirm setup fires, FREEZE the single param set). Most recent ~3 months = SEALED holdout, ONE peek only.
- **Min sample:** ≥400 holdout trades AND ≥25/symbol, else INCONCLUSIVE (stop, don't re-peek).
- **Costs:** paper_cost_model pessimistic tiers on every fill; funding on 8h crossings.
- **Metrics (holdout, net of cost):** expectancy + block-bootstrap 95% CI, profit_factor, win_rate, max_drawdown, per-symbol + per-regime breakdown.

## KILL criterion (binding, KILL is the expected default)

KILL (do not wire chart edge into decisions) if ANY: expectancy lower-95%-CI ≤ 0; OR profit_factor < 1.2; OR holdout trades < 400 (INCONCLUSIVE, stop); OR max_drawdown > 25%; OR positive on < 3/9 symbols or < 2 regime windows; OR deflated/Bonferroni t-stat < 3. Research base rate: retail TA is net-negative after costs, and this repo's own scalps sit at expectancy ~−0.10 / PF ~0.85. **Expect KILL. Accept it without parameter fishing.**

## Implementation steps (small, testable)
1. Historical candle fetcher: pull ≥9mo 5m+1h for the frozen symbols, cache to a deterministic file. Test: no-lookahead, gaps reported.
2. Point-in-time HTF join: each 5m bar ← last CLOSED 1h EMA state. Test: no future 1h leak.
3. Deterministic chart signal fn `chart_pullback_reclaim(visible)->signal|None` reusing chart_indicator_engine. Test: fires only when all gates true; fail-open on missing data.
4. Multi-bar bracket PnL with paper_cost_model + SL-first tie-break + funding. Test: costs applied, pessimistic tie-break.
5. Backtest driver over in-sample: confirm it fires, freeze params, count trials.
6. Block-bootstrap CI + deflated-Sharpe. Test on synthetic no-edge data → must NOT show fake edge.
7. ONE sealed-holdout run → apply kill criterion → report edge or KILL.
8. Adversarial audit (Phase 3.5) before any conclusion is trusted.

## Note
No live trading anywhere in Phase 3. This is measurement only. If it passes, wiring into decisions is a later, separately-gated step.

## Owner direction update (2026-07-01)
1. **Optimize NET EXPECTANCY, not win rate.** Owner confirmed: lãi ròng > win rate. Keep the R:R-driven setup (35-45% win, RR 2-3). Do NOT chase high win rate (near TP / far SL) — that is the account-blowup shape. Every candidate still gated by holdout expectancy CI.
2. **Agent must DRAW charts, not just read numbers.** Infra already exists:
   - `chart_snapshot_renderer.py` (matplotlib): renders candles + EMA + volume + overlays (zones/trendlines/structure/SL-TP) to PNG. Reuse.
   - `tradingagents_crypto_src/.../tv_data.py` `fetch_tv_multi_tf` (tvdatafeed): multi-TF TV indicators. Reuse for signal + cross-check.
   - Vision model (Gemini via ai-multimodal skill) can READ a rendered chart PNG.
   Plan: after the deterministic signal fires, the agent RENDERS the setup chart (candles+EMA+volume+SL/TP) as a PNG artifact for every paper decision — so decisions are visual + auditable, and a vision pass can sanity-check the setup. This makes the agent "see" its own charts, learn from rendered snapshots, and lets the owner review the exact chart per trade.
3. **Research chart methodology from pros/whales** (SMC, order blocks, price action, MA pullback) to inform future setups — but every method must survive the same backtest-holdout gate before it can trade. No method trades on reputation.

Sequencing: finish the deterministic prove-or-kill FIRST (it's the honest edge test). The chart-rendering + vision layer is built alongside as the "agent draws its own chart" capability, wired to every decision snapshot. TV methodology research feeds the NEXT candidate setups if the first is killed.
