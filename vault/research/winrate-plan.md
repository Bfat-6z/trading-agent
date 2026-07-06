---
tags: [research, winrate]
---

# Winrate Levers (10-agent, 2026-07-06)

## Ranked Plan

# WINRATE ACTION PLAN — capitulation_long (rsi14<22 & vol_ratio>1.8, SL1/TP6/48-bar, lockbox +0.72R p=0.009)

**Governing rule (all four briefs converge):** win rate rises legitimately only by *deleting losing fires*; it rises fraudulently by *shrinking winners*. Every lever below is accepted only if lockbox expectancy stays ≥ +0.72R (or within CI) through the existing grid+purged-split+lockbox pipeline. Breakeven WR math: SL1/TP6 needs 14.3%; TP3 needs 25%; TP1.5 needs 40% (crosstrade.io, tradezella.com).

---

## (1) TOP-8 RANKED LEVERS

| # | Lever | What | Expected WR impact | EV risk | Effort |
|---|-------|------|--------------------|---------|--------|
| 1 | **Per-fire outcome + MFE/MAE logging** | Log every historical fire: barrier hit (TP/SL/timeout), MFE/MAE in R, bars-to-extreme. One table answers every question below. | 0 directly; enables everything | None | **S** |
| 2 | **Single-condition veto grid ("poor-man's meta-label")** | AND-filters over fires using existing DSL feats: `ema4h_state==1`, `close_pos>0.3` (trigger bar reclaimed), cap `dd_from_high96_pct` (skip terminal dumps — matches falling-knife lesson), `funding_z` extremes. Run through grid+lockbox as capitulation-family conditioning (passes novelty gate). | +5–10pts if losers cluster in vetoed buckets; regime filters demonstrably delete losing oversold-bounce entries (coinquant.ai, quant-signals.com) | Low — fires removed, exits untouched; risk is only power loss (smaller n) | **S–M** |
| 3 | **BTC-freefall veto (bucket split, fail-open)** | Add btc_* columns (spec in §4), test `btc_ret5 >= -1.5%` and vol-normalized `btc_ret5/btc_atr_pct >= -1.0`. Alt RSI<22 during BTC knife = beta, not idiosyncratic capitulation (NARDL asymmetric spillover; alt beta >1, canary.capital). Artemis regime gating: Sharpe 0.50→1.31, DD −66.6%→−27.2%. | +5–10pts if market-wide-beta fires are the loser cluster (likely per spillover lit) | Low — but evaluate as bucket split, only exclude buckets *demonstrably* negative; gate halves N | **M** |
| 4 | **Idiosyncratic-dump discriminator** | `dd96_pct − btc_dd96_pct` (coin dumped far more than BTC). Brief flags this as likely the strongest single WR discriminator per beta-spillover literature. Trivial once #3 columns exist. | Potentially the biggest single-feature lift | Low | **S** (after #3) |
| 5 | **Meta-label logistic filter** | L1/L2 logistic on ≤5–7 features (see §2), purged CV, veto below threshold. Threshold sweep: keep only where OOF expectancy ≥ +0.72R; must beat "remove random 30% of fires" or kill. | Literature: precision/F1 lift confirmed (Hudson & Thames JFDS); Sharpe 0.36→0.74 in published examples | Med — overfit at small n; bounded because it only gates, never picks side (de Prado/GARP) | **M** |
| 6 | **Time-stop grid: 24/36/48/64 bars** | If MFE table shows winners resolve by ~bar 24, shorter timeout converts drifting timeout-losers into freed capital. QS: time exits are underrated, low curve-fit risk. | ~Neutral WR, EV-positive; may nudge WR up if timeout-exits currently book small red | Low | **S** |
| 7 | **Exit-on-strength variant** | Replace fixed 6% TP with "first close > prior bar high after ≥N bars" or close > ema20 (mean-touch). Only exit family with published positive MR backtests (QuantifiedStrategies QS Exit) — but stocks/daily, must re-validate. | Likely largest *optical* WR jump (more trades close green) | **Med-High** — reshapes R distribution; adopt only if lockbox R holds within CI | **M** |
| 8 | **Owner-reporting reframe: precommit streak math** | Every summary reports WR + expectancy(R) + expected-max-streak. At ~25% WR: expected max streak ≈ ln(100)/ln(1.333) ≈ **16 straight losers per 100 trades = normal operation**, erased by ~3 winners. Precommit so a 10-loss run doesn't trigger mid-sample parameter panic. | 0 statistical; kills the pressure that produces EV-destroying changes | None | **S** |

Sequencing: 1 → 2 → (3+4) → 6 in parallel → 5 → 7. Lever 8 immediately.

---

## (2) META-LABELING VERDICT: FEASIBLE, WITH HARD CONSTRAINTS

**Yes — but only as regularized logistic, and only after levers 2–4.**
- Binding constraint is events-per-variable: ~10 minority-class events per parameter (clinical-stats sims support 5–20; PMC6710621). At ~300 fires × ~25% WR ≈ 75 winners → **cap ~7 parameters. No gradient boosting, no nets, no ensembles** (Thumm/Barucca needs 10x our data).
- Feature shortlist (regime/context, orthogonal to trigger, per H&T design): `atr_pct`, `ema4h_state`, `funding_z`, `dd_from_high96_pct`, `close_pos` — plus `btc_ema4h_state` once §4 lands.
- Label with production exits exactly (SL1/TP6/48) or the filter learns the wrong game.
- Treat output as **ranking, not calibrated probability**, until n>1,000 fires. Sweep veto threshold on purged out-of-fold predictions; report fires-removed vs losers-removed; require expectancy ≥ +0.72R at chosen threshold; kill if it can't beat random-30%-removal.
- Favorable setup: our primary is rule-based (the case where meta-labeling works); the QuantConnect failure mode (ML-on-ML) doesn't apply.
- Legitimacy: this is conditioning on the validated capitulation family, not a new graveyard method — passes the novelty gate per Meyer/Joubert architecture.
- **If a single-condition veto (lever 2) already lifts WR with EV intact, the logistic may be unnecessary.** Weekend job either way, not a research blocker.

---

## (3) EXIT CHANGES: GRID-TEST vs DO-NOT-TOUCH

**Test in the grid:**
1. **Time-stop 24/36/48/64** — cheapest, lowest curve-fit risk, decided by MFE bars-to-extreme distribution.
2. **Exit-on-strength** (close > prior high after ≥N bars; close > ema20) — the one exit family with published MR support; strict acceptance: lockbox R within CI of +0.72R.
3. **Partial ladder (LOW priority, conditional):** 50% at +2%, runner to 6% — *only if* MAE/MFE table shows a material cluster of trades reaching +2% then reversing to SL. Default expectation is harm: blended-R math says partials always cut EV unless they dodge late reversals (Metriclan; Mabe's backtest: "almost half of total profit evaporates"). Accept only if total R ≥ incumbent.

**Do NOT test (evidence against, or pure EV trap):**
- **Breakeven-stop-at-1R** — zero quantified evidence anywhere (folklore-grade journaling blogs only); on 15m capitulation longs post-entry chop revisits entry routinely → converts winners to scratches, guts the +0.72R.
- **Tightening SL below 1%** — Kaminski & Lo (J. Fin. Markets 2014): stop rules *subtract* EV from mean-reversion strategies specifically; stops are already a cost center for MR.
- **Nearer TP (3%) as a standalone WR play** — doubles WR optically while halving R/win; breakeven WR jumps 14.3%→25%. Exactly the trap the owner flagged.
- **Blanket trailing stops** — QS: complex exits don't consistently beat simple ones for MR.
- **SQN-maximizing exit optimization** — structurally biased toward TP truncation (Wealth-Lab critique).

---

## (4) BTC-REGIME FEATURE SPEC — exact columns

Zero new infra: run the existing per-coin feature code on the BTCUSDT 15m frame, prefix `btc_`, join every coin frame on bar timestamp. Names mirror the canonical DSL feats confirmed in `E:\keo-moi-mail\trading-agent\method_canonical.py` (ret5, ret20, px_vs_ema200, ema4h_state, atr_pct, dd96_pct, bar_z):

| Column | Definition | Role |
|--------|-----------|------|
| `btc_ret5` | BTC 5-bar return | Primary freefall veto: `btc_ret5 >= -1.5%` |
| `btc_ret20` | BTC 20-bar return | Slower drift context |
| `btc_atr_pct` | BTC ATR% | Normalizer: `btc_ret5/btc_atr_pct >= -1.0` |
| `btc_ema4h_state` | BTC 4h trend up/down | Coarse regime split (mirrors daily-200MA evidence) |
| `btc_px_vs_ema20` / `btc_px_vs_ema50` / `btc_px_vs_ema200` | BTC price vs EMAs | Macro trend location |
| `btc_dd96_pct` | BTC drawdown from 96-bar high | Input to idio-dump feature |
| `btc_bar_z` | BTC bar z-score | Shock detection at fire bar |
| **Derived:** `idio_dump = dd96_pct - btc_dd96_pct` | Coin's excess drawdown over BTC | Strongest expected WR discriminator |
| **Optional:** `breadth_neg20` | Fraction of universe with ret20<0 (from frames already held) | Cheap market-stress proxy |

**Evaluation protocol:** bucket-split historical fires by btc state → lockbox expectancy per bucket → exclude only significantly-negative buckets. **Fail-open** (missing BTC data ⇒ fire allowed), per our gating principles. **Skip:** HMM/K-means regimes (overfit bait at our n; `btc_px_vs_ema200`+`btc_ema4h_state` ≈ same partition free), on-chain composites (wrong cadence), Fear&Greed (daily folklore), Coinglass liquidation feeds (`funding_z` already proxies crowding).

---

## (5) HARD WARNINGS — where chasing WR burns EV

1. **WR is not the objective function.** 90% WR with $5 wins/$50 losses = −$0.50/trade; our ~25% WR at b≈6 gives Kelly f* ≈ +12% (healthy); a 75% WR at 0.25R gives Kelly **−0.25 — unbet-able**. High WR with truncated R flips Kelly negative fast.
2. **Every WR lever that touches exits is a suspected TP-cut in disguise** — partials, nearer TP, BE stops, tight trails all raise breakeven WR silently. Reject any config whose lockbox R/trade drops below +0.72R *regardless of WR gain*.
3. **Every filter shrinks n.** p=0.009 exists at current n; a gate that halves fires can push lockbox p over 0.05 even when genuinely good. Always report fires-removed vs losers-removed, and verify significance survives.
4. **A filter that raises WR but cuts total R is cosmetic** — acceptable only if it holds R while cutting variance.
5. **Loss streaks are the system working.** ~16-loss max streak per 100 trades (~−16% at 1% SL) is expected under +0.72R/trade. Parameter changes triggered by streaks are how MR systems die (loss-aversion mechanism: traders mangle R:R until they need 65–75% WR to break even).
6. **High-WR = negative skew**: months of small wins, then one erasure day. Our positive-skew profile is psychologically ugly and statistically robust — reframe, don't re-engineer.
7. **Size discipline:** stay ≤¼ Kelly (~3% risk equivalent max; 1% SL already sub-Kelly-safe). Never size up to "make back" a streak.
8. **Skip entirely:** 90%-WR strategy shopping, deep-learning meta-models at n≈300, scale-out "free trade" folklore, any standalone new-entry idea already in the 460-idea graveyard.

Key sources: Kaminski & Lo 2014 (sciencedirect.com/science/article/abs/pii/S138641811300030X); Hudson & Thames meta-labeling efficacy (hudsonthames.org); de Prado/GARP; EPV literature (PMC6710621); Artemis BTC regime gating (research.artemis.ai); NARDL BTC→alt spillover (sciencedirect S1544612319310311); QuantifiedStrategies exit backtests; Metriclan/Mabe partial-TP math.

## Insights

- [entry_confirm] Bulkowski candlestick data: reversal patterns WITHOUT a follow-through bar fail ~60% of the time vs ~30% WITH confirmation; the confirmation bar costs ~1% of the move — i.e., confirmation roughly halves false fires at a modest entry-price cost. Directly supports adding bar-quality conds to capitulation_long instead of new entries. https://thepatternsite.com/Hammer.html and https://www.tradingsim.com/blog/6-best-bullish-candlestick-patterns
- [entry_confirm] Hammer anatomy = in-bar confirmation: Bulkowski measures 60.3% bullish-reversal success for hammers confirmed by close near range high; hammers near yearly lows perform best. In our DSL this is close_pos>=0.6 on the flush bar — a wide-range, high-volume bar that CLOSES strong means the reclaim already happened inside the bar, no extra bar of delay needed. https://thepatternsite.com/Hammer.html
- [entry_confirm] Internal Bar Strength (IBS = (C-L)/(H-L), our close_pos) is one of the most robust published mean-reversion features (NAAIM paper + 30yr backtests on indices): the close's position within the bar's range carries real next-period return information — using it as a confirmation feature is evidence-backed, unlike most candlestick folklore. https://www.naaim.org/wp-content/uploads/2014/04/00V_Alexander_Pagonidis_The-IBS-Effect-Mean-Reversion-in-Equity-ETFs-1.pdf and https://www.quantifiedstrategies.com/ibs-internal-bar-strength-indicator-strategies/
- [entry_confirm] Macroption on RSI confirmation: waiting for RSI to leave the oversold zone filters out 'small correction then trend continues' cases and improves win percentage, but enters later/misses the fastest bounces — the canonical WR-vs-entry-price tradeoff. Our analog without a new feature: require streak_up>=1 (first green closed bar) while rsi14 is STILL <22, so we get the turn without waiting for RSI to fully exit. https://www.macroption.com/rsi-overbought-oversold-confirmation/
- [entry_confirm] Sweep/reclaim literature (SMC) consistently says don't enter on the wick — wait for reclaim of the swept level; claimed ~60% WR at 1:2 RR. Our graveyard shows standalone sweep-reclaim is dead, but the CONDITIONING idea survives translation: exclude entries where the trigger bar itself is still a multi-sigma dump (bar_z very negative = mid-flush, knife still falling). https://dailypriceaction.com/blog/liquidity-sweep-reversals/
- [entry_confirm] Capitulation definition from volume literature: true selling climax = volume >=2x (often 3x) average PLUS long lower wick with close near range high, resolving within 1-3 sessions. Our vol_ratio>1.8 captures the volume half only; the close-location half is the missing filter and is exactly close_pos. https://www.tradingsim.com/blog/capitulate
- [entry_confirm] Deeply negative perp funding has coincided with every major BTC local bottom (Nov-2022 $15.5k, Mar-2020 $3.8k; longest negative streak since 2022 flagged as bottom signal Apr-2026). funding_z<=-1 on top of price capitulation = positioning capitulation confirming price capitulation — orthogonal to all price/volume conds. https://www.coindesk.com/markets/2026/04/16/bitcoin-funding-rates-hit-most-negative-since-2023-history-suggests-bottom-is-in
- [entry_confirm] Anti-goal warning: a 66.3% win-rate RSI oversold scalp still returned -16.9% over 6 months of 15m BTC data — win rate bought via nearer targets/looser stops destroys expectancy. Any wr_conf_* variant with a shortened TP must beat the +0.72R lockbox on EV, not just WR; filter-type variants (same 1%/6% exits, fewer bad fires) are the safe path. https://www.coinquant.ai/blog/crypto-scalping-strategy-backtested-6-months-of-15-minute-data-on-btc
- [entry_confirm] Signal-count cost is real and large: published confirmation filters routinely cut the signal universe 50-70% (e.g., a location filter cutting signals 70% while 'dramatically' raising bounce success). Expect wr_conf_* variants to fire far less than base capitulation_long — evaluate on per-trade expectancy and keep the base method running in parallel for sample size. https://www.tradingsim.com/blog/relative-strength-index-rsi
- [regime_filter] MR failure is regime-driven: markets mean-revert ~60-70% of the time but the trending 30-40% produces the catastrophic losses; regime detection before entry (not wider stops) is what preserves expectancy. https://setupalpha.com/blogs/articles/mean-reversion-strategy-failures-complete-fix-guide
- [regime_filter] Long-MA trend filters are the proven win-rate raiser for oversold dips: Connors RSI(2) restricted to price above the 200-day MA hit 70-85% win rates in equities and 62-68% in BTC adaptations; the identical signal below the MA is a falling knife. Critically, using the MA as an EXIT made results worse -- the trend filter belongs at entry only. https://www.quantifiedstrategies.com/rsi-2-strategy/
- [regime_filter] Volatility clusters (GARCH): an ATR explosion signals a shift from range regime to trend/crash regime where tight-stop mean reversion systematically fails; high vol today predicts high vol tomorrow, so capitulation fires during already-elevated ATR face continuation risk, not bounce. https://www.daytrading.com/volatility-clustering
- [regime_filter] Deeply negative perp funding = crowded shorts = squeeze fuel for longs: sustained negative funding has preceded every major relief rally in BTC history; capitulation prints where shorts are paying longs are the highest-quality bounce context, while capitulation with still-positive funding means longs haven't been flushed yet. https://www.coindesk.com/markets/2026/04/16/bitcoin-funding-rates-hit-most-negative-since-2023-history-suggests-bottom-is-in
- [regime_filter] Drawdown depth separates washout from knife: a heavy-volume capitulation near a prior low is a bounce candidate, but a meaningfully lower low after an extended multi-day slide is lower-low continuation; depth-of-decline filters (reject fires deep into a 96-bar drawdown) remove the worst-losing fires. https://atas.net/blog/catching-falling-knives/
- [regime_filter] Liquidation cascades end on volume climax WITH absorption: exhaustion shows as a vertical volume spike where price closes off its lows (buyers absorbing forced sells); fading the climax after absorption is the liquidity-provider trade, while fading a bar that closes at its low is fading mid-cascade. https://medium.com/@XT_com/liquidation-cascades-in-altcoin-futures-trading-how-advanced-traders-anticipate-and-profit-from-946b6b84a636
- [regime_filter] Short-term reversal returns are liquidity-provision rents that concentrate in the INTRADAY component and during stress when intermediaries are constrained; overnight/drift moves do not revert -- supporting bar-level capitulation triggers but implying the panic must be sharp and local (ret5 extreme) rather than slow grind (ret20 extreme). https://quantpedia.com/strategies/short-term-reversal-in-stocks
- [regime_filter] Raw RSI-oversold is measurably dead standalone (confirming our graveyard), but adding exactly two regime filters -- a trend filter plus a volatility-regime gate -- flipped a failing RSI system positive at the cost of far fewer signals; selectivity via regime conditioning, not threshold tweaking, is the mechanism. https://quant-signals.com/rsi-trading-strategy/
- [flush_anatomy] Volume climax magnitude: trading literature consistently defines a true capitulation climax as volume 3-5x average, not merely elevated; our vol_ratio>1.8 base likely admits ordinary red bars — a vol_ratio>=3 tier should isolate 'final seller' flushes and cut losing fires. Source: https://www.kucoin.com/blog/what-is-capitulation
- [flush_anatomy] Close location is the single-bar tell: bottoms that hold show a long lower wick with price 'quickly bought back' (smart money absorbing), while dead-cat entries close at/near the bar low and keep falling. Our close_pos(0..1) directly encodes this — requiring close_pos>=0.4 on the flush bar is a confirmation filter, not a rebrand. Sources: https://www.tradingsim.com/blog/capitulate and https://www.kucoin.com/blog/what-is-capitulation
- [flush_anatomy] Trend-regime filter is the biggest documented win-rate lever for oversold buying: Connors RSI(2) only reaches 75-80% WR because entries are restricted to price above the 200-day MA — the same oversold signal below the MA is the textbook falling knife. Analog for us: px_vs_ema200>0 or ema4h_state==1 gating on capitulation fires. Sources: https://www.quantifiedstrategies.com/rsi-trading-strategy/ and https://stratbase.ai/en/blog/rsi-2-strategy-larry-connors
- [flush_anatomy] Streak exhaustion is quantified: 3-4 consecutive down closes before entry yields 65-78% win rates in index/ETF mean-reversion backtests (SPY 3-days-down overnight = 65% WR; SMH = 78% WR, 121 trades). streak_down>=3 as an AND condition selects flushes at the END of a slide rather than mid-cascade. Source: https://www.quantifiedstrategies.com/3-days-down-overnight-trading-strategy/
- [flush_anatomy] Overreaction magnitude scales the reversal: academic work on crypto overreactions finds larger initial negative moves produce larger subsequent reversals, and crypto reversal trading survives fees where the S&P equivalent does not — supporting depth conditions (bar_z extreme, deep dd) rather than shallow-dip entries. Sources: https://www.sciencedirect.com/science/article/abs/pii/S1042443120300780 and https://www.sciencedirect.com/science/article/abs/pii/S1062976921000168
- [flush_anatomy] Crypto short-term reversal is liquidity-dependent: reversal profits concentrate in smaller/less liquid coins while the largest coins exhibit short-term momentum — consistent with our falling-knife-universe lesson that the $50M liquidity gate is load-bearing and should NOT be loosened to gain fires. Sources: https://www.sciencedirect.com/science/article/pii/S1057521921002349 and https://wp.ffu.vse.cz/pdfs/wps/2023/01/03.pdf
- [flush_anatomy] Deeply negative funding marks max capitulation: extreme negative funding near lows (crowded, paying shorts) is the documented squeeze-fuel condition; flushes WITHOUT funding stress are more often mid-trend continuation. funding_z<=-1.5 is a natural confirmation layer on capitulation fires. Sources: https://medium.com/@XT_com/bitcoin-futures-market-microstructure-liquidation-cascades-funding-regimes-and-open-interest-978b107b4889 and https://tradelink.pro/blog/funding-rate-open-interest/
- [flush_anatomy] Time-of-day matters: BTC volume/volatility cluster in US/EU equity hours, and the 21:00-23:00 UTC window shows the strongest positive drift — flushes in thin dead-zone hours (roughly 00:00-06:00 UTC) are more likely stop-hunt wicks than real climaxes; hour_utc is a cheap audit dimension before hard-coding a filter. Sources: https://blog.paperswithbacktest.com/p/bitcoin-never-sleeps-exploiting-seasonality and https://www.sciencedirect.com/science/article/abs/pii/S1544612319301904
- [timing_liquidity] Order-book depth is strongly hour-dependent: Amberdata measured BTC depth within 10bps at ~$3.86M at 11:00 UTC vs ~$2.71M at 21:00 UTC (-42%). MR entries filled 21:00-23:00 UTC eat materially worse slippage and thinner bounce fuel. https://blog.amberdata.io/the-rhythm-of-liquidity-temporal-patterns-in-market-depth
- [timing_liquidity] The 'tea time' study finds trading activity, volatility AND illiquidity all peak 16:00-17:00 UTC, while the 21:00-23:00 UTC window (all major equity markets closed) shows the most economically significant free-floating returns — i.e., late-US-evening capitulation prints are disproportionately cascade-driven, not exhaustion. https://link.springer.com/article/10.1007/s11156-024-01304-1 and https://quantpedia.com/strategies/intraday-seasonality-in-bitcoin
- [timing_liquidity] Weekend microstructure is measurably degraded: BTC volume drops 20-40% vs weekdays with wider spreads, and a 2014-2024 study finds volatility/activity lower with NO compensating return premium — weekend capitulation fires get thin-book cascade risk without extra edge. https://phemex.com/blogs/weekend-crypto-trading-explained and https://www.researchgate.net/publication/396418897_Bitcoin's_Weekend_Effect_Returns_Volatility_and_Volume_2014-2024
- [timing_liquidity] Post-mortems of the Oct 2025 $19B flash crash show market makers pulled quotes or widened spreads from single-digit bps to double-digit percent during forced-liquidation waves, with altcoin perps falling 50-80%; a vol_ratio spike during a depth vacuum is a liquidation cascade signature, not seller exhaustion — hence hammer/absorption confirmation (close_pos) matters. https://insights4vc.substack.com/p/inside-the-19b-flash-crash and https://www.fticonsulting.com/insights/articles/crypto-crash-october-2025-leverage-met-liquidity
- [timing_liquidity] Funding-rate state separates real capitulation from traps: extremely negative funding marks maximum capitulation (longs already flushed), while price dropping with funding still positive indicates 'desperate longs buying the dip against spot selling' — a continuation setup. funding_z is the cleanest available trap filter for the capitulation family. https://www.altrady.com/blog/crypto-trading-strategies/crypto-funding-rates-explained and https://zipmex.com/blog/how-to-analyze-funding-rates-in-crypto/
- [timing_liquidity] Oversold RSI sitting on freshly broken support invalidates bounce theses; markets stay oversold for weeks in defined downtrends, and momentum stalling at low RSI historically precedes either a dead-cat squeeze or a flush to new lows — RSI depth alone cannot distinguish these, but where the bar CLOSES within its range (absorption) can. https://www.ainvest.com/news/bitcoin-oversold-rsi-trap-bounce-setup-2602/
- [timing_liquidity] Macro release windows (CPI ~12:30-13:30 UTC, FOMC 18:00-19:00 UTC) produce pre- and post-announcement volatility spikes in 5-minute data with fast synchronized reversals; capitulation bars printed inside these windows are news whipsaws where 1% SLs get run before any 6% reversion. https://www.sciencedirect.com/science/article/pii/S1059056025006720
- [timing_liquidity] Practitioner guidance on thin off-hours MR: it can still work but the SAME sell flow moves price further in thin books, producing deeper overshoot and faster snapback — implying an exploratory dead-zone variant should pair thin-hours entry with a NEARER take-profit, only kept if grid EV survives. https://mudrex.com/learn/best-time-to-trade-crypto-futures/