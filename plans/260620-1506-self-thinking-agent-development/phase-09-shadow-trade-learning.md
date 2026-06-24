# Phase 09: Shadow Trade Learning

## Context Links

- [Plan](./plan.md)
- Builds on phases 05-06
- Existing: `scalp_autotrader.py`, `inner_critic.py`, `reasoning_trace.py`

## Overview

Priority: P1. Collect would-trade samples while risk gates, memory sleep, or inner critic block execution.

Status: Complete.

## Requirements

- Create `shadow_trade_logger.py`.
- Shadow records must include signal, order plan, entry, SL, TP, block reason, and critic payload when available.
- Shadow records must never create live or paper positions.
- Executor should keep collecting learning samples during sleep/block states.

## Related Code Files

- Create: `E:\keo-moi-mail\trading-agent\shadow_trade_logger.py`
- Create: `E:\keo-moi-mail\trading-agent\tests\test_shadow_trade_logger.py`
- Modify: `E:\keo-moi-mail\trading-agent\scalp_autotrader.py`
- Modify: `E:\keo-moi-mail\trading-agent\tests\test_scalp_autotrader.py`

## Completion Notes

- Added shadow trade records under `state/agent_memory/shadow_trades.jsonl`.
- `scalp_autotrader.tick` now scans raw signals and logs a throttled `shadow_open` when risk blocks new execution in PAPER mode.
- `open_paper` now logs a shadow sample when `inner_critic` blocks a candidate.
- Shadow records are explicitly marked `no_execution: true` and do not mutate `paper_position`.
- Added deterministic TP/SL evaluation helper for later replay analysis.
- Verification: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 venv\Scripts\python.exe -m pytest tests -q` -> `111 passed, 3 warnings`.
- Runtime smoke test produced a `memory_sleep` shadow sample without opening paper/live trade.

## Safety

- No live orders.
- No paper positions are created by shadow logging.
- Duplicate shadow logs are throttled by `--shadow-log-interval-seconds`.
