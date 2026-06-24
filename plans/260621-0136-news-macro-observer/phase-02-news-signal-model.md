# News Signal Model

Status: Complete

## Context Links

- Parent plan: [News Macro Observer](./plan.md)
- Inputs: `state/agent_memory/news_events.jsonl`
- Outputs: `state/agent_memory/news_latest.json`, `state/agent_memory/news_latest.md`

## Overview

Create `news_signal_model.py`, a deterministic scorer that turns normalized headlines into trading-context signals. It should be explainable, fixture-testable, and conservative.

## Requirements

- Produce top-level scores from 0.0 to 1.0: `macro_risk_score`, `crypto_regulatory_risk`, `catalyst_score`, `headline_chaos`, `source_quality_score`, `freshness_score`.
- Produce `symbol_impacts` keyed by symbol with `bullish`, `bearish`, `risk`, `confidence`, `reasons`, and `event_ids`.
- Detect topics: rates/Fed/CPI/jobs/liquidity, ETF flows, exchange hack/outage, liquidation risk, token unlock, listing/delisting, lawsuit/regulation, geopolitical shock, stablecoin/depeg, chain outage, whale/social pump.
- Apply freshness decay so old headlines lose influence.
- Apply source quality weighting so social posts cannot outrank reputable news by volume alone.
- Optional LLM summary may exist, but deterministic scores are canonical.

## Implementation Steps

- Implement keyword/topic maps and symbol extraction helpers.
- Implement source-quality and freshness decay functions.
- Implement score aggregation with reasons for every nonzero risk.
- Render concise markdown report for dashboard and human review.
- Add fixture tests for high-risk, low-risk, malformed, duplicate, and stale headlines.

## Todo Checklist

- [x] Define normalized event schema constants.
- [x] Implement topic classification.
- [x] Implement risk/catalyst scoring.
- [x] Implement symbol impact aggregation.
- [x] Implement latest JSON and MD writer.
- [x] Add deterministic fixture tests.

## Risks

- Headline keyword scoring can false-positive. Keep it tighten-only and require reason visibility.
- Catalyst score must not become an entry signal by itself.
