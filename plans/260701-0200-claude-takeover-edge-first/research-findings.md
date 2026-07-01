# Edge Research — Findings (round 1)

*Committed summary of the self-research harness output. The live ledger
(`state/agent_memory/research_ledger.jsonl`) + per-sweep reports are local
(gitignored); this file is the durable, in-repo record of conclusions.*

## Verdict so far: NO EDGE FOUND (3 families, all KILL, adequate sample)

KILL is the normal outcome of edge research. Every family below was swept through
the overfit gate (Deflated Sharpe adjusted for N trials + skew/kurtosis, purge,
cross-consistency, plateau). The sealed holdout was NEVER peeked — no family
passed the in-sample gate, so the holdout stays intact for a future real
candidate.

### 1. Chart TA — EMA pullback-reclaim (5m / 15m / 1h / 4h)
- **KILL on every timeframe.** 5m/15m: negative expectancy with ample sample
  (152 / 80+ trades). Root cause: SL≈1.5·ATR on low TF makes round-trip fees
  ~40%+ of risk → tight scalps can't beat fees. 1h/4h: too few trades.
- **LOCKED PERMANENT:** low-TF (<1h) chart-TA is dead to fees. Not retested unless
  a cost-reducing mechanism (maker entries) is added.

### 2. Liquidity Sweep Reversal (SMC/PO3, forced into numeric rules)
- Round 1 (4 mandatory ANDs): only 2–22 trades at 1h/4h — the +0.46R (1h short,
  22 trades) looked promising but was **small-sample noise**.
- Round 2 (disciplined loosen: structure_shift + displacement made optional —
  logically redundant with sweep_reversal; cumulative N=320 with DSR penalty):
  **1026 trades on 1h short → −0.054R.** The round-1 positive was confirmed
  NOISE. All cells KILL, DSR=0.
- **This is the anti-overfit discipline working:** loosening for sample (not for
  green) turned a noisy +0.46R into a real −0.05R.

### 3. Order-flow — CVD + funding (Family A, backtestable subset)
- CVD derived per-bar from kline taker-buy (no aggTrades needed); funding joined
  point-in-time; OI excluded (only 30d history → regime feature at most).
- **KILL, ample sample:** long 1h −0.16R over **2531 trades**; short 1h −0.06R
  (648); short 4h +0.06R (903, DSR=0, not significant); long 4h −0.10R (727).
- CVD + funding as a standalone signal has **no edge** on 1h/4h.

## Forward-only channel (not backtestable)
Order-book imbalance / liquidations / whale flow have NO usable history, so they
can only be measured forward. `forward_test_harness` records real snapshots + tags
matured returns; the clock has started. Needs weeks of wall-clock + MIN_SAMPLE=200
labels before any read-out. Forward-only = higher risk + longer wait, NOT a
shortcut to live.

## Methodology wins (reusable)
- **DSR punishes multiple testing:** noise never passes; more configs → higher bar.
- **pick_best requires ≥300 trades:** stops the gate from evaluating 2-trade
  flukes; forces a statistically meaningful candidate.
- **Disciplined loosening with cumulative trial count:** reveals small-sample
  noise instead of hiding it.
- **Sealed holdout, peeked once:** never burned — still available for a real edge.

## What this means
No tradeable edge yet — expected. The harness now lets us test new hypotheses
cheaply and honestly. Next candidate families (owner's call): mean-reversion
bands, genuine breakout-retest, or CVD/funding as a FILTER on a chart setup
rather than a standalone signal. Everything stays paper-only; live_guard intact;
ALLOW_LIVE_ORDERS never set.
