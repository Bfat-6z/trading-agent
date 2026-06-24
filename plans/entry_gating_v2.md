# Plan: Entry Gating v2 — Multi-TF Confirmation + Volatility-Normalized

**Author:** Claude (trading agent operator)
**Date:** 2026-05-21
**Status:** PROPOSED (review before implement)

---

## 1. Context

### What triggered this plan

**FIGHT incident, 2026-05-21 09:41**
Bot opened LONG FIGHTUSDT @ $0.005743 (margin $1.50, lev 4x) when:
- 24h gain was +15% (already pumped)
- Range position 88% (at top of day's range)
- Setup classifier tagged as `healthy_momentum_LONG`
- LLM debate flipped bullish (0.55 confidence) after TV analyst added neutral vote
- Risk verdict: `reduce_size` (4.2)

20 minutes later: position closed -$0.204 (-2.62% price move). The trade was a textbook FOMO buy at intraday top.

### Stop-gap already applied (2026-05-21 ~10:15)
Hard gates in `decide_action()` + `_find_executable_cached()`:
- Block LONG if `ch24 > +12%`
- Block SHORT if `ch24 < -15%`

### Why stop-gap is insufficient (honest assessment)

| Problem | Detail |
|---|---|
| Misses real catalyst trades | A coin pumping +20% on Coinbase listing news may continue +30-50%. Hard block kills these. JTO scan example: +31% with real Solana partnership → would be blocked even though valid trade. |
| Threshold arbitrary | +12% means very different things for BTC (extreme) vs meme (normal). No normalization by coin volatility. |
| 24h window too wide | A coin +12% over 24h but flat last 6h ≠ a coin +12% in last 2h. Stop-gap can't tell them apart. |
| Doesn't fix root cause | FIGHT failed due to bad setup classification + missing 15m confirmation. ch24 filter is a coincidental catch. |
| Hard rule, no escape hatch | When 4+ analysts STRONGLY bullish on a catalyst pump → still blocked. Lost EV. |

---

## 2. Root Cause Analysis (FIGHT post-mortem)

### Causal chain
```
[1] Setup classifier tagged ch=+15% rng=88% as "healthy_momentum_LONG"
       │
       │ should have been "exhaustion_SHORT" or "no_setup"
       ▼
[2] Bot launched 17-call pipeline on this candidate
       │
       ▼
[3] LLM analysts saw uptrend signals (EMA trends UP across TFs)
       │
       │ but missed that price was AT TOP of intraday range
       │ and momentum had been distributing (not accumulating)
       ▼
[4] Debate consensus: bullish 0.55  (just over 0.55 threshold)
       │
       ▼
[5] Risk debate: reduce_size 4.2  (not abort — borderline)
       │
       ▼
[6] decide_action() → LONG @ 3x leverage
       │
       ▼
[7] open_position() executed at $0.005743 (near intraday high)
       │
       ▼
[8] Mean reversion → -$0.204 loss
```

### Root causes (ranked)

1. **Setup classifier too lenient** — `healthy_momentum_LONG` range was `3 ≤ ch ≤ 8 AND 0.3 < rng_pos < 0.7`. FIGHT at ch=+15% should NOT have hit this. But it qualified at LATER recheck cycle (when range expanded). Need stricter bounds + reject when at top of range.

2. **No intraday momentum check** — bot used 24h price change but not 1h or 4h velocity. Coin pumping 80% in last 4h is different from pumping +15% spread over 24h.

3. **No multi-TF confirmation requirement** — debate consensus alone isn't enough. Need 15m-1h-4h trend agreement before pulling trigger.

4. **No volatility-normalized extension check** — should check `price vs EMA20 / ATR` to know if entry is statistically extended.

5. **LLM debate threshold too low** — 0.55 strength = barely above 0.50 split. A "borderline bullish" verdict with a `reduce_size` risk is a low-conviction signal, not an execute signal.

---

## 3. Proposed Fix — Layered Gating

### Design philosophy
- **Prefer composable signals over hard kill-switches** (so good catalyst trades aren't killed)
- **Volatility-aware** (each coin's normal vs extreme calibrated)
- **Use existing TV multi-TF data** (already pulled, free)
- **Fail-open when data unavailable** (don't break pipeline)

### Layer 1: TV multi-TF confirmation gate (PRIMARY FIX)

New function `_tv_confirms(tv_data, action)` checked AFTER `decide_action()` returns LONG/SHORT.

**LONG blocked if any:**
- `1h RSI > 75` (intraday overbought)
- `4h RSI > 78` (swing overbought)
- `4h price_vs_ema20_pct > 10` (extended too far above mean)
- `4h MACD histogram negative AND 1h MACD histogram negative` (momentum already turning)
- `1h ADX > 50 AND -DI > +DI` (strong downtrend developing)

**SHORT blocked if any:**
- `1h RSI < 25` (oversold)
- `4h RSI < 22`
- `4h price_vs_ema20_pct < -10` (extended too far below mean)
- `4h MACD hist positive AND 1h MACD hist positive` (uptrend confirmed both TFs)
- `1h ADX > 50 AND +DI > -DI` (strong uptrend developing)

**Fail-open:** If TV data unavailable → return True (pipeline continues, trusts other signals).

### Layer 2: Conviction threshold raise (TIGHTEN)

Current: `consensus_strength >= 0.55` → execute.
New: `consensus_strength >= 0.62` for any execution.

Rationale: 0.55 is barely above the neutral 0.5 line. Asking for ≥0.62 ensures debate had meaningful lean, not a coin flip. Should halve false positives.

### Layer 3: Volatility-normalized momentum gate (replaces hard ch24 cap)

Replace `MAX_LONG_24H_GAIN_PCT = 12` (arbitrary) with **normalized**:

```python
def _momentum_normal(ch24: float, atr_4h_pct: float) -> float:
    """Return ch24 expressed in 4h ATR multiples. Coin-volatility aware."""
    if not atr_4h_pct or atr_4h_pct <= 0:
        return 0
    return ch24 / atr_4h_pct  # e.g., ch +15% / atr 3% = 5x normal daily move

# Block LONG if normalized momentum > +4 (i.e., 24h gain is 4x normal volatility)
# Block SHORT if normalized momentum < -4
```

This makes BTC's +12% (huge in BTC ATR terms) block correctly, while letting meme coin +12% (normal) through.

### Layer 4: Setup classifier sanity check (BUG FIX)

In `scan_futures_movers()`, the bracketing `if -12 ≤ ch ≤ -3 and rng_pos > 0.45 → oversold bounce` etc.

Add explicit reject for `rng_pos > 0.9` (top of day range) on LONG-tagged setups:
- `healthy_momentum_LONG` → require `rng_pos < 0.75` (currently 0.3 < pos < 0.7 but FIGHT slipped through on recheck)
- `momentum_continuation_LONG` → require `rng_pos > 0.6 AND ch < 12` (was no cap)
- Anything with `rng_pos > 0.85` MUST be SHORT candidate or skipped

### Layer 5: Keep ch24 hard caps as safety net (defense in depth)

Don't remove the +12/-15 caps. They're not the primary fix but catch edge cases when Layer 1-4 fail (TV data missing, ATR calc errored, etc.).

---

## 4. Files to Change

| File | Section | Change |
|---|---|---|
| `futures_watch.py` | top config | Add CONVICTION_THRESHOLD = 0.62, MOMENTUM_ATR_MULT_CAP = 4.0 |
| `futures_watch.py` | `decide_action()` | Bump consensus_strength threshold 0.55 → CONVICTION_THRESHOLD |
| `futures_watch.py` | `decide_action()` | Wrap with `_tv_confirms()` check |
| `futures_watch.py` | new function | `_tv_confirms(symbol, action)` calls `fetch_tv_multi_tf` + rules |
| `futures_watch.py` | new function | `_momentum_normal()` + apply in decide_action |
| `futures_watch.py` | `scan_futures_movers()` | Tighten regime bounds, reject `rng_pos > 0.9` for LONG setups |
| `futures_watch.py` | `_watchlist_remember` | Save 4h ATR + 4h RSI in cache for fast re-eval |
| **`feedback_no_counter_momentum.md`** | memory | Update to reflect new layered design (deprecate single ch24 rule) |
| **NEW** `feedback_multi_tf_confirmation.md` | memory | Document Layer 1 + 4 rationale |
| **NEW** `tests/test_entry_gates.py` | unit tests | Mock TV data + verify each gate fires |

---

## 5. Test Cases

Each gate gets a positive (passes) + negative (blocks) case.

| # | Scenario | Expected |
|---|---|---|
| T1 | FIGHT replay: ch24=+15%, 1h RSI=72, 4h RSI=68, ema dist +6% | BLOCKED (Layer 1: 4h not super overbought BUT Layer 4 setup classifier rejects rng_pos 88% → no signal at all) |
| T2 | JTO catalyst: ch24=+31%, 1h RSI=68, 4h RSI=70, ema dist +12% | BLOCKED Layer 1 (4h ema dist > 10%) and Layer 5 (ch24 > 12% hard cap). Acceptable miss given parabolic state. |
| T3 | Healthy LONG: ch24=+5%, 1h RSI=55, 4h RSI=58, ema dist +3% | PASSES — all gates green |
| T4 | BANANAS31 SHORT: ch24=+25%, 1h RSI=77, 4h RSI=81, ema dist +20% | PASSES (SHORT direction, all gates favor SHORT) |
| T5 | Borderline LONG conviction: debate=bullish strength=0.57 | BLOCKED by Layer 2 (0.57 < 0.62 threshold) |
| T6 | TV data unavailable (FIGHTUSDT case) | Pass through to other layers (fail-open Layer 1, still caught by Layer 4 or 5) |
| T7 | Counter-trend perfect: ch24=+30%, 4h RSI=82, BB pos 100%, debate=bearish 0.80 | PASSES SHORT (debate strong, TF confirms exhaustion) — this is BANANAS31 ideal |

---

## 6. Acceptance Criteria

Plan considered "done" when:
- [ ] All 5 layers implemented in `futures_watch.py`
- [ ] All 7 test cases pass via unit tests
- [ ] FIGHT-like log replay (extracted from `state/futures_watch.log` lines 09:39-09:41) results in NO_TRADE
- [ ] BANANAS31-like setup (current +25%, 4h RSI 81) results in SHORT signal
- [ ] JTO-like setup (+31% with bullish debate) is blocked (acceptable trade-off; we don't trade parabolics)
- [ ] No regression on previous winning trades: ALGO (-3% range_pos 50%) and MAGMA (+3% range_pos 55%) would still trigger LONG/SHORT correctly
- [ ] Bot log shows clear `BLOCKED [reason]` audit trail when gate fires
- [ ] Memory files updated: deprecate old single-gate rule, add layered design doc

---

## 7. Rollback Plan

If new gating breaks more trades than it saves:
1. `state/entry_gating_audit.log` records every gate fire with reasoning
2. After 24h: review audit log, count blocked-but-would-have-won vs caught-bad-trades
3. If ratio worse than 1:1 (block 1 good for each 1 bad caught), revert to current ch24 hard caps
4. Revert via `git diff HEAD~1` + manual reapply minimal changes (no git here, but file history preserved)

---

## 8. Out of Scope (future work)

- Funding rate divergence (negative funding = bearish, positive = bullish skew) — Binance API supports
- Liquidation cluster analysis (Coinglass API)
- Volume profile / vwap distance
- Order book imbalance (top-of-book bid vs ask depth)
- Cross-asset correlation (don't open 2 short-the-pump simultaneously)

---

## 9. Estimated effort

- Code: 60-90 min
- Tests: 30 min
- Documentation + memory updates: 15 min
- **Total: ~2-3 hours focused**

---

## 10. Open questions for review

1. **Conviction threshold 0.62**: tight enough? Or should it be 0.65? Trade-off: fewer trades vs higher win rate.
2. **TV failover behavior**: fail-open (current proposal) or fail-closed? Fail-closed = safer but kills trades whenever TV is flaky.
3. **Layer 3 ATR multiplier 4.0**: empirically calibrate or accept this number?
4. **Should Layer 5 (hard ch24 cap) be removed once Layer 1-4 work?**
5. **Apply to cached signals too?** Cached path needs to re-fetch TV data (expensive) or trust cached snapshot.

---

## 11. Implementation order (if approved)

1. Layer 4 first (cheapest, biggest impact on FIGHT-like cases) — tightens classifier
2. Layer 2 next (just change a constant) — fastest improvement
3. Layer 1 (TV confirmation) — biggest engineering, biggest payoff
4. Layer 3 (ATR normalization) — needs ATR plumbing into cache
5. Layer 5 stays in place throughout
6. Tests after layers 1-4 complete

Each layer can ship independently. Bot doesn't need full plan to benefit.
