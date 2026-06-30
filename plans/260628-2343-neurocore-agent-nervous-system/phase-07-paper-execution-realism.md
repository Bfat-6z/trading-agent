# Phase 07: Paper Execution Realism

## Overview

Make paper futures behave close enough to Binance futures for learning: fees, funding, slippage, filters, liquidation, partial fill assumptions.

## Related Code

- `paper_execution_lifecycle_loop.py`
- `paper_execution_simulator.py`
- `paper_portfolio_manager.py`
- `instrument_registry.py`

## Implementation Steps

1. Route lifecycle open/close through simulator or shared execution primitives.
2. Deduct entry fee at open.
3. Apply maker/taker entry fee, exit fee, fee-to-close reserve, realized PnL, and funding accrual.
4. Apply adverse slippage and spread from Feature Factory.
5. Enforce min notional, qty step, tick size, leverage bracket, LOT_SIZE vs MARKET_LOT_SIZE, maxQty/maxNotional, reduce-only close, directional rounding, and reject-after-rounding-below-minNotional.
6. Add liquidation price using mark price, maintenance margin tiers, isolated/cross mode, free collateral, and liquidation fee assumptions.
7. Close as liquidation when mark price breaches liquidation before SL/TP.
8. Store funding snapshots by boundary, not latest-only.
9. Record exact execution assumptions per trade.
10. Add golden paper execution fixtures: entry, funding, slippage, liquidation, exit, realized PnL.
11. Make the account ledger source of truth. Derive snapshots only from deduped ledger events.
12. Define account invariants: `equity = balance + unrealized_pnl`, `free_margin = equity - used_margin - reserves`, `used_margin = sum(initial_margin)`, and no negative free margin unless liquidation event is emitted.
13. Model order/fill lifecycle explicitly: `paper.order`, `paper.fill`, `paper.position_update`, `paper.close`, cancel/expire, partial fills, residual qty, average price, per-fill fee.
14. Add reduce-only invariant: executable qty is capped to current open qty after rounding toward zero; it can never increase or flip the position.
15. Add `funding.settlement` ledger events with boundary time, mark notional, side sign, funding rate source, and position-open-at-boundary rule.
16. Add `paper.liquidation` event with mark path, liquidation price, bankruptcy price, maintenance margin, liquidation fee, realized loss, and post-liquidation account snapshot.
17. Require `instrument_snapshot_id`, bracket id, margin asset, and price basis on every order/fill/position/PnL/funding/liquidation calculation.
18. Add account-level cross-margin engine: shared collateral, maintenance tiers, liquidation queue, contagion between positions, bankruptcy/liquidation fees, and cascade tests.
19. Add conservative ADL/insurance stress model when venue ADL/insurance data is absent; score uncertainty penalty must record the assumption.
20. Add margin-asset peg/liquidity haircut for USDT/USDC-style collateral when stablecoin data is stale/stressed.
21. Serialize account ledger writes through one per-account transaction/single writer. Fill, close, funding, liquidation, reservation capture, and projection update must commit atomically.
22. Forbid direct snapshot mutation; account latest/projection is derived in the same transaction from deduped ledger rows with unique transaction keys.
23. Record `operator_intervention.applied` when a human/tool changes paper state: action, reason, signer, before/after state, causal flag, and inclusion/exclusion effect for scoring.
24. Add `account.capital_event` ledger rows for deposit, withdrawal, reset, correction, and manual rebalance. Capital events split score/equity windows and cannot be hidden inside PnL.
25. Add manual override policy: allowed actions, forbidden actions, role, reason code, expiry, idempotency key, pre/post verification, and scoring segmentation. Risk-increasing or live-like manual actions are forbidden in this phase.
26. Define `golden_paper_ledger_binance_usdm_v1.json`: order, partial fills, funding boundary, partial close, liquidation-before-SL, manual close, capital event, expected snapshots after each row, and corruption variants.
27. Define `golden_funding_fee_boundaries_v1.json`: long/short, positive/negative funding, before/exactly-after boundary opens, maker/taker fees, fee-to-close reserve, mark-notional at boundary, and no double-funding on replay.

## Tests

- Entry fee reduces cash/equity immediately.
- Exit fee and fee-to-close reserve affect realized PnL.
- Maker/taker mode is explicit and tested.
- 50x trade liquidates if price breaches liquidation before SL.
- Min notional/step rounding is deterministic.
- Funding across multiple boundaries uses historical rates.
- Slippage can turn tiny winner into loser.
- Reduce-only close respects exchange filters.
- Golden execution fixture matches expected ledger and PnL exactly.
- Snapshot rebuilt from ledger matches latest account within decimal precision.
- Partial fill, partial close, cancel, and residual qty produce correct avg price, margin, fee, and PnL.
- Duplicate fill does not double debit balance or margin.
- Funding is charged only if position is open at the funding boundary and is not double-counted on replay.
- Reduce-only over-close cannot flip long to short or short to long.
- Liquidation fixture includes liquidation fee, bankruptcy price, and correct post-liquidation equity.
- Divergent mark/last/index fixture uses the documented price basis for each calculation.
- Cross-margin cascade fixture liquidates/de-risks correlated legs from shared collateral loss.
- ADL/insurance stress assumption appears in execution assumptions and scoring completeness.
- Stablecoin depeg/liquidity stress blocks or haircuts margin before sizing/fill.
- Concurrent close/funding/liquidation writers cannot double-apply or overwrite account state.
- Direct latest/snapshot mutation test fails.
- Manual close/pause/reduce event is segmented from strategy-driven exit in scoring and memory.
- Account capital event splits equity/PF/DD windows; reset/deposit cannot mask drawdown.
- Paper ledger corruption fixtures cover missing row, duplicate fill, reordered funding, forked hash chain, and mismatched event/account/audit transaction ids.
- Restored account ledger rebuild matches latest, scoring, dashboard equity chart, and trial proof bundle from the same seq range.
- Funding/fee boundary fixture proves funding is based on mark notional at boundary, not latest mark, and replay is idempotent.
- Manual override without allowed action, role, reason, expiry, and post-check is rejected and excluded from learning as strategy behavior.

## Done Gate

Paper PnL after fees/funding/slippage is learning source of truth.

## Audit Questions

- Would this trade have been liquidated on real futures?
- Is the paper fill possible under exchange filters?
- Can the current equity be rebuilt exactly from the ledger?
- Which price basis and instrument snapshot made this fill/PnL valid?
- Does cross-margin liquidation treat the portfolio as one collateral pool when configured?
- Are all account mutations serialized through the ledger transaction boundary?
- Did a strategy exit happen automatically or because an operator intervened?
