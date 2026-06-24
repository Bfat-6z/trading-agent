# News Source Ingestion

Status: Complete

## Context Links

- Parent plan: [News Macro Observer](./plan.md)
- Reuse: `tradingagents_crypto_src/tradingagents/dataflows/cryptopanic.py`
- Reuse: `tradingagents_crypto_src/tradingagents/dataflows/alpha_vantage_news.py`
- Reuse: `tradingagents_crypto_src/tradingagents/dataflows/yfinance_news.py`
- Reuse: `tradingagents_crypto_src/tradingagents/dataflows/reddit.py`

## Overview

Create `news_observer.py`, a long-running read-only process that fetches recent news/social items on an interval, normalizes them, dedupes them, writes append-only events, and emits latest state plus heartbeat.

## Requirements

- CLI flags: `--once`, `--interval-seconds`, `--symbols`, `--max-items-per-source`.
- Sources in priority order: CryptoPanic when `CRYPTOPANIC_API_KEY` exists, AlphaVantage when configured, yfinance global news fallback, Reddit public search as social/noise input, optional RSS fallback.
- Normalize each item to: `event_id`, `ts_seen`, `published_at`, `source`, `source_type`, `title`, `summary`, `url`, `symbols`, `topics`, `raw_sentiment`, `raw_importance`, `fetch_status`.
- Dedupe by stable hash of normalized title, source, URL, and published time.
- Never print or persist API key values.
- Degrade gracefully if one source fails; heartbeat must show per-source status.

## Implementation Steps

- Add `news_observer.py` following the process style of `market_observer.py`.
- Add source adapters that convert existing dataflow return types into the normalized schema.
- Write latest files under `state/agent_memory/` and heartbeat under `state/`.
- Append events to `event_store` with source `news_observer`.
- Add simple backoff/rate-limit protection per source.

## Todo Checklist

- [x] Implement CLI and PID/heartbeat helpers.
- [x] Implement CryptoPanic adapter.
- [x] Implement yfinance/global fallback adapter.
- [x] Implement Reddit social adapter with low request budget.
- [x] Implement dedupe and append-only event writer.
- [x] Add malformed payload handling tests.

## Risks

- Provider payloads can change; adapters must be defensive.
- Reddit/social data is noisy; phase 1 only stores it, phase 2 scores it conservatively.
- yfinance news can be flaky; source health must be visible.
