# Phase 22: Read-Only MCP Boundary

## Overview

Expose safe tools for future agent clients without live execution.

Phase 22 is optional only if no MCP/tool surface exists. If any local/external client, dashboard command endpoint, LLM tool bridge, or queue API is exposed, this phase becomes a prerequisite before exposure.

## Related Code

- New MCP server module if needed.
- `memory_retrieval.py`
- `real_scoring_board.py`
- `market_feature_store.py`
- `paper_portfolio_manager.py`

## Read-Only Tools

- Get market feature snapshot.
- Search memory.
- Get paper account/scoring.
- Read dashboard status.

## Paper-Research Command Tools

These are not read-only. They must live in a separate command server/process/port/token issuer from read-only MCP, disabled by default behind a local capability token, quota, and schema allowlist:

- Propose paper candidate.
- Queue backtest/replay job.

Command tools also require approval-policy metadata for any risk/execution/config/threshold/tool-capability effect. They cannot loosen risk, enable tools, change thresholds, mutate memory/skill/scoring, or consume panic/override-risk text.

## Transport And Capability Contract

- Auth even on localhost; strict Host/Origin; CSRF protection on mutations; no browser-readable command endpoint; no cookies for command tools.
- Per-tool scoped opaque tokens with short TTL, nonce/challenge, request-hash binding, revocation, hashed audit only, and no token/scope exposure in dashboard/latest.
- Strict JSON Schema per tool: `additionalProperties:false`, enum validation, size/depth caps, canonical request hash, idempotency key, homoglyph/nested override fuzz fixtures.
- No shell/subprocess in MCP. No arbitrary path/URL input. Read-only filesystem allowlist for read tools. Command tools call internal APIs only.
- MCP output envelope includes taint, content type, source ids, max text, redaction status, and no markdown/tool syntax by default for untrusted text.
- Every tool/job/event carries actor_id, client_id, capability_id, parent_call_id, chain_depth, max_effect, token hash, and quota charge.
- Signed MCP call ledger records every invocation, denial, request/response hash, schema digest, approval id, quota charged, parent call, and denial reason.
- Separate signed `operator_command` channel for risk-reducing actions only: pause paper opens, cancel pending, reduce paper exposure, activate kill switch. Commands are idempotent, non-learnable, and cannot loosen risk.
- Precedence is fixed: safety hard gates > kill switch/risk breakers > scoring gates > approvals > preferences. Approval conflicts create rejection events.
- External/MCP tools may not propose, apply, approve, promote, or rollback skill patches. They can only create quarantined research tickets with taint and evidence refs.
- Operator command policy includes allowed actions, forbidden actions, role, reason code, expiry, post-action verification, idempotency, and signed audit event. Risk-increasing, live-like, approval-loosening, or learning-mutating commands are forbidden.
- Dashboard/tunnel exposure command requires owner approval, TTL, scope/IP/token policy, token rotation, public-link audit event, and emergency shutdown.
- Central approval schema from Phase 02 is required for any command with risk/config/threshold/tool-capability effect; denial events are immutable and replayable.

## Forbidden Tools

- Place live order.
- Change live permission.
- Modify `.env`.
- Clear kill switch without explicit local action.
- Run shell/subprocess, accept arbitrary path/URL, call legacy scripts, or access private live account state.
- Start skill patch/promotion/approval workflow directly.

## Tests

- MCP tools cannot import live execution modules.
- Tool responses are sanitized.
- Read-only tools return evidence ids.
- Permission test proves no live order tool exists.
- Paper-research command tools require local capability token and quota.
- Command tools cannot mutate memory/skill/scoring directly.
- Command tool request that changes risk/config/threshold/capability is rejected without approval manifest.
- Panic/revenge-trade input cannot become a queued paper command.
- Read-only and command servers cannot share token issuer/port/process.
- Stolen/replayed token, bad Origin, missing nonce, or expired TTL is rejected.
- Nested/unknown/homoglyph schema fields are rejected.
- MCP cannot spawn shell, read `.env`, accept arbitrary paths, or call legacy scripts.
- Tainted tool output remains tainted through client/job/event chain.
- Rogue token/job flood appears in signed MCP call ledger and is quota-blocked.
- Risk-reducing operator command works without becoming learning feedback.
- Human approval cannot loosen a failed safety/risk/scoring gate.
- MCP attempt to propose/apply/promote a skill creates only a quarantined research ticket and cannot touch skill ledger.
- Operator commands require signer role, TTL, reason, idempotency key, and post-action verification.
- Tunnel exposure without approval/TTL/token/scope is rejected and emergency shutdown revokes it.

## Done Gate

External clients can inspect and request paper research without increasing live risk.

## Audit Questions

- Can MCP become a backdoor to shell/order placement?
- Are all tools read-only or paper-only?
- Can a "paper command" indirectly change risk gates or tool capabilities?
- Can a localhost webpage or stolen token invoke command tools?
- Can tool output poison another tool/job/LLM path?
- Is this a safe operator command or learning feedback?
- Can an external client start a skill patch or public dashboard exposure without owner approval?
