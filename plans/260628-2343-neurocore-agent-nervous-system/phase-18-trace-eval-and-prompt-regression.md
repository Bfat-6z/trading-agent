# Phase 18: Trace Eval And Prompt Regression

## Overview

Borrow Langfuse/Opik/AgentOps/promptfoo patterns locally: trace every reasoning path and regression-test prompts/sanitizers.

## Related Code

- `reasoning_trace.py`
- `model_usage_ledger.py`
- `llm_output_quality_gate.py`
- `tests/`

## Implementation Steps

1. Add trace schema: schema_version, run_id, parent_id, event_id, source/provenance ids, model, prompt_version, gate_result, outcome, payload_hash.
2. Link LLM traces to paper decision/review/skill patch ids.
3. Build fixture evals for unsafe live prompts, fake news, hallucinated evidence, weak skill patch.
4. Add prompt/council/sanitizer regression tests.
5. Add trace dashboard payload.
6. Add evidence resolver tests for real-but-irrelevant, stale, wrong-type, and wrong-window ids.
7. Add prompt-injection corpus with Unicode homoglyphs, markdown/tool text, quoted JSON, and indirect developer/system override language.
8. Add property/fuzz or mutation-style tests for firewall, coverage denominator, stale walk-forward, and evidence resolver gates.
9. Add eval label contract: `eval_case.yml/jsonl` with input, context, expected structured output, expected label, forbidden fields/actions, severity, grader version, adjudicator, pass/fail threshold.
10. Store golden prompt trace bundles: rendered prompt, completion, router config, tool context, model params, schema, sanitizer version, labels, refusals, evidence refs, cost/latency.
11. Replay golden traces on prompt/router/model/schema/sanitizer changes and diff structured output, labels, denials, evidence refs, cost, and latency.
12. Add claim-grounding triples: claim, evidence id, field path, derivation. Valid id with unsupported claim must fail or abstain.
13. Add second-order injection traces: source -> storage/FTS/vault -> recall -> LLM -> tool/job with taint assertions and final denial.
14. Add preference eval taxonomy: `deny`, `ui_only`, `research_only`, `risk_reducing_command`, `quarantine` across memory, source onboarding, MCP, vault import, and council synthesis.
15. Add memory recall eval corpus to prompt regression: query, expected/forbidden memories, stale/contradiction cases, precision/recall/false-block labels, latency/cost budget, and grader version.
16. Promotion/prompt eval must run on exact candidate digest in clean worktree; candidate patches cannot modify eval cases, graders, fixtures, or prompt trace oracles.
17. LLM "learning" claims require grounding triples plus downstream deterministic delta. Without delta, trace label is `hypothesis_only`.

## Tests

- Unsafe prompt cannot produce live permissions.
- Hallucinated evidence id fails quality gate.
- Real but irrelevant/stale evidence id fails quality gate.
- External text is quoted as tainted data, not executable instructions.
- Inverted safety/coverage/evidence conditions are caught by tests.
- Skill patch without invalidation/rollback fails.
- Trace links LLM recommendation to deterministic final decision.
- Eval corpus has explicit labels/oracles and fails on schema or label regression.
- Prompt trace replay catches weakened sanitizer/prompt/router behavior.
- Real evidence id with invented field claim fails grounding check.
- Persisted tainted text cannot become instruction after recall/vault roundtrip.
- Memory eval corpus catches stale/forbidden recall, contradiction misuse, false-block regressions, and cost/latency budget breaks.
- Candidate patch cannot alter eval corpus/fixtures/graders used to certify itself.
- LLM learning claim without memory/skill id delta and decision diff fails grounding.

## Done Gate

LLM reasoning becomes inspectable engineering output, not hidden text.

## Audit Questions

- Can we replay why the model said something?
- Did a prompt change silently weaken safety?
- Did model output obey role schema and cite evidence fields correctly?
- Can old tainted text become a future tool instruction?
