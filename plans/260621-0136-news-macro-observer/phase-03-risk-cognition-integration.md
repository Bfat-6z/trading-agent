# Risk And Cognition Integration

Status: Partial

## Context Links

- Parent plan: [News Macro Observer](./plan.md)
- Integrates: `inner_critic.py`, `cognitive_supervisor.py`, `reasoning_trace.py`, `dream_cycle.py`, `scalp_autotrader.py`

## Overview

Wire news context into the existing self-thinking loop so the agent understands the market regime better while keeping execution gates deterministic and conservative.

## Requirements

- `inner_critic.py` reads `news_latest.json` and can only return `tighten` or `block` due to news.
- `cognitive_supervisor.py` includes news regime in focus selection and hypothesis generation.
- `reasoning_trace.py` includes news observations, contradictions, missing evidence, and whether news is stale.
- `dream_cycle.py` includes headline shock scenarios in simulated risk patches.
- `scalp_autotrader.py` attaches the latest news regime snapshot to shadow/paper decision logs.

## Tighten/Block Rules

- Block if news is stale beyond configured max age during active trading hours.
- Block if macro/regulatory/headline chaos exceeds hard threshold.
- Block symbol if relevant hack/outage/delisting/lawsuit/depeg headline is fresh and high confidence.
- Tighten if news conflicts with the signal side or source quality is weak.
- Never lower `min_signal_score`, never increase leverage, never mark a weak setup as A+.

## Implementation Steps

- Add `load_news_context()` helper with safe default behavior.
- Extend critic verdict payload with `news_context`, `news_reasons`, and `news_max_age_seconds`.
- Extend supervisor state and reasoning trace schemas.
- Add tests showing high-risk news blocks, stale news blocks or flags, and benign news does not loosen.

## Todo Checklist

- [x] Add read helper and stale-status helper.
- [x] Integrate with `inner_critic.py` as tighten-only.
- [ ] Integrate with `cognitive_supervisor.py` focus/hypotheses.
- [ ] Integrate with `reasoning_trace.py` quality checks.
- [ ] Attach news snapshot to paper/shadow logs.
- [x] Add regression tests for no-loosen behavior.

## Risks

- Overblocking can reduce sample collection. Shadow logging should still record would-trades with the news block reason.
- Missing news should not crash the agent; it should become an explicit data-health issue.
