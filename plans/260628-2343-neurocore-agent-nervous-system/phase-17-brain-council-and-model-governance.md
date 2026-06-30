# Phase 17: Brain Council And Model Governance

## Overview

Make the LLM council real, traceable, and safe. Ensure 9router `cx/gpt-5.5` usage where configured.

## Related Code

- `llm_council.py`
- `llm_reasoning_agent.py`
- `model_router.py`
- `model_usage_ledger.py`
- `llm_output_quality_gate.py`
- `live_permission_firewall.py`

## Implementation Steps

1. Unify provider env names and effective model snapshot.
2. Add explicit routes for council roles and synthesis.
3. Verify configured deep model (`cx/gpt-5.5`) is actually called when available.
4. Enforce quorum: risk critic required, min accepted roles, disagreement record.
5. Make degradation fail-closed and ledger-accurate.
6. Apply recursive sanitizer from Phase 0 to all council outputs.
7. Apply input redaction and field allowlist before sending prompts to external providers.
8. Add data classification: public market data, internal strategy, secrets, personal/user notes. Secrets and raw `.env` never leave process.
9. Record provider retention/egress policy in model snapshot.
10. Track prompt tokens, completion tokens, cost, retries, latency, and request ids per role.
11. Enforce daily/run/role token and cost budgets; budget exhaustion fails closed.
12. Capture actual provider response model id, route reason, fallback reason, and no-fallback policy for safety-critical roles.
13. Define quorum table: required roles, risk veto, timeout-as-abstain, provider outage behavior, degraded role handling.
14. Use hermetic provider contract tests with fake/recorded provider responses; live provider smoke is opt-in and quarantined.
15. Use Phase 00 typed config loader only; no direct provider/env reads inside council modules.
16. Tests run with ambient env cleared, fake sentinel keys only, network disabled unless the live smoke flag is explicitly set.
17. Apply global redaction filter to request/response logs, provider errors, retry traces, usage ledger, and dashboard summaries.
18. Egress proof is required before every external LLM call. Personal/user notes, screenshots/OCR, private social text, secrets, and internal strategy are blocked by default.
19. Add appeasement/user-preference evals: LLM cannot loosen risk, create canonical setup/A+ labels, or optimize for user preference over deterministic expectancy.
20. Define role-specific JSON/Pydantic schemas for every council role and synthesis: enums, required fields, `additionalProperties:false`, parse-fail as abstain/reject.
21. Add effective-model canary gate: run versioned evals on model/provider/router snapshot changes and daily; quarantine route on schema, hallucination, refusal, injection, or preference regression.
22. Add cost/quality routing policy: per-role quality minimums, safety/risk budget reserve, eval-certified cheap/deep routing, burn-rate alerts, and fallback quality floor.
23. Use global `resource_budget_ledger` with user/session/root_request_id, pre-call reservation, post-call charge, and signed budget-exhaustion events.
24. Add degraded-mode matrix for LLM roles: required/optional, exhaustion action, paper-trading impact, readiness impact, dashboard state.
25. Add memory/skill indexing budget reservations to the global resource ledger: summarization, clustering, embeddings, rebuild, vault export, eval replay, prompt trace replay, and model canary.
26. LLM outputs that describe lessons without deterministic downstream memory/skill/decision delta are classified `hypothesis_only`, not `learned`.
27. Network/provider tests are socket-denied by default and may use only fake/recorded provider fixtures unless an explicit quarantined live-smoke flag is set.

## Tests

- Role route uses deep model when configured.
- Missing provider writes degraded payload, no crash.
- Nested live intent rejected.
- Council synthesis fails if risk critic missing.
- Usage ledger includes model, provider, latency, request id, quality gate result.
- Prompt payload to provider contains no secret/config/live-order fields.
- Cost/token budget exhaustion blocks council jobs.
- Safety-critical role cannot silently fall back to weaker model.
- Risk critic veto blocks synthesis.
- Fake provider contract proves actual response model id is captured.
- Provider tests fail if real-looking API keys are present or network is used without live smoke flag.
- Sentinel secret does not appear in provider ledger, prompt logs, exception text, or dashboard.
- Blocked data class cannot leave process even if quoted as data.
- LLM-authored setup/tier/preference label remains annotation-only.
- Unknown/extra council output field is rejected and cannot reach synthesis.
- Effective model alias/update triggers canary eval before route remains active.
- Budget exhaustion follows explicit degraded matrix, not ad hoc behavior.
- Memory/skill consolidation and retrieval spend is reserved/charged centrally and follows degraded mode on exhaustion.
- LLM "lesson learned" output without deterministic consumer delta is classified `hypothesis_only`.
- Provider tests fail on unmocked network or ambient live keys.

## Done Gate

The "big brain" is verifiably connected, safe, and auditable.

## Audit Questions

- Did it really use GPT-5.5/9router?
- Did council output change a deterministic proposal or just text?
- Did any provider call receive secrets, raw env, or panic/override-risk user text?
- Is this model route still certified after provider/model drift?
- Did cost exhaustion pause, degrade, or invalidate the right subsystem?
