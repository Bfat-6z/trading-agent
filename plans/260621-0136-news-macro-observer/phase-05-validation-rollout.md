# Validation And Rollout

Status: Partial

## Context Links

- Parent plan: [News Macro Observer](./plan.md)
- Test command: `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; venv\Scripts\python.exe -m pytest tests -q`

## Overview

Validate the news/macro observer without enabling live trading. Roll it out first as observation, then tighten-only paper context, then 14-day readiness evidence.

## Requirements

- Unit tests for scoring, dedupe, stale handling, source failures, malformed data, no-secret logging, and tighten-only critic behavior.
- Smoke command: `venv\Scripts\python.exe news_observer.py --once`.
- Runtime check: supervisor starts the observer and dashboard reports fresh heartbeat.
- Paper/shadow logs include news context after integration.
- No live mode changes.

## Rollout Stages

1. Observe only: collect news state and dashboard visibility.
2. Shadow annotation: attach news regime to would-trades, no blocking yet except stale-data warning.
3. Paper tighten-only: enable block/tighten for high-risk news.
4. Readiness evidence: include news-health metrics in day-7 and day-14 reports.

## Todo Checklist

- [x] Add tests for ingestion adapters.
- [x] Add tests for deterministic scoring fixtures.
- [x] Add tests for critic tighten-only contract.
- [x] Run `news_observer.py --once` smoke.
- [x] Run full test suite.
- [x] Restart/verify `agent_process_supervisor.py`.
- [x] Verify dashboard at `http://127.0.0.1:8090/`.

## Risks

- Live internet/API instability can make tests flaky. Unit tests must use fixtures, not live API calls.
- News score thresholds should be calibrated from shadow/paper outcomes, not guessed into live trading.
