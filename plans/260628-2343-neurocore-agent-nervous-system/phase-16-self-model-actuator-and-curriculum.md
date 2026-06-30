# Phase 16: Self-Model Actuator And Curriculum

## Overview

Turn self-model from a dashboard snapshot into a scheduler for learning tasks.

## Related Code

- `self_model.py`
- `test_result_memory_agent.py`
- `learning_exam_benchmark.py`
- `curiosity_scheduler.py`
- `agent_work_queue.py`

## Implementation Steps

1. Rank repeated gaps from tests/exams/scoring/replay.
2. Create curriculum tasks: collect data, run replay, test skill patch, review weak setup.
3. Queue tasks into event/work queue with priority and evidence ids.
4. Add daily "homework" score: tasks assigned, completed, learned.
5. Prevent self-model from loosening risk or live permissions.
6. Add anti-loop controls: task budget, cooldown by gap type, max retries, source partition, and circuit breaker for self-generated failures.

## Tests

- Low replay coverage queues replay data task.
- Weak setup queues experiment or retirement review.
- Same repeated failure rises in priority.
- Self-model task can be traced to completion/outcome.
- Repeated self-generated task loop is throttled and reported.

## Done Gate

Agent can answer "what should I learn next and why?"

## Audit Questions

- Is curriculum based on evidence or vibes?
- Did queued learning task complete and update memory/skill?
