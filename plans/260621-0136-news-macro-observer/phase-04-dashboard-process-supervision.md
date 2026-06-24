# Dashboard And Process Supervision

Status: Complete

## Context Links

- Parent plan: [News Macro Observer](./plan.md)
- Integrates: `agent_status_dashboard.py`, `agent_process_supervisor.py`

## Overview

Make the news layer visible and keep it alive through the local process supervisor.

## Requirements

- `agent_process_supervisor.py` supervises `news_observer.py` with a heartbeat freshness limit.
- `agent_status_dashboard.py` shows news observer process status, source health, latest risk scores, top headlines, and stale/missing warnings.
- Dashboard remains a single UI at `http://127.0.0.1:8090/`.
- Failure states should be obvious: missing key, source HTTP failure, stale news, malformed payload, no fresh headlines.

## Implementation Steps

- Add `news_observer` to supervisor specs.
- Add heartbeat file to dashboard core heartbeats.
- Add a news panel/view backed by latest JSON.
- Keep UI dense and operational; no marketing/explanatory hero layout.
- Smoke-test dashboard API and rendered page after integration.

## Todo Checklist

- [x] Add process supervisor spec.
- [x] Add dashboard heartbeat mapping.
- [x] Add news summary API payload.
- [x] Add UI panel/table for news risk and headlines.
- [x] Run dashboard smoke test on port 8090.

## Risks

- Dashboard can become noisy. Show top risk reasons and source state, not every headline.
- Supervisor should not manage live execution; keep `scalp_autotrader.py` under `scalp_watchdog.py` only.
