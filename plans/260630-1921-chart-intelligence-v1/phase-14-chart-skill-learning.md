# Phase 14: Chart Skill Learning

## Overview

Let chart evidence improve skills and DONT_DO rules safely.

## Related Code

- `skill_forge_agent.py`
- `setup_skill_library.py`
- `memory_consolidation_agent.py`
- `dont_do_memory.py`
- `memory_retrieval.py`

## Requirements

- Add chart-specific skill fields: required structure, forbidden conditions, preferred regime, invalidation template, failure examples.
- Skill patches require enough post-trade chart reviews, sample size, and positive expectancy after costs.
- DONT_DO can capture repeated chart mistakes.
- Retire or downgrade skills with negative chart evidence.
- LLM can propose wording; deterministic gate decides.

## Implementation Steps

1. Extend skill forge evidence resolver for chart review ids.
2. Add chart skill patch type.
3. Add validation: min samples, no contradiction, cost-aware expectancy, replay coverage.
4. Add DONT_DO generator for repeated chart failure modes.
5. Add rollback metadata for skill patches.

## Tests

- Single trade cannot promote chart skill.
- Negative expectancy blocks promotion despite high winrate.
- Repeated liquidity-trap losses create DONT_DO candidate.
- LLM text without evidence stays hypothesis-only.
- Retired skill cannot affect scorer.

## Done Gate

Chart learning changes skills only with objective evidence.

## Audit Questions

- Can the agent become overconfident from low-N chart data?
- Are old beliefs retired when contradicted?

