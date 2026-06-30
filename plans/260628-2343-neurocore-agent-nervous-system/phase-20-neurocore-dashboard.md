# Phase 20: NeuroCore Dashboard

Status: Complete

Completed: 2026-06-30

Implementation report: [260630-phase-20-implementation-report.md](./reports/260630-phase-20-implementation-report.md)

## Overview

Add compact UI for the nervous system: topology, event flow, feature health, scoring, memory, skills, experiments, ops.

## Related Code

- `agent_status_dashboard.py`
- `learning_dashboard_data.py`
- `tests/test_agent_status_dashboard.py`

## Implementation Steps

1. Add `NeuroCore` nav tab with internal sub-tabs and URL state.
2. Add topology view: observers -> bus -> feature factory -> paper -> learning -> memory/skills.
3. Add event bus metrics: lag, DLQ, consumer offsets, throughput.
4. Add feature health: missing rate, source trust, latest feature rows.
5. Add scoring board: PF/expectancy/DD/uncertainty by window.
6. Add memory/skill lineage with evidence ids.
7. Add chart hover tooltips, keyboard/touch equivalents, Vietnamese labels.
8. Add drilldown by `trade_id`, `event_id`, `skill_patch_id`, `experiment_id`.
9. Bind dashboard to localhost by default, deny CORS by default, and require a local token if binding beyond localhost.
10. Escape/sanitize all untrusted text from Telegram/news/manual sources in UI and API.
11. Add cache-control and redaction for sensitive strategy/detail payloads.
12. Add triage hierarchy: default view shows health/readiness blockers first, with progressive drilldown.
13. Stage 1 minimal drilldown must support signal -> paper decision -> replay/score -> memory ids for at least one golden e2e trade.
14. Add widget freshness contract: `as_of`, TTL, source watermark, stale color state.
15. Add chart contract: axis units, denominator, costs included flag, sample/effective sample, CI method.
16. Add glossary for Vietnamese labels and retained English terms: PF, DD, expectancy, liquidation, uncertainty, source trust.
17. Add local security: Host allowlist, Origin/CSRF policy, DNS rebinding defense, token storage/rotation, `no-store` on sensitive payloads.
18. Add paper-only/futures-risk disclaimer to dashboard, reports, exports, and readiness views: educational paper research, not financial advice, no adviser/broker role, no profit guarantee, futures liquidation risk.
19. Add advice-boundary ledger for user-facing responses: request hash, response hash, classification, disclaimer shown, acknowledgement if required, evidence ids, retention policy.
20. Read dashboard bound port/server identity from canonical runtime port registry/latest; probes must verify owner/build id, not just HTTP 200.
21. Serve dashboard from immutable summary snapshots/materialized views with `snapshot_id`, seq bounds, source hashes, and read-only transaction consistency. Mixed/torn widgets render degraded.
22. Add payload budget: summary endpoint fixed size ceiling, field projections, cursor drilldowns, `limit/since`, gzip where applicable, and render/payload tests.
23. Define mandatory tooltip schema shared by hover, keyboard, touch, table, drilldown, and export: window, denominator, N/effective N, setup_contract_hash, cost completeness vector, CI/lower bound, snapshot id, source watermarks, staleness, readiness eligibility.
24. Separate monitoring vs readiness visuals: 10/25/50/100 monitoring windows are `promotion_ineligible`, grey/neutral, with visible N/effective N and lower bound.
25. Replace `costs included` boolean with per-point cost completeness vector: entry/exit fee, realized/estimated funding, slippage, liquidation fee, ADL/stress, margin haircut, unknown/missing flags.
26. Define equity chart semantics: ledger-derived marked equity only, realized/unrealized bands, fees/funding, deposits/resets, manual interventions, liquidation markers. Closed-trade-only curves cannot be labeled equity.
27. Add per-series/per-point freshness: `source_watermarks[]`, missing/stale/imputed/unknown counts; unknown renders as gap, not zero/carry-forward.
28. Default scoring charts facet by `setup_contract_hash`; cross-version aggregation requires visible compatibility declaration and rollup invalidation marker.
29. Ban pie/donut for readiness evidence. Stacked bars require common denominator, raw counts, 100% normalization label, and no dual-axis.
30. Add chart identity fields: `snapshot_id`, `metric_query_id`, `series_hash`, `point_hash`; visual glyph, tooltip, table, drilldown, and export must match.
31. Add accessibility contract: WCAG AA target, logical focus order, keyboard-only chart/table drilldown, ARIA labels, table fallback for charts, contrast checks, color-not-sole-signal, 44px touch targets, and reduced-motion mode.
32. Add mobile/data-density contract: explicit 360px/768px/desktop breakpoints, no horizontal page overflow, sticky critical blockers, compact tables with column priority, max widget height, drilldown pagination, and no card sprawl.
33. Tooltip truth parity: title, subtitle, axis labels, value formatting, timezone, bucket semantics, units, transforms, clipping, smoothing, normalization, missing/imputed points, and source watermarks must match chart/table/export.
34. Ban misleading chart encodings: dual axes, hidden zero/truncated axes without warning, zero-fill/carry-forward unknowns, smoothed equity as truth, green/pass colors for low-N/stale/inconclusive data, and pie/donut readiness evidence.
35. Add Vietnamese glossary coverage for visible labels, tooltips, exports, errors, empty states, disclaimers, retained English terms, number formatting, date/timezone formatting, and untranslated-key scan.
36. Add `golden_dashboard_chart_truth_v1.json`: ledger-derived equity, realized/unrealized, fees, funding, deposits, reset/capital events, manual intervention, liquidation marker, stale/unknown/imputed gaps, point hashes, cost vector, denominators, effective N, CI lower bound, and parity expectations.
37. Add split health endpoints/views: liveness, readiness, degraded dependencies, synthetic local probe, active incidents, alert queue health, silences, and error-budget burn.
38. Add tunnel exposure policy: Cloudflare/ngrok/Tailscale allow/deny mode, approval id, TTL, IP/token scope, token rotation, exposure audit event, public-link warning, and emergency shutdown command.
39. Top-level status equals max severity of mandatory widgets. Any stale/unknown mandatory source, active Sev1/Sev2, failed reconciliation, or forbidden wording prevents green.
40. UI may say "learned" only when changed memory/skill ids, evidence ids, and before/after deterministic decision diff are visible.
41. Ban green/pass styling based on win-rate alone. WR must be displayed beside payoff ratio, expectancy, lower bound, N/effective N, and cost completeness.
42. Equity chart must show deposit/reset/correction/manual rebalance markers and cannot connect across capital-event reset as one continuous performance curve.

## Tests

- Dashboard API returns NeuroCore payload when some files missing.
- Tooltip data exists for charts.
- Sub-tabs are stable by id, not DOM count.
- Vietnamese labels keep key English trading terms when useful.
- Untrusted news/Telegram text cannot render script/HTML.
- Non-local dashboard bind fails without explicit token config.
- Default view shows top blockers without scrolling.
- Stale widget cannot render as healthy.
- Keyboard/touch can inspect chart values and drilldowns.
- Readiness/export views show paper-only disclaimer and never say live eligible.
- Advice-boundary ledger captures user-facing trade/risk output hashes.
- Probe fails if 8090 is served by the wrong process or stale build id.
- Dashboard never mixes scoring/ledger/feature/memory states from incompatible seq ranges.
- `/api/status` summary stays under payload budget and drilldowns are paginated.
- Tooltip schema includes N/effective N, denominator, cost vector, CI lower bound, setup hash, source watermarks, and readiness eligibility.
- Monitoring window cannot render as readiness/pass evidence.
- Equity chart uses ledger-derived marked equity, not closed-trade-only PnL.
- Unknown/stale series points render as gaps/degraded, not zeros.
- Chart glyph, tooltip, table, drilldown, and export share the same point hash.
- Playwright smoke runs desktop/tablet/mobile viewports, console error fail, wrong-server probe, screenshot artifact on fail, keyboard chart drilldown, and no horizontal overflow.
- Accessibility tests verify focus order, ARIA/table fallback, contrast, color-not-sole-signal, touch target, and reduced-motion behavior.
- Tooltip/chart/table/export parity fixture passes for labels, units, timezone, bucket semantics, transforms, point hash, and missing/imputed disclosure.
- Dashboard chart tests fail on dual-axis, hidden truncation, zero-filled unknowns, closed-trade-only equity, or WR-only green/pass states.
- Glossary/untranslated-key scan covers labels, tooltips, exports, errors, empty states, and disclaimers.
- Tunnel exposure requires approval/TTL/token audit and can be emergency-shutdown without leaving public stale links.
- Top-level green is impossible with stale mandatory widget, active high-severity incident, unknown cost vector, or forbidden readiness/live wording.
- UI "learned" label requires evidence ids plus deterministic before/after decision diff.

## Done Gate

User can see "what is it doing, what did it learn, is it improving, why not ready?"

## Audit Questions

- Is data compact or forcing endless scrolling?
- Can user trace a trade from signal to lesson?
- Could UI wording be read as a live trading recommendation?
- Is the dashboard probing the real NeuroCore server or an unrelated process?
- Is this UI snapshot internally consistent or stitched from different moments?
- Does this chart make weak/small/stale data look trade-ready?
- Are costs, uncertainty, setup versions, and denominators visible at the point level?
- Can a mobile user see top blockers without scrolling through long pages?
- Can color, chart type, or headline wording make weak evidence look ready?
- Is a public tunnel exposed, approved, time-limited, and auditable?
