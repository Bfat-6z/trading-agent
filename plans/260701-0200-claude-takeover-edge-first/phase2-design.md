# Phase 2 Design — Honest cost & exit model

Status: DRAFT for review before coding. Investigation (2.1 map + 2.2 research) complete.

## Goal
Make the paper simulator stop flattering us. After this, edge should look WORSE — that is correct and expected. No live trading touched.

## Verified current state (2.1)
- Fees: taker 5bps / maker 2bps. Entries always market→taker (both legs taker in real runs). ✓ realistic already.
- Slippage: flat **2bps** at market-entry, SL, TP. **Timeout exit = 0 slippage. Liquidation = 0 slippage.** ← optimistic.
- Spread: **not modeled at all.** ← optimistic.
- Depth/impact: not modeled (only exogenous candle fill_fraction).
- Liquidation: **flat MMR 0.5%**, not tiered. Duplicated in paper_portfolio_manager.py:152.
- **3 code sites compute exit/cost math and must change in lockstep:** paper_execution_simulator.py, paper_execution_lifecycle_loop.py (timeout exit :825), counterfactual_replay_agent.py:200-290 (shadow engine reimplements it).

## Research numbers to encode (2.2, sourced)
- Taker 5.0bps / maker 2.0bps (VIP0, no BNB). Keep taker both legs (pessimistic).
- Spread (bps of price): majors 0.5–2, mid-caps 3–15, microcaps 20–80+.
- Slippage: majors ≈ half-spread (~1–2bps is a FLOOR not average); microcaps 15–50bps.
- Tiered MMR: BTC bracket-1 = 0.40%, alts tier-1 often 1–2.5%. $100 account is always bracket 1.
- Funding: 8h settle, ~0.01%/interval; already modeled — leave as is.
- Round-trip taker scalp on a major already costs ~12–14bps before profit.

## Design (minimal, lockstep across the 3 sites, reversible)

### C1 — Central cost module (NEW `paper_cost_model.py`)
Single source of truth so the 3 sites can't diverge. Pure functions, no network:
- `half_spread_bps(liquidity_tier) -> Decimal` and `slippage_bps(liquidity_tier, is_stop=False) -> Decimal`.
- `liquidity_tier(quote_volume_24h) -> "major"|"mid"|"micro"` from the market row's 24h quote_volume (already on the candidate row). Thresholds: major ≥ $500M, mid ≥ $50M, else micro.
- Constants (conservative, pessimistic-leaning):
  - half-spread bps: major 1, mid 6, micro 30.
  - slippage bps (market/limit-touch): major 2, mid 10, micro 40.
  - **stop-order slippage multiplier: 3×** (stop-market slips worse in volatility — research checklist).
  - MMR floor: `max(symbol_tier1_mmr, 0.005)`; if unknown use micro-safe **0.01** (1%). (Full tiered brackets deferred — $100 acct is always bracket 1; a conservative floor is safer than a fake-precise flat 0.5%.)

### C2 — Apply spread + realistic slippage at every fill (paper_execution_simulator.py)
- `adverse_slippage`/`exit_slippage` take a `bps` arg already (:41,:48). Feed them `slippage_bps(tier) + half_spread_bps(tier)` instead of flat 2.
- **Add slippage to the two currently-free exits:** liquidation (:156,:170) and — in the lifecycle — timeout (:825). Timeout is a market exit → taker + slippage + half-spread.
- Stop/SL fills use the 3× stop multiplier.
- Limit entries: keep exact-price fill for now BUT do NOT add a maker-entry strategy (that needs queue/adverse-selection modeling — out of scope, noted).

### C3 — Timeout exit realism (paper_execution_lifecycle_loop.py:825)
Currently returns raw mark, zero slippage. Change to apply exit slippage+spread+taker fee via the shared model (it's a market close).

### C4 — Liquidation MMR (both sites)
Replace flat 0.005 with `mmr_floor(symbol)` from the cost module (conservative floor). Keep the isolated-margin formula; just raise the MMR input. Mirror in paper_portfolio_manager.py:152.

### C5 — Shadow engine lockstep (counterfactual_replay_agent.py:200-290)
Route its exit math through the same `paper_cost_model` so it can't diverge from the live paper sim.

### C6 — Exit-source honesty stamping (folds in Phase-1 m4/m6/m7)
Stamp each close record with: `exit_price_source` (real_ohlc vs mark_fallback), `spread_bps`, `slippage_bps`, `liquidity_tier` applied. So we can audit later that costs were charged.

## Risks & mitigations
| Risk | Mitigation |
|---|---|
| 3 sites diverge | central `paper_cost_model.py` = single source; C5 routes shadow through it |
| Tests lock old numbers (liq==9.85, SL fills, 2bps) | update those tests intentionally; assert NEW cost >= OLD cost |
| Over-charging kills all paper trades | tiers are graded; majors stay cheap (~1–2bps); only microcaps get heavy floors — which is correct |
| MMR floor too crude | conservative (safer than optimistic); full tiered brackets = later phase, log the simplification |
| Breaking counterfactual math | test shadow vs sim produce same cost for same inputs |

## Test plan
- Unit: cost module tiers + bps + stop multiplier + mmr floor.
- Every fill now costs ≥ old flat-2bps model (assert monotonic).
- **Ledger replay: re-run the recorded 946 closes through the new model → net PnL equal-or-MORE-negative than the −$52.79 baseline** (proves we removed optimism, not added it).
- Timeout and liquidation now carry non-zero slippage.
- Shadow engine and sim agree on cost for identical inputs.
- Full suite green (with intentional test updates).

## Deferred (logged, not now)
- Full Binance tiered-MMR brackets from leverageBracket API (acct is bracket-1).
- Maker-fill realism (queue position, adverse selection) — required BEFORE any maker-entry strategy.
- Partial-fill / latency modeling.

## Open question for owner
Cost floors above are deliberately **pessimistic** (micro slippage 40bps, stop 3×). This will make many microcap scalps look unprofitable in paper — which is the point (they likely are). OK to lean pessimistic, or prefer mid-range estimates? Recommendation: **pessimistic** — better to kill a fake edge in paper than discover it live.