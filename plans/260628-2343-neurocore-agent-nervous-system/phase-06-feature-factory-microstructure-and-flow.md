# Phase 06: Feature Factory Microstructure And Flow

## Overview

Add orderbook, funding/OI, liquidation, spread/slippage, whale/Telegram, and news features into one aligned factory.

## Related Code

- `orderbook_observer.py`
- `derivatives_observer.py`
- `liquidation_observer.py`
- `whale_flow_observer.py`
- `news_signal_model.py`
- `market_feature_store.py`

## Feature Families

- Orderbook imbalance, depth slope, spread, size-aware impact.
- Funding rate history, OI delta/z-score, crowding pressure.
- Liquidation clusters by side, time bucket, price proximity, decay.
- Whale/Telegram normalized symbol/side/urgency/source trust.
- News/macro/regulatory risk, catalyst chaos, symbol impact.
- Instrument normalization: contract type, quote unit, notional conversion, provider semantics, funding boundary/effective time.
- Orderbook integrity: depth level, update id continuity, checksum/resync, crossed book filter, age, size bucket.
- Binance instrument registry: `exchangeInfo` snapshot id, canonical instrument id (`binance_usdm:BTCUSDT:PERPETUAL`), symbol/pair/contract type, status, base/quote/margin asset, filters, precision, rate limits, and schema digest.
- Leverage bracket registry: account-scoped notional brackets, max leverage, floor/cap, maintenance margin ratio, `notionalCoef`, and stale/fail-closed policy.
- Price-basis contract: explicit `MARK`, `INDEX`, `LAST`, `BOOK_MID`, `CANDLE_CLOSE` basis on every feature/calculation.
- Funding schedule: `nextFundingTime`, `fundingIntervalHours`, cap/floor, boundary snapshots, missed-boundary backfill, and stale completeness flag.
- Alias resolver: social/news tickers map to canonical instruments or quarantine ambiguity (`PEPE` vs `1000PEPEUSDT`).
- Funding feature split: `predicted_funding_rate`, `settled_funding_rate`, `backfilled_settled_rate`, `announced_at`, `settled_at`, and decision-use policy.
- Funding sign schema: `rate_decimal`, `payer_side`, `position_side`, `settlement_time`, `effective_interval`, and venue convention.
- OI schema: raw value/unit, contract multiplier, base qty, quote notional, USD notional, margin asset, price basis, and sampling interval.
- Liquidation feed health: uptime, last-event watermark, reconnect gaps, duplicate key, partial-coverage flag, provider completeness, stale-as-unknown.
- Liquidation cluster windows are half-open and cutoff-safe: `[window_start, decision_cutoff]` with all events `available_at <= decision_cutoff`.
- Price-basis policy matrix: fills=`BOOK_MID/LAST+slippage`, liquidation=`MARK`, funding notional=`MARK@boundary`, premium=`MARK-INDEX`, candles=`CANDLE_CLOSE`.
- V1 derivatives venue scope is Binance USD-M only unless a `DerivativesVenueAdapter` maps funding/OI/liquidation semantics with fixtures.
- External flow families: on-chain/CEX netflow/reserves, stablecoin liquidity/peg, DEX liquidity/basis, unlock/calendar/macro event windows, wallet/entity attribution.
- Golden fixture corpus: `binance_usdm_exchangeInfo_YYYYMMDD.json`, `binance_usdm_leverageBrackets_YYYYMMDD.json`, and ws/rest samples for BTCUSDT, ETHUSDT, 1000PEPEUSDT, non-TRADING/delisted symbols, precision/filter edge cases, 429/418, malformed depth, server-time skew, and bracket schema drift.
- Golden social/news corpus: raw Telegram/news inputs with copied claims, XSS, ambiguous aliases, unicode homoglyphs, publish-before/ingest-after rows, quorum failures, and expected canonical instrument or quarantine reason.

## Tests

- Feature row includes aligned microstructure/source timestamps.
- Liquidation cluster near price differs from distant cluster.
- Funding/OI history uses rolling window, not latest-only.
- Telegram/news parser confidence affects feature confidence.
- Stale/dropped/crossed orderbook is rejected.
- Funding/OI/liquidation units normalize across instruments/providers.
- Copied Telegram/news claims do not count as independent quorum.
- Every feature row carries `instrument_snapshot_id`, canonical instrument id, and price basis.
- Ambiguous social symbol is quarantined, not mapped silently.
- Non-TRADING/delist/settling instrument blocks open-candidate features.
- Funding schedule uses per-symbol interval, not fixed 8h assumption.
- Replay cannot use settled funding rate before it was known.
- Positive/negative funding charges correct long/short side.
- OI normalization outputs base qty, quote notional, and USD notional from raw unit.
- Liquidation websocket outage produces unknown/unusable feature, not zero liquidations.
- Liquidation after decision inside same bucket is excluded.
- High-impact macro/unlock blackout blocks or size-caps paper opens.
- Instrument fixture rejects stale, delisted, below-min-after-rounding, reduce-only-over-close, and tick/step directional rounding failures.
- Binance fixture server/websocket replay catches 429/418, schema drift, malformed depth update, clock skew, dropped update id, and reconnect gap.
- Parser fixture proves copied claims count as one source and private/protected or ambiguous claims stay shadow/quarantine.

## Done Gate

Candidate feeder/ranker can use microstructure and flow features without reading raw observer latest files.

## Audit Questions

- Is a volume spike confirmed by orderbook/tape or just candles?
- Is a Telegram signal high trust or noisy spam?
- Which exact instrument snapshot made this feature valid?
- Is this price/funding/OI feature using mark, index, last, or book basis?
- Is this settled value known at decision time or only after the fact?
- Does absent liquidation/on-chain/macro data mean zero signal or unknown coverage?
