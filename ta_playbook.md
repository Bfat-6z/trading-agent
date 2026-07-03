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

## Candlestick + EMA (researched, 102 sources — location+confirmation+timeframe > pattern)
Core truth: standalone candles are ~45-55% coin-flip; edge = LOCATION (a real level) + CONFIRMATION (next-candle close + volume) + TIMEFRAME (4h/1h carry signal, 15m is noise).
1. LOCATION-FIRST GATE: Only act on a candlestick pattern if it prints AT a real level — an SMC zone, BOS/CHoCH level, prior swing high/low, or EMA20/50/200. A pattern in mid-range (no level in `smc_zones` nearby) is noise; skip it regardless of shape. Screen the level first, then look for the candle.
2. CONFIRMATION CLOSE IS MANDATORY: Never enter on the pattern candle itself. Require the NEXT candle to CLOSE beyond the pattern (above the high at a bottom / below the low at a top). Bullish invalidation = close below the trigger candle's low; bearish = close above its high. Use these as the stop, not the EMA touch.
3. VOLUME FILTER: Treat any reversal/breakout candle with volume_ratio < 1.5 as unconfirmed and skip it. A pattern on declining volume 'lacks conviction.' Prefer volume_ratio >= 2.0 for high-conviction fades/breakouts.
4. TIMEFRAME WEIGHTING: Weight 4h and 1h candle/EMA signals heavily; demote 15m to entry-timing only, never the primary signal source. Only take a 15m setup when it agrees with the 4h MTF trend and 4h/1h structure. Do NOT treat standalone 15m patterns as tradable.
5. EMA CROSSES ONLY ON 4h (confirms existing bot finding): Act on golden/death and fast/slow EMA crosses only on 4h, and only when the slow EMA slope is non-flat (directional). On 15m/1h, ignore crosses entirely — they whipsaw. If ema_slope ~0 (flat/ranging), stand aside on all cross logic.
6. STACK ALIGNMENT AS DIRECTION GATE: Only take longs when EMA stack is bullish (price>EMA20>EMA50>EMA200, EMA200 sloping up) and shorts when fully inverted. When the stack is tangled/compressed (no clean order), take no trend trades — this is the #1 EMA failure regime.
7. TWO WORKHORSE CONFLUENCE SETUPS ONLY: (A) Pullback-to-EMA + rejection candle — price retraces to rising EMA20/50 in a stacked trend and prints a pin/engulfing rejection = continuation entry. (B) EMA reclaim + bullish engulfing — body engulfs prior candle AND closes back above a key EMA = trend-change entry. Both require volume_ratio>=1.5 and RSI agreement. Everything else is lower priority.
8. REQUIRE >=2 CONFLUENCES beyond the candle before entry: (1) with-trend EMA slope/stack, (2) a real level (SMC zone / swing / EMA), (3) volume_ratio>=1.5, (4) RSI/MACD agreement. Candle+confluence ~60-68% vs ~50% naked. Fewer than 2 = no trade.
9. JUDGE BODY CLOSE, NOT WICK (crypto liquidation wicks): A long wick spiking past a level then closing back inside = liquidity sweep — fade it in the reversal direction (spring/upthrust), ~65-75% ONLY when aligned with 4h trend, confirmed by a structure shift (BOS/CHoCH) within ~5 bars. A body closing firmly beyond the level on rising volume = real breakout — trade continuation. A wick alone with no close-back-inside is not a signal.
10. MOST SINGLE CANDLES ARE LOW-EDGE — DEPRIORITIZE: Doji (all types), spinning top, inverted hammer, hanging man, harami, tweezers are ~48-55% and near coin-flip alone. Use them only as early warnings to tighten risk or watch for a setup, never as standalone entries. The tradable core is: engulfing, hammer/shooting-star AT a level, and the three-candle star/soldiers families.
11. AVOID THE LATE-ENTRY TRAP on Three White Soldiers / Three Black Crows: by the third candle price has often run 5-15%, giving ~1:1 R:R. Do not chase the third candle — wait for a pullback/retest entry into the EMA instead, or skip.
12. IGNORE GAP-DEPENDENT PATTERNS in crypto: Abandoned Baby, kickers, and classic gapped piercing/dark-cloud rarely form in 24/7 markets — what looks like one is usually a plain morning/evening star. Do not weight them. Four-price doji = liquidity artifact, ignore entirely.
13. STOP PLACEMENT: Place stops beyond structure (pullback swing low/high) OR ~0.5x ATR(14) beyond the EMA, whichever is closer — never exactly ON the EMA (normal noise/stop-hunts wick it out). A clean slice through an EMA on rising volume is a REVERSAL, not a bounce — do not fade it.
14. CRYPTO CONFIRMATION LAYERS: Use funding and OI to grade a candle signal. Extreme positive funding + rejection at resistance strengthens a short; extreme negative funding + hammer/reclaim at support strengthens a long. Breakout candle with rising OI = structurally sound; falling OI = liquidity grab likely to mean-revert — skip or fade. Prime stop-hunt windows: weekends and ~00:00-06:00 UTC thin liquidity.
