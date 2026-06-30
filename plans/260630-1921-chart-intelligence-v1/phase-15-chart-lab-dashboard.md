# Phase 15: Chart Lab Dashboard

## Overview

Add a compact, Vietnamese-first Chart Lab page with nested tabs instead of long scroll.

## Related Code

- `agent_status_dashboard.py`
- `learning_dashboard_data.py`
- `tests/test_agent_status_dashboard.py`

## Requirements

- Main tab: `Chart Lab`.
- Sub-tabs: `Tong quan`, `Da khung thoi gian`, `Vung gia`, `Cau truc`, `Thanh khoan`, `Lenh mo phong`, `Hoc tu chart`.
- Implement inside existing stdlib inline HTML/CSS/JS dashboard. Do not introduce React/Vite/Next for this phase.
- Keep Chart Lab read-only. Do not reuse mutating `log_server.py` `/tv-webhook` behavior.
- Symbol/timeframe selector.
- Annotated chart image viewer.
- Hover/keyboard/touch tooltips with exact value at cursor.
- Tables mirror chart values with same point ids.
- Show freshness, source, cutoff, confidence, blockers, score reasons.
- No pie/donut for readiness evidence.
- Respect `/api/status` payload budget; load heavy chart series/images through drilldown ids.
- Do not label closed-trade-only PnL curve as equity. Equity views must be ledger-derived or clearly named otherwise.
- Chart image API must use artifact ids, not raw file paths.

## Implementation Steps

1. Add Chart Lab API payload builder.
2. Add nested tab UI with URL state.
3. Add chart snapshot viewer and overlay legend.
4. Add evidence table: reason code -> source id -> confidence.
5. Add paper trade drilldown: entry chart, close chart, post-trade review.
6. Add Vietnamese labels and glossary for retained English terms.
7. Add mobile/desktop layout tests.

## Tests

- `/api/status` includes Chart Lab summary without huge payload.
- Missing snapshots render degraded state.
- Tooltip, table, drilldown, export share point hash.
- Keyboard can inspect chart points.
- No horizontal overflow on 360px.
- Untrusted text is escaped.
- Vietnamese labels cover visible UI, errors, empty states.
- Mutating webhook/log UI routes are not exposed through Chart Lab.
- Closed-trade-only curve cannot be labeled `equity`.
- Chart artifact route rejects path traversal and unknown artifact ids.

## Done Gate

User can inspect why the agent entered/skipped a trade from chart evidence.

## Audit Questions

- Can user see critical chart blockers above the fold?
- Does UI make weak chart evidence look stronger than it is?
