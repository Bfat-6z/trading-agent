# 15m Scalping Playbook

You get per-coin: EMA20/50/200 + stack label, RSI(14)+state, volume ratio (vs 20-bar avg), MTF trend, and an SMC block (trend/bias, S/R zones + strength, BOS/CHoCH, HH/HL swings) + chart image. Apply these each decision. **Confluence QUALITY beats indicator count** — 3 aligned signals from different families beat 6 from one.

## 1. Trend read (EMA stack + MTF trend)
- Trade only WITH the stack. LONG needs `close>EMA20>EMA50` (ideally `>EMA200`); SHORT mirrors. Tangled/flat stack = no directional edge.
- Demand slope, not just order: rising EMA20 for longs, falling for shorts. Flat EMAs = chop → skip.
- `MTF trend` must agree with the 15m direction. If MTF conflicts or is sideways, downgrade to C-grade or stand aside.
- EMA200 / stack ordering is *regime context*, not a trigger — it lags.

## 2. Location (SMC S/R zones + structure)
- Never long into a strong `resistance zone` or short into a strong `support zone`. Best entries fire OFF a zone, into open space toward the next opposing zone.
- Prefer entries at FRESH/high-strength zones (impulsive origin + caused a BOS). Downgrade zones already tested 2+ times.
- `BOS` = continuation (trade with `trend/bias`); `CHoCH` = only an early reversal *warning* — require a liquidity sweep of the prior swing + close back through structure before acting. Naked CHoCH is the most over-traded, unreliable signal.
- Confirm breaks on candle-BODY close, never wick. Favor break→retest of the flipped level over chasing the initial break.
- `HH/HL` labels confirm uptrend structure (long bias); `LH/LL` confirm downtrend.

## 3. Momentum (RSI + candles + volume)
- Use `RSI(14)` as a 50-midline filter: longs need RSI>50, shorts <50. Do NOT fade RSI>70/<30 in a trend — it stays pinned. Only fade extremes at a tested zone when trend is absent.
- Pullback entry: uptrend buy RSI dip into 40–50 landing on rising EMA20; downtrend sell bounce into 50–60.
- Chart image trigger: require a decisive full-body candle — engulfing (body ≥1.5x prior), pin/rejection wick (≥2x body) at the zone, or sweep-and-reclaim. Ignore dojis/small bodies mid-range.
- `volume ratio` must expand on the trigger/breakout candle (≥1.5x, strong ≥2x). Breakout on sub-average volume = fakeout → skip or fade.

## 4. CONFLUENCE gate
Enter only with **≥3 independent factors from ≥3 categories** (Trend / Location / Momentum-Trigger). Never count RSI+candle+volume as three if they're one read.

**LONG:** `close>EMA20>EMA50` + EMA20 rising + MTF up **AND** at/reclaiming a support zone or BOS-up retest **AND** RSI>50 + bullish trigger candle + volume ≥1.5x.

**SHORT:** `close<EMA20<EMA50` + EMA20 falling + MTF down **AND** at/rejecting a resistance zone or BOS-down retest **AND** RSI<50 + bearish trigger candle + volume ≥1.5x.

## 5. Stand aside (default = NO TRADE)
- Flat/tangled EMA stack, or 15m/MTF trend conflict.
- Price mid-range (middle ~40% between zones); low volume (<avg) / compressed volatility.
- Only a CHoCH with no sweep; wick-only break; already-tapped zone.
- Fewer than 3 independent confluences, or all same family.
- After 2 consecutive stop-outs, stop for the session.

## 6. Stops & R:R (after fees)
- Stop at STRUCTURE ± ~0.3–0.5x ATR: below the demand zone/swing low (long), above supply zone/swing high (short) — beyond obvious equal-highs/lows so sweeps don't clip you.
- Round-trip fees+slippage ≈0.1%; require **net R:R ≥1.5:1** (prefer 2:1) to a REAL zone target. Never widen the target to force the ratio — skip instead.
- Manage: partial at 1R/first zone, move stop to breakeven+fees, trail remainder under each new 15m swing / EMA21. EMA/MACD crosses are context only, never a live exit.