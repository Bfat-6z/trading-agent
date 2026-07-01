# Edge-Research Harness — Design

Goal: a harness where the agent RESEARCHES edge itself — define building blocks,
compose them into parameterized setups, sweep many through prove-or-kill, kill the
losers, keep survivors. Backbone = anti-overfit. PAPER-ONLY, live_guard untouched,
ALLOW_LIVE_ORDERS never set. Build minimal-that-runs first, expand later.

## Steps
- **0 (done):** audited foundation. Fixed train/holdout EMBARGO + added foundation
  tests. No lookahead, costs correct (Phase-2 tiered), split clean. 838 tests pass.
- **1:** `strategy_blocks.py` — no-lookahead vectorized predicate/feature blocks
  (trend/regime/structure/volume/location/direction), each with a no-future test.
- **2:** strategy spec (serializable dict) + `strategy_compiler.py` → signal fn;
  refactor backtest_symbol/simulate_trade to accept an injected signal fn.
- **3:** `sweep_runner.py` — grid × block combos → all specs → run IN-SAMPLE ONLY,
  log every spec + run_id, honest N-trial count. Holdout untouched here.
- **4:** OVERFIT GATE (never bypass): Deflated Sharpe (Bailey & López de Prado,
  adjusted for N trials + skew/kurtosis), purge+embargo, cross-consistency (≥6/9
  symbols AND multiple sub-periods), plateau-not-spike (neighbor params also
  profitable), sealed holdout peeked EXACTLY once for the single best candidate.
  KILL-by-default.
- **5:** first setup family: "short when price rejects EMA cluster from below +
  bearish structure + retest of broken level" (+ symmetric long). Sweep params.
- **6:** report per sweep (full result distribution, N, top DSR, plateau map,
  verdict + reasons) → plans/.../reports/.

## Owner directives (folded in)
1. **TIMEFRAME as a sweep dimension:** test 15m, 1h, 4h. Report which TF has the
   highest DSR. Do NOT pre-exclude 15m (5m died to fees; 15m is near the fee-drag
   zone) — let the overfit gate + Phase-2 cost model decide, no favoritism.
2. **UNIVERSE by objective liquidity:** symbols with 24h quote-volume >= a
   threshold measured at the START of the backtest window (anti-survivorship),
   NOT "hot today". Prefer low-fee high-liquidity majors + liquid alts.
3. **FREQUENCY is an OUTPUT** of the setup's selectivity — never mandate
   "always trade". The demo loop runs always-on and scans continuously, but only
   enters when a setup actually fires.
4. **EXPERIENCE LEDGER:** `state/agent_memory/research_ledger.jsonl` (append-only)
   + `experience_ranked.md` (human-readable). One row per tested setup: spec,
   timeframe, universe, n_trades, win_rate, expectancy (in-sample + holdout),
   profit_factor, DSR, verdict, reason. experience_ranked.md sorted by holdout
   expectancy → DSR, refreshed after each sweep. Primary review artifact.
5. **DEMO ALWAYS-ON:** in parallel, the paper loop runs continuously scanning the
   high-volume universe on the timeframe under test, draws a chart per trade,
   pushes to the dashboard (port 8090) viewable remotely. Paper-only, live_guard
   intact.

## Liquidity-sweep loosening — LOGICAL reasons (disciplined, documented)

Round 1 result: 4 mandatory ANDs (htf_bias + sweep_reversal + structure_shift +
displacement) gave only 2-22 trades at 1h/4h — too few to conclude; the +R seen
is noise (DSR=0). Loosen ONLY where a condition is market-logically redundant,
not to chase green:

- **structure_shift is redundant with sweep_reversal.** A sweep_reversal already
  encodes "price broke a prior level then closed back the opposite side" — a
  structure event. Also requiring a separate BOS >= X*ATR in the same window
  double-counts structure and slashes samples. -> make structure_shift OPTIONAL.
- **displacement is a confirmation, not the hypothesis.** The core claim is
  "sweep a liquidity level -> revert"; a big confirmation candle (range >= Y*ATR)
  is a filter, not the signal. -> make displacement OPTIONAL.
- **Keep htf_bias_po3 + sweep_reversal as the CORE** (the actual hypothesis).

Discipline: every loosened variant is another trial -> DSR penalized via
n_trials_offset (cumulative honest count includes round-1's 128/cell). Goal =
reach thousands of trades at 1h/4h to learn whether round-1's +R is real or
noise, NOT to "make it green". Holdout sealed, peeked once for the final best.
Low-TF (<1h) chart-TA is LOCKED DEAD to fees — not retested.

## Non-negotiables
Paper-only; never touch live_guard; per-step test + commit; adversarial audit
before declaring done; minimal-that-runs over feature-rich; KILL is the normal
result of most sweeps and must be reported honestly.
