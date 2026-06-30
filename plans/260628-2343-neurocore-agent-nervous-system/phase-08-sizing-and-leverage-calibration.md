# Phase 08: Sizing And Leverage Calibration

## Overview

Let the paper agent use a $100 futures account more realistically without faking risk. Fix tiny order causes and allow leverage up to 50x only when risk math supports it.

## Related Code

- `capital_allocation_policy.py`
- `paper_portfolio_manager.py`
- `autonomous_paper_trading_brain.py`
- `paper_candidate_feeder.py`
- `portfolio_correlation_guard.py`

## Implementation Steps

1. Separate stop distance from crude 24h high/low range.
2. Use ATR/microstructure-based SL templates per setup.
3. Track `initial_margin`, `maintenance_margin`, `fee_to_close_reserve`, `risk_at_stop`, `funding_reserve`, and `gap_loss_estimate` separately.
4. Allow larger notional when SL is tight, liquidation distance is safe, instrument/bracket data is fresh, and account invariants pass.
5. Cap risk by setup evidence, drawdown, correlation, liquidity.
6. Record `paper_sizing` object in every open/skip decision.
7. Include gap-through-stop, fee-to-close, funding before exit, correlated open positions, and available margin reservation.
8. Add immutable paper risk policy: max risk/trade, max daily loss, max correlated exposure, max open notional, drawdown throttle, liquidation-distance floor, symbol/regime leverage cap, and stress-test pass rule.
9. Treat 50x as paper-only high-risk simulation allowed only inside the immutable policy; live plan must re-approve leverage from zero.
10. Sizing must fail closed if leverage bracket, mark price, funding schedule, exchange filters, or account reconciliation is stale.
11. Add single admission controller with atomic risk reservations for pending/open/closing states; concurrent candidates cannot each see the same free margin.
12. Add rolling BTC/ETH beta model, stressed beta fallback, gross/net beta-notional caps, and pre-trade marginal beta check.
13. Specify correlation guard: estimator horizons, decay, stale policy, stressed matrix, sector/beta clusters, and cluster exposure caps.
14. Add exposure ledger: underlier, quote/margin asset, delta-notional, beta-notional, contract family, alias group, and collapsed synthetic exposure caps.
15. Add hierarchical budgets: account -> market beta -> sector/liquidity tier -> symbol -> setup/source -> trade, enforcing max marginal risk contribution.
16. Define daily loss breaker state machine using realized + unrealized + fees/funding/liquidation marks: cancel pending, reduce/flatten by risk, cooldown, and signed audit event.
17. Add pre-trade stress pack: BTC/ETH crash, alt beta shock, correlation=1, spread x5, funding shock, source outage, and stablecoin/margin-asset shock.
18. Add `risk_reservation` ledger lifecycle: `reserved -> captured|released|expired|cancelled`, reserve id, TTL, order/fill ids, CAS transitions, expiry sweeper, breaker transaction, and reconciliation.
19. Add candidate viability inputs before reservation: min executable notional/risk by instrument/setup, minimum safe size, expected fee/funding/slippage, and below-min action (`shadow_only` or `counterfactual_only`).

## Tests

- Wide-stop candidate gets small size or skipped.
- Tight-stop high-liquidity candidate can use larger notional.
- 50x is allowed in paper only when liquidation/fee/risk checks pass.
- Negative-expectancy setup cannot receive normal allocation.
- Gap-through-stop scenario still respects max loss or is rejected.
- Correlated open exposure reduces available risk.
- Initial margin and risk-at-stop are not conflated.
- Drawdown/daily-loss breaker reduces or blocks new paper positions.
- Missing/stale bracket or instrument snapshot rejects sizing.
- Ten simultaneous same-timestamp candidates respect account and cluster caps through atomic reservations.
- Calm correlation allows trade but stressed matrix blocks overexposure in crash test.
- Daily loss breaker cancels pending orders and de-risks existing exposure, not just blocks new opens.
- Crash stress pack fails unsafe 50x sizing even when tight SL math passes.
- Rejected/expired order releases reservation; fill capture cannot use cancelled reservation.
- Candidate below min executable risk is not ranked as normal paper open.

## Done Gate

User can inspect why a paper order size is small/large.

## Audit Questions

- Is size small because risk is real or because SL calc is bad?
- Is leverage chosen by evidence or hardcoded defaults?
- Is 50x being used as a bounded paper experiment or as an ungoverned default?
- Did initial margin, maintenance margin, and risk-at-stop all pass independently?
- Is this symbol diversification or one collapsed BTC/ETH beta exposure?
- Did concurrent candidates reserve risk atomically?
- Can any reservation stay stuck or be captured after breaker/cancel?
- Is this candidate executable with $100 without violating risk or min-notional rules?
