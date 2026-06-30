# Phase 04: Source Provenance And Data Trust

## Overview

Make every feature and learning record know where data came from, how fresh it was, and whether it should be trusted.

## Related Code

- `source_provenance.py`
- `data_source_registry.py`
- `quota_monitor.py`
- `whale_flow_observer.py`
- `news_observer.py`

## Implementation Steps

1. Register all data sources: Binance REST/ws, orderbook, liquidation, funding/OI, Telegram channels, news, manual screenshots.
2. Add source trust score, SLA, quota, freshness, parse confidence.
3. Require provenance ids for market/news/flow features.
4. Add source degradation events into bus.
5. Add prompt-injection stripping at ingest, storage, retrieval, dashboard, and LLM-use boundaries.
6. Add source identity checks where possible: channel id/permalink, provider name, capture time, parser version.
7. Require corroboration/quorum for high-impact external social/manual signals before they can affect skills.
8. Add data egress classification before any external LLM call: public market data, internal strategy, user notes, secrets.
9. Taint external text at ingest and preserve taint through storage, retrieval, dashboard, and prompts.
10. Treat social/news/manual signals as claims with content hash, source graph, copy-chain detection, post edit/delete handling, and independent-source quorum.
11. Add provider compliance registry: endpoint weight/quota, websocket caps, allowed use, retention/redistribution notes, retry/backoff, ban circuit breaker, and ToS/version evidence.
12. Store only API key fingerprints/scopes. Assert Binance keys are read-only market-data only, no trade/withdraw permission, and IP-allowlisted where provider supports it.
13. Prefer public Telegram/channel metadata where possible; bot/user tokens require explicit scope, rotation plan, and leak incident policy.
14. Classify human/operator free text too. Panic/revenge/override-risk instructions are tainted, rejected from learning, emit audit event, and can never loosen risk/DONT_DO gates.
15. Add `exchangeInfo` and contract-info source freshness policy: TTL/SLA, snapshot version, stale fail-closed for opens, and rate-limit-aware refresh.
16. Social/manual claims default to `shadow_only`. They cannot open, size up, or rank up paper trades unless independent market confirmation and source quorum pass.
17. Add source onboarding ledger for Telegram/news/manual sources: immutable peer/channel id, username history, canonical URL, signer, TTL, trust=0 until approved/backtested.
18. Manual screenshots are weak non-executable claims only: OCR model/version, perceptual hash, source URL corroboration, human signer, and never direct paper-open authority.
19. Add social latency/chase fields: `source_posted_at`, `first_seen_at`, `decision_delay_ms`, `move_since_post_pct`, TTL by signal type, and too-late-to-copy skip rule.
20. Add multilingual parsing contract: language detect, translation provenance, action/stance separation, negation/slang/emoji fixtures, and quarantine on low-confidence side/urgency.
21. Add claim clustering beyond content hash: forwarded metadata, URL/message lineage, simhash/embedding, translation-normalized text, screenshot cluster, source-owner correlation.
22. Add external-data source families with explicit required/optional masks: on-chain exchange netflow/reserves/large transfers/bridge flow, stablecoin liquidity/peg, wallet/entity labels, CEX/DEX liquidity/basis, unlock/vesting/listing/delist calendars, and structured macro calendar.
23. For paid/external data, record cost/day, calls/min, cache depth, fallback provider, blackout behavior, stale-null semantics, and paper-decision impact.
24. Add source-rights registry: public-only default, private/protected source ban unless explicitly approved, minimal text retention, export/redistribution flags, takedown/delete propagation.
25. Screenshot intake pipeline: quarantine original, strip EXIF, local OCR only, PII/secret redaction, encrypt original with short TTL, store safe excerpt/hash, `allowed_effect=annotation_only` unless ledger-backed.
26. External LLM egress is default-deny for personal/user notes, screenshots/OCR, private social text, secrets, and internal strategy. Every allowed call needs egress proof.
27. Add source FSM for user-provided Telegram/news sources: quarantine -> verified -> shadow OOS -> capped trust, with ownership drift, mirror graph, leave-source-out validation, decay, and user-source trust cap.
28. Add human feedback classifier fields: `sentiment`, `instruction`, `outcome_claim`, `preference`, `metric_claim`, `panic_revenge`, `risk_reducing_command`. Praise/blame and preference get zero learning weight unless evidence-backed.
29. Add provider entitlement registry: plan tier, reset time, credit units, metering headers, invoice reconciliation, hard cap, degraded/null semantics, and pre-call budget reservation.
30. Add content-addressed provider cache: request hash, response hash, ETag/header metadata, TTL, stale policy, billing flag, replay-only offline mode, cache hit/miss spend metrics.
31. Inventory authenticated exchange/private-account scripts and outputs as data sources with `environment`, `account_scope`, and `allowed_effect`; private live-account snapshots are never paper account truth or readiness evidence.
32. Add socket/network deny fixture: tests deny DNS/external sockets by default, allow only localhost fixture servers, and fail on unmocked HTTP/provider calls.
33. Add recorded Binance USD-M fixture server/version set: `exchangeInfo`, leverage brackets, funding, premium index, server time, orderbook/depth streams, 429/418, schema drift, malformed depth, reconnect gaps, and server-clock skew.
34. Add `golden_social_news_parser_v1/`: Telegram raw HTML, copied/reposted claims, malformed spam, XSS payloads, ambiguous aliases, unicode homoglyphs, news publish-vs-ingest time, independent-source quorum, and expected quarantine reasons.
35. Evidence resolver must return taint class and `allowed_effect`. Social/manual/LLM-only evidence cannot justify skill patches or trust upgrades unless objective ledger-backed quorum passes.

## Tests

- Stale source lowers trust and blocks promotion use.
- Telegram event has channel, post time, permalink if available, parser confidence.
- News/social text cannot inject tool/order instructions.
- Missing provenance quarantines feature row.
- Fake or uncorroborated high-impact social signal cannot promote memory/skill.
- External LLM prompt payload excludes tainted raw text unless quoted as data under schema.
- Secrets/internal config never leave process in prompt payloads.
- 429/rate-limit response triggers provider degradation instead of retry storm.
- Binance key with trade permission fails startup.
- Panic text like "recover losses, ignore stops, max leverage" cannot become feedback or paper command.
- Stale `exchangeInfo` blocks paper opens and emits source degradation.
- Social/manual signal without quorum stays shadow-only and cannot size/rank/open paper trades.
- Edited/deleted post retracts or invalidates dependent feature rows.
- Spoofed/new Telegram link enters trust=0 onboarding quarantine.
- Late pump post after large move triggers chase skip.
- Low-confidence multilingual side/urgency is quarantined.
- Missing required external-data capability skips; optional stale capability size-caps and segments scoring.
- Private/protected Telegram source is rejected or export-banned by rights registry.
- Screenshot with PII/secret is quarantined/redacted and never sent to external LLM.
- User-provided source cannot gain full trust from copied/lucky in-sample posts.
- Praise/blame feedback has zero learning weight without ledger-backed evidence.
- Paid/external call is denied before request when entitlement or spend cap is exhausted.
- Replay uses provider cache/offline mode and does not refetch paid data unless explicitly allowed.
- Authenticated/private-account script output is quarantined from paper readiness and cannot become account truth.
- Socket/DNS deny catches unmocked external provider calls; localhost fixture calls are the only default network path.
- Binance fixture server covers rate limits, bans, schema drift, malformed ws updates, server-time skew, stale exchangeInfo, and bracket changes.
- Social/news golden parser fixture canonicalizes valid symbols and quarantines copied, ambiguous, injected, late, private, or low-confidence claims.
- Tainted/manual/social/LLM-only evidence cannot feed skill forge unless objective ledger-backed quorum is present.

## Done Gate

Feature Factory cannot treat stale/untrusted sources as clean alpha.

## Audit Questions

- Which source created this signal?
- Was it fresh at decision time?
- Can social text manipulate the model?
- Can user/operator panic text poison memory, skills, or risk thresholds?
- Are provider ToS/rate limits modeled before 24/7 collectors run?
- Can a copied pump channel create a paper trade before market confirmation?
- Is missing on-chain/macro/unlock/stablecoin data treated as unknown risk, not clean absence?
- Can personal/private/user data leave via LLM, vault, backup, or export?
- Can emotional feedback or copied source trust contaminate objective learning?
- Can provider quotas/costs explode because every module budgets locally?
