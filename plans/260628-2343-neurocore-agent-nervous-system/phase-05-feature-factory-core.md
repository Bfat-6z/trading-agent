# Phase 05: Feature Factory Core

## Overview

Replace scattered snapshots with timestamp-aligned feature rows.

## Related Code

- `market_feature_store.py`
- `market_data_lake.py`
- `market_observer.py`
- `regime_labeler.py`
- `microstructure_observer_loop.py`

## Implementation Steps

1. Split raw source paths from normalized feature paths.
2. Add canonical feature window: symbol, timeframe, start/end, candle close time.
3. Hash all inputs, not just `bool(derivatives)`.
4. Add feature rows for OHLCV, volatility, trend, volume spike, BTC/ETH regime.
5. Add missing-rate and confidence fields.
6. Persist features with manifest ids for replay.
7. Define required/optional source matrix per feature family.
8. Distinguish `zero`, `missing`, `stale`, and `imputed` values explicitly.
9. Mark feature rows unusable when required source is missing/stale; do not only lower confidence.
10. Enforce decision cutoff proof: `max(available_at, known_at, ingested_at, finalized_at) <= decision_cutoff - latency_buffer`.
11. Add golden feature fixtures from existing runtime state and expected normalized output.
12. Split `decision_regime_state` from `post_trade_regime_outcome`; decision labels store labeler version, allowed inputs, horizon, cutoff, finalized-candle lag, and input event ids.
13. Add market breadth and beta features: BTC/ETH beta, advance/decline, cross-sectional returns/vol/OI/funding/liquidation breadth, sector tags, and liquidity tier.
14. Add `decision_data_capability_mask` per setup: required, optional, missing, stale, source confidence, and action (`skip`, `size_cap`, `shadow_only`, `normal`).
15. Add historical universe membership manifest: listed/delisted/status, first/last seen, filters/brackets, data availability, and missing-data reason by timestamp.
16. Universe-at-time manifest must include delisted, non-trading, newly listed, unavailable, excluded, and not-scanned symbols with reason; current active symbols alone are diagnostic only.
17. Every normalized feature, scaler, regime threshold, trust calibration, or transform stores fit window, fit cutoff, train partition, input event ids, and artifact digest.
18. Add `golden_replay_determinism_e2e_v1/`: raw events, candles, exchangeInfo, funding/OI, Telegram/news, decision cutoff, expected feature id, paper decision, ledger, score row, and output hashes under shuffled input order.

## Tests

- Same candles with different funding/OI produce different feature ids.
- Mixed timestamps are aligned or marked unusable.
- Missing source lowers confidence, not hidden as zero.
- Replay by feature id reproduces row.
- Required source missing/stale makes feature row unusable.
- Future-available source is rejected as lookahead.
- Golden raw source fixture produces stable feature row.
- News ingested after decision cannot be used even if article publish time is earlier.
- Decision regime label uses only pre-cutoff inputs; post-trade outcome label is separate.
- Missing required capability mask blocks candidate; optional missing capability caps size and tags score segment.
- Historical delisted/unavailable symbol remains visible in replay universe with missing-data reason.
- Current-survivor-only universe is labeled diagnostic and cannot feed readiness/walk-forward.
- Feature scaler/regime/trust calibration fit outside train partition contaminates validation/holdout and fails.
- Golden e2e fixture produces identical feature/decision/ledger/score hashes when input file order is shuffled.

## Done Gate

Paper decisions reference a feature row id, not random latest snapshots.

## Audit Questions

- Can current/latest data contaminate an old decision?
- Are zeros real values or missing-data fallbacks?
- Is a regime label known at entry or hindsight outcome?
- Which required capabilities were present, missing, stale, or optional at decision time?
