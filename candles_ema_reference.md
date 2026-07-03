# Candlestick + EMA Reference (crypto 15m/1h/4h futures)

## Core truth (read first)
No candle or EMA signal is an edge alone. Standalone candle win rates cluster at **45–55% (coin-flip)**; a 502-stock, 136k-event study found candles predicted the next move only ~5.3% of the time. Edge comes from **LOCATION** (a real level to react at), **CONFIRMATION** (next-candle close in the signal direction + volume), and **TIMEFRAME** (4h/1h carry signal; 15m is mostly noise). Screen for the level first, then look for the candle there — never scan candles blindly.

---

## 1. Candlestick patterns

### Single-candle (mostly low reliability)
- **Hammer / bullish pin** — MEDIUM (only at tested support after a downtrend; needs next close above the hammer high). Rejection of lows via long lower wick.
- **Shooting star / bearish pin** — MEDIUM (best single-candle; ~60% *with* confirmation+context, ~53% blind). Only at resistance after uptrend; short only if next candle closes below its low.
- **Hanging man** — LOW (identical to hammer but at a top; long lower wick means buyers still there — needs strong bearish confirmation).
- **Inverted hammer** — LOW (same shape as shooting star; trend context distinguishes them; weak alone).
- **Marubozu** — MEDIUM as a momentum/continuation read (all body, no wick); can flip to exhaustion at S/R.
- **Doji (standard / long-legged / four-price) / Spinning top** — LOW / **NOISE**. Indecision only. Four-price doji is a liquidity artifact — ignore. On 24/7 crypto these print constantly off-hours; never trade alone.
- **Dragonfly / Gravestone doji** — LOW; only meaningful at support (dragonfly) / resistance (gravestone) with a confirming candle.

**Location rule:** Hammer(bottom)=Hanging Man(top) and Inverted Hammer(bottom)=Shooting Star(top) are *identical candles*. The preceding trend assigns the meaning — get trend wrong and you invert the signal.

### Two-candle
- **Bullish / Bearish Engulfing** — MEDIUM, the workhorse. Only *bodies* must engulf (wicks optional; engulfing wicks too = stronger). ~63% to target with volume+follow-through vs ~47% without. Invalidation is precise: bullish fails on close below the engulfing **low**; bearish on close above its **high**.
- **Piercing Line / Dark Cloud Cover** — MEDIUM. Trigger = close beyond the **50% midpoint of the prior body**. Weaker cousins of engulfing.
- **Bullish / Bearish Kicker** — MEDIUM but **rare in crypto** (needs a gap; 24/7 markets barely gap). Real ones appear only around news/thin liquidity and often gap-fill.
- **Tweezer Top / Bottom** — LOW (~55–60%). Matching wicks = double rejection; needs follow-through.
- **Bullish / Bearish Harami** — LOW. Signals a *pause*, not a takeover. Early-warning only.

### Three-candle (stronger — third candle is built-in confirmation)
- **Morning Star / Evening Star** — MEDIUM (~60–72% on 4h/1D). Third candle should close >50% into the first body, on rising volume, at a level.
- **Three White Soldiers / Three Black Crows** — MEDIUM. Reliable but **late-entry trap**: by the third close price has often run 5–15%, crushing R:R to ~1:1. Prefer a pullback entry. Need rising volume; fails if long opposite wicks appear (exhaustion).
- **Three Inside Up/Down** — MEDIUM (= confirmed harami). **Three Outside Up/Down** — MEDIUM (= confirmed engulfing, slightly stronger).
- **Abandoned Baby** — LOW / **UNUSABLE in crypto** (requires gaps on both sides of the doji; 24/7 markets don't gap — it's just a morning/evening star).

### Reliability table (honest)
| Pattern | Reliability | Note |
|---|---|---|
| Engulfing (w/ vol + level) | MEDIUM | Primary two-candle trigger |
| Shooting star / Hammer at level | MEDIUM | Best singles; need confirming close |
| Morning/Evening Star, Soldiers/Crows | MEDIUM | 4h/1D; watch late-entry |
| Three Inside/Outside | MEDIUM | Self-confirming |
| Piercing / Dark Cloud | MEDIUM | 50% body penetration |
| Tweezers, Harami, Marubozu | LOW | Warnings, not entries |
| Doji, Spinning top, Inverted hammer, Hanging man | LOW | Weak/noise alone |
| Four-price doji, Abandoned Baby, gap patterns | NOISE in crypto | Ignore |

**Crypto-specific:** Long "John Wick" wicks = liquidation cascades / stop hunts, not direction. Judge the **body close**, not the wick. A close *back inside* a swept level = fade it (spring/upthrust, ~65–75% *with HTF trend*); a close *firmly beyond* on volume = real breakout. Confirm with **volume ≥1.5× avg**, and check **funding** (which side is over-leveraged) + **OI** (breakout with rising OI = real; falling OI = liquidity grab that reverts). Fakeout rates by TF: 1–5m ~70%, 15m ~60%, 1h ~50%, 4h ~40%, 1D ~30%.

---

## 2. EMA

### Fundamentals
EMA multiplier = 2/(n+1); front-loads recent price → less lag than SMA but more noise. Period map: **8/9** = scalp momentum (noisy, low reliability alone), **20/21** = swing pullback line (MEDIUM), **50** = intermediate trend (MEDIUM), **200** = regime line / institutional (HIGH as a filter). Shorter = earlier + more false signals; all EMAs lag — they confirm, never predict tops/bottoms.

### Crossovers & timeframe
Golden/Death cross (50/200) and dual/triple crosses (9/21, 9/21/55) are **trend-confirmation tools, lagging by design**. Honest hit rates: golden cross fails ~33% over 6mo; death cross continues down only ~57%; false signals up to ~35%. **Biggest failure = ranging markets** (lines braid and whipsaw — StockCharts: 3 whipsaws before one good trade). Crosses only carry edge on **4h/1D** — below 1h they are noise (matches the bot's existing finding). Gate every cross behind a sloped slow EMA / ADX>20; if the slow EMA is flat, don't trade the cross.

### Dynamic S/R
In a trend, price bounces off rising EMAs (shallow dips → 20, deeper → 50; 200 = regime). Reliability scales with period: 20 LOW (whipsaw/trail reference), 50 MEDIUM (institutional "reload"), 200 HIGH. Never trade the bare touch — need a rejection candle + volume + momentum. **Reclaim** (decisive close above + held retest = flip to support) is higher-quality than anticipating the first touch. A **clean slice through on rising volume = reversal, not a bounce**.

### Ribbon / slope / stacking
Read the ribbon as one object — order + spacing + slope must agree. **Bullish stack** = price>20>50>200 all rising (regime = only longs). **Expansion** = momentum (but extreme fanning = over-extension, poor entry). **Compression** = coiling/no-direction (wait for breakout close + volume). **Normalize slope** (% or ATR-adjusted) — raw on-screen angle is meaningless (depends on chart scale).

### Confluence (the actual edge)
EMA sets **direction**, the candle is the **trigger**. Two workhorse setups: (1) **Pullback-to-EMA + rejection candle** (pin/engulfing at 20/50 in a stacked trend) = continuation; (2) **EMA reclaim + bullish engulfing** (green body engulfs prior red AND closes back above key EMA) = potential trend change. Require ≥2 confluences beyond the candle: with-trend EMA slope + real level/VWAP + volume surge (~20–30% above avg) + RSI/MACD agreement. Candle-with-confluence runs ~60–68% vs ~50% naked. **Always wait for the candle CLOSE** — intrabar engulfings/pins reverse constantly on 15m/1h around funding/news. Stop beyond structure (swing or ~0.5×ATR beyond EMA), never *on* the EMA (gets wicked).