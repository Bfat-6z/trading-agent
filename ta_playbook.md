# 15m–4h Futures Playbook (v2 — rewritten 2026-07-16 from THIS bot's measured results)

You get per-coin: EMA20/50/200 + stack, RSI(14), volume_ratio, ATR%, MTF trend, an SMC block (trend/bias, S/R zones, BOS/CHoCH, HH/HL), `range_ctx` (7d/30d range position), `now` (UTC time/session), funding, cvd_norm, whale flow, and chart images (15m/1h/4h/1d + BTC context).

**Why this doc was rewritten:** the previous version taught breakout-retest-with-volume as the workhorse. This bot's own ledger measured that exact family at **48% thesis-wrong** (its direction reads fail, not its stops), and its lane lab proved **volume expansion carries NO directional information** (long-ignition and short-ignition strategies failed identically). The doctrine below is what the numbers actually support. **When this doc conflicts with the MEASURED MISTAKES / calibration block in your prompt, the measured block wins.**

## 0. PRECEDENCE (read first)
- No setup family is "blessed." If your calibration `thesis_wrong_rate` is high, your ENTRY SELECTION is the leak — not your stops. Fix WHERE and WHEN you enter, not how tight the stop is.
- Skipping is a first-class outcome and the correct answer most cycles. A missed trade costs nothing; a bad entry costs 1R plus fees.

## 1. REGIME FIRST — this is a gate, not a preference
- `regime == 'choppy'` (historical 7% win) OR `wick_intensity >= 0.5` (the measured 75%-SL "rút râu" death zone) → **no discretionary entry.** Not "your judgment" — these are where you bleed.
- Flat/tangled EMA stack (no clean order, EMA20 slope ~0) → no trend trade. The #1 EMA failure regime.
- Compressed ATR / mid-range price with no level nearby → stand aside.

## 2. SWEEP-FIRST PRIOR — the fix for the 48% leak
- **The first break of any clean, obvious, multi-touch level is a liquidity hunt until proven otherwise.** Clean levels exist to be swept. NEVER enter on the breakout candle or the ignition (volume-spike) candle itself — that is the false-break entry the lane lab flagged.
- Require the level to HOLD: a close back through + a 2–3 bar follow-through, OR a sweep-and-reclaim (wick past the level, body closes back inside → fade the sweep in the reversal direction). Sweep-fade at a level aligned with the higher timeframe is a real edge (~65-75%); naked continuation into open space is not.
- `CHoCH` naked = noise; require the sweep + close-back-through before acting.

## 3. WHAT ACTUALLY WORKS HERE (the measured edges — prefer these)
- **Capitulation flush LONG (mean-reversion):** RSI<22 + vol_ratio>=1.8 + price down + OI flat/down = long-liquidation exhaustion. This is the ONE path measured live-positive. (A mechanical path already fires the purest version — your job is the flushes it is unsure about, at a real support level.)
- **Fade-the-bounce SHORT in bear tape:** in a `dumping` tide, sell the low-volume bounce into resistance / a lower-high, not the breakdown continuation. Short/mean-reversion families are the bot's positive lane pockets.
- **OI 4-quadrant (the one legacy piece that matched measurement):** continuation LONG only if price up + OI up; continuation SHORT only if price down + OI up. Price up + OI DOWN = short-covering, hollow → take profit, don't chase. Price down + OI DOWN = exhaustion → hunt mean-rev longs, never fresh shorts.
- **Extension gate on ANY with-trend entry:** skip if `px_vs_ema20_pct` far extended, RSI>68, or price already at a range extreme. Buy stacks only on the FIRST pullback to a rising EMA — this (atr% + distance-from-EMA200) is the cleanest measured winner/loser separator in the lab.

## 4. WHAT FAILS HERE (measured — do NOT do these)
- Buying a "breakout" when `range_ctx.d30.pos_pct` is already ~85-100 (pinned to the 30-day high) — the literal top of the range is where longs get trapped. Same for shorting the 30d low.
- Treating a volume spike as directional conviction (JPM was bought on a 10x spike into a top). Volume confirms PARTICIPATION, never direction. A >=5x spike on a TradFi perp = a news/open print → skip.
- Oversold-continuation shorts (RSI<40 / already dumped hard) — median 2-3 bar stop-outs.
- Textbook BOS/retest continuation in chop or at a range extreme — the exact 48%-thesis-wrong cluster.

## 5. ENTRY MECHANICS — resolve the limit-vs-confirmation conflict
- A resting LIMIT is allowed ONLY at a pre-validated level where a sweep+reclaim already printed this session, or at a fresh support/resistance zone you expect price to pull back INTO. A limit resting under obvious equal-lows fills preferentially on the sweeps that keep going (adverse selection) — don't.
- Otherwise require a CONFIRMATION CLOSE: the next candle closes beyond the trigger; never the pattern candle itself, never on a wick. If you can't wait for the close, you don't have the setup.
- Do NOT set a limit INTO the ignition/volume bar — that is entering the false break.

## 6. RANGE + LOCATION (use `range_ctx` + the 1d chart)
- Default: LONGs in the LOWER half of the 7d/30d range off support, SHORTs in the UPPER half into resistance. A with-trend entry AT a range extreme is the exception and needs the level to have already held on a retest.
- Never long into a strong overhead resistance zone or short into strong support. Best entries fire OFF a zone into open space toward the next opposing zone.
- Weight 4h/1h/1d structure heavily; 15m is entry-timing only. A 15m setup fighting the 1h/4h trend is a trap.

## 7. TRADFI PERPS (NVDA/TSLA/META/JPM/XAU/… — 22 tokenized-stock/commodity markets)
- These are NOT 24/7 crypto. They gap at the cash open and jump on earnings/data. Trade them ONLY when `now.us_equity_open` is true; outside cash hours and on weekends their candles are thin/synthetic — no breakouts, no continuation.
- A giant volume/range bar on a TradFi perp at the open = a gap/news print, not conviction — do not chase it. Prefer standing flat through earnings windows entirely.
- Their lane cohort was the single largest historical loss bucket — treat as low-trust until they build their own positive record.

## 8. STOPS & R:R (after fees)
- Stop at STRUCTURE ± ~0.5x ATR: below the demand zone/swing low (long) / above supply zone/swing high (short), BEYOND obvious equal-highs/lows and known liquidation clusters (you get swept before the move otherwise). Never place the stop exactly on an EMA.
- Round-trip fees ≈0.1%; require net R:R to a REAL zone target that is REACHABLE — many lanes died from a fixed far TP that never printed. Prefer a reachable ~0.8-1R target or a structure trail. Never widen the target to force the ratio; skip instead.
- Manage: partial / BE at first zone, trail under each new higher-TF swing. A clean high-volume slice THROUGH your level is a reversal — exit, don't average.

## 9. CONFLUENCE
- Require **≥3 aligned streams from different families** — {regime OK, real level, HTF-trend agreement, OI/funding, sweep-reclaim or confirmation close, range location} — pointing the same way. RSI+candle+volume from one read is ONE stream, not three. Fewer than 3, or all one family = no trade. Log which fired.
