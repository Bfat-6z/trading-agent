# Volume & Futures-Signal Reference (crypto perps, 15m/1h/4h)

## Honest reliability ladder (read this first)
Almost every signal below is **confirming/coincident, not predictive**. Ranked by genuine edge:

- **HIGHEST edge:** Price + OI + CVD 4-quadrant regime · Funding *extremes* (z-scored) + OI confluence · Spot-vs-Perp CVD divergence · breakout-BAR volume expansion (RVOL ≥1.5–2x).
- **MEDIUM (confluence only):** RVOL/climax, VWAP band fades & pullbacks (regime-gated), volume-profile POC/VA/LVN, taker delta, liquidation-spike reclaim, long/short *extreme* contrarian, basis/premium crowding.
- **LOW / noise:** OBV, A/D, single-bar divergence, VWAP breakout, naked-POC "80% revisit," mid-range funding, raw OI/price divergence, VPIN, order-book imbalance (spoofable).

Rule that dominates all of them: **regime first (trend vs range), level second, confluence third.** No single stream triggers a trade alone.

---

## VOLUME

**Relative volume (RVOL = vol / 20–50-bar vol MA)** — *high reliability as confirmation.* >1.5–2x = participation confirmed; >3x = event; 3–10x at the END of an extended move = climax/exhaustion; <0.7x = drift/dry-up. Deseasonalize for session time-of-day. It validates a move is real, never its direction.

**Breakout volume confirmation** — *high.* Only trust a level break if the breakout candle prints ≥1.5–2x avg. Bulkowski: bigger breakout volume → bigger *move size* (not better win-rate). Expect a **retest 68–74% of the time**; healthy retest volume DRIES UP (<50% of breakout bar). Retest on *rising* volume that re-enters the range = failed breakout — the single most actionable fakeout tell.

**Climax / exhaustion spike** — *medium.* 3–10x bar after a directional run, ideally closing back off the extreme. The #1 expensive mistake is fading the first spike — it often precedes 1–3 continuation bars. Require price-failure confirmation (next bar fails new extreme). In crypto the real climax engine is a **liquidation cascade + reclaim**.

**Volume dry-up (VDU)** — *medium.* RVOL <0.5–0.7x in a tightening range flags WHERE and readiness, never direction. Trade only the expansion candle (RVOL re-expands >1.5x), never the dry-up itself.

**VWAP (session + anchored + σ bands)** — *medium, regime-dependent.* Above VWAP = intraday bull bias. **±2σ fades work ~60–70% only in confirmed ranges (ADX<20) with a rejection candle**; in trends price rides the band and every fade loses. Trend-pullback bounces off VWAP ~65–70% in trends. **Anchored VWAP** (from a swing or liquidation event) beats session VWAP in 24/7 crypto where the daily reset is arbitrary. Widen band multipliers ~10–15% (2.2σ) for crypto vol. The "institutional magnet" is real only on BTC/ETH.

**Volume profile (POC / VAH-VAL / HVN / LVN)** — *medium, structural not timing.* In ranges: fade VAL/VAH back to POC. Best breakout: **LVN break on ≥1.5x volume → fast travel to next HVN (natural 3:1–5:1 R:R)**. HVNs = take-profit shelves. No exchange returns these — bin klines/aggTrades by price yourself. Skip the "naked POC 80% revisit" stat (unsourced).

**CVD / delta** — *medium alone, high in context.* Absolute value is meaningless; only slope, divergence-at-a-level, and behavior matter. Reset at a session/pivot anchor. **Spot-vs-Perp CVD divergence is the standout (high):** perp CVD ramping into a level with no spot follow-through = leverage-led chase → fade; spot CVD leading = durable. Perp CVD "lies" (short-covers, liquidations register as buy aggression). Use ≥5–15m bars.

**OBV / A/D / MFI / CMF** — *low.* Lagging, close-only, wash-trade-contaminated. Direction + divergence-at-a-tested-level only, never a trigger.

---

## FUTURES SIGNALS

**Funding rate** — *sign = crowding gauge, not a predictor.* Mid-range = noise. **Only z-scored EXTREMES carry probabilistic edge, with terrible timing.** Hard danger flags: sustained >+0.1%/8h (~45% annualized) or aggregate ~30% annualized preceded major flushes (Oct 2025 ~$19B, Dec 2024 BTC crash). Persistent NEGATIVE funding after a selloff = capitulation/bullish tell. Annualize = rate×3×365. Prefer OI-weighted aggregate for market-wide crowding. Never fade the level alone — extremes persist for weeks in trends.

**Open interest (OI)** — *high as context, non-directional alone.* The **4 regimes are the core framework:**
- Rising OI + rising price = new longs (**strongest continuation**)
- Rising OI + falling price = new shorts (continuation down)
- Falling OI + rising price = **short-covering (hollow, don't chase)**
- Falling OI + falling price = long liquidation (exhaustion/bottoming)

Best single use: **breakout validation** — a break with expanding OI is real; flat/falling OI = likely fake. OI + funding = crowding/squeeze lens (fuel × which side). Use ≥15m; 5m is noise. Beware hedging/basis OI (non-directional).

**Long/short ratio** — *medium contrarian, only at extremes.* Distinguish WHO: **Global *account* ratio = retail head-count → FADE extremes; Top-trader *position* ratio = size-weighted big money → follow / watch divergence.** Best signal = retail heavily long WHILE top-trader position ratio short. Hedging contaminates ("whale short" often delta-neutral). Only act when ratio + funding + OI align at a multi-day extreme and start reverting.

**Liquidations** — *heatmap medium (magnets, not destiny); realized prints lagging.* Clusters are TARGETS; the tradeable pattern is **sweep-and-reverse** (price drawn to a heavy cluster → cluster clears, OI drops → snap reversal). Never place your own stop inside a cluster. Displayed intensity is relative/assumed. Funding+OI is the only *leading* liquidation signal.

**Basis / perp premium** — *medium regime gauge.* premium = mark/index − 1; it slightly LEADS funding. Widening positive premium + rising OI = fragile leveraged longs. Dated-futures annualized basis collapse from high contango → deleveraging/tops. Too slow for 15m entries; watch sign-convention.

**Taker aggressor flow** — *medium confluence.* Taker-buy ratio >0.55 with rising price confirms demand; the tell is aggressor side vs price *direction*. Foundation of CVD; same caveats.

---

## Bottom line for this bot
The bot already has the two best raw ingredients (funding_rate, cvd_norm). The **highest-value missing pieces are OI (for the 4-quadrant regime + breakout validation) and long/short ratio (retail contrarian)** — both free from Binance `/futures/data/*`. VWAP is a cheap, purely-derived add for intraday bias/mean-reversion gating. Volume-profile POC and basis are lower priority (POC = compute-heavy structural context; basis largely duplicates funding). Everything must be layered — funding extreme + OI regime + CVD + price at level — never fired standalone.