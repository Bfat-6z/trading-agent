# Phase 15: Skill Evolution V2

## Overview

Make Hermes-style skill evolution real: create, patch, test, apply, monitor, revert, retire.

## Related Code

- `skill_forge_agent.py`
- `setup_skill_library.py`
- `setup_ranker.py`
- `capital_allocation_policy.py`
- `walk_forward_validator.py`

## Implementation Steps

1. Make skill patches materialize behavior in matcher, ranker, risk, and allocation.
2. Add patch types: new setup, split by regime, invalidation, SL/TP template, leverage cap, setup retirement.
3. Require evidence ids: review, replay, shadow, score, memory.
4. Add lifecycle: proposed -> shadow_test -> walk_forward -> paper_applied -> monitored -> reverted/promoted.
5. Add rollback criteria for every applied patch.
6. Version setup skills as procedural contracts.
7. Enforce hard patch scope: skill patches may only touch setup/ranker/risk metadata paths, never firewall, config, secrets, live execution, supervisor, or `.env`.
8. Add allowlist/denylist, patch signature/hash, and safety scan before any patch is applied.
9. Require human approval for any code mutation. Default skill evolution changes data contracts/metadata only.
10. Add atomic apply/rollback manifest: before/after hash, canary window, rollback thresholds, revert event, dependency invalidation.
11. Forbid dependency, import, postinstall, test-path, secret/config, firewall, supervisor, MCP capability, and live-execution changes from skill evolution.
12. Add approval matrix for any risk/execution/config/threshold/tool-capability behavior change, including "metadata-only" leverage caps and allocation thresholds.
13. Store human-signed manifest fields: signer, reason, scope, TTL, nonce, before/after hash, evidence ids, approval timestamp, and rollback owner.
14. Run patch tests in isolated worktree with scrubbed env and fake provider/network defaults.
15. Add patch ledger with single applier queue, current skill version CAS, base-version check, global patch lock, and rollback head check/inverse patch handling.
16. Define canonical setup ontology: `setup_contract_id`, semantic version, contract hash, matcher/ranker/risk versions, allowed sides/timeframes, required features/capability contract, entry, stop, TP, time exit, invalidation before/after entry, no-trade criteria, source masks, regime policy.
17. Define immutable pre-entry `setup_quality_tier` enum and A+/5A+ rubric: numeric score, required evidence, scorer version, cutoff proof, and no post-outcome relabeling.
18. Persist `setup_id`, `setup_version`, `setup_contract_hash`, `setup_quality_tier`, matcher/ranker/risk versions on every candidate, skip, open, close, replay, score row.
19. Support `setup_matches[]`, `primary_setup_id`, pre-entry match scores, attribution policy, conflict rules, and primary/fractional scoring choice.
20. Add setup lifecycle events: proposed, active, superseded, retired; consumers must invalidate rollups, recall, memory scope, dashboard badges, vault docs, and active trials.
21. Add cold-start lifecycle: `shadow_seed -> micro_paper -> capped_paper -> normal`, priors, min days/symbols/regimes, max burn budget, and rollback on decay.
22. LLM labels are annotation only. Canonical setup/tier/failure labels come from deterministic matcher/scorer with evidence ids and calibration tests.
23. Define canonical `skill_promotion_manifest`: candidate artifact digest, base version, target version, test run ids, replay ids, walk-forward ids, scoring snapshot hash, approval id, approver role/key id, expiry, rollback plan, promoted version, and proof bundle hash.
24. Skill evolution may never edit its judges: `promotion_board.py`, `real_scoring_board.py`, `walk_forward_validator.py`, evidence resolver, eval corpus, graders, fixtures, approval logic, firewall, or any scoring/promotion gate.
25. Promotion is blocked unless baseline suite, targeted skill tests, replay, walk-forward, prompt/taint regression, secret/network scan, and rollback rehearsal pass on the exact candidate digest in a clean worktree. Candidate patches cannot supply or modify their own tests/fixtures.
26. Approval authority model: proposer != approver, signer role/key id, m-of-n quorum for risky patches, TTL, revocation, denial events, and replay validation.
27. Rollback gate: rehearse downgrade/rollback before promotion, prove CAS head restoration without deleting newer patches, define emergency disable path, and record rollback drill id in promotion manifest.
28. Evidence resolver must return taint class and `allowed_effect`; tainted/social/manual/LLM-only evidence is rejected for skill patch justification unless objective ledger-backed quorum exists.
29. Add semantic conflict detector: same setup opposite risk caps, DONT_DO conflict, retired setup resurrection, memory contradiction, scoring incompatibility. Precedence is safety > DONT_DO/risk > scoring > skill patch > preference.
30. Add immutable skill registry: parent version, supersedes, compatibility class, migration notes, consumer compatibility, deprecation/tombstone, reproducible artifact bundle, and source digest.
31. Add `learning_claim` output: a skill patch may claim learning only when changed skill ids and deterministic before/after decision diffs exist.

## Tests

- Patch changes candidate match/risk behavior in paper.
- Negative expectancy cannot create loosened risk.
- Reverted patch no longer affects decisions.
- New setup remains paper-only until OOS passes.
- Patch touching safety/config/live paths is rejected.
- Unsigned or unreviewed code mutation is rejected.
- Failed canary triggers rollback and dependency invalidation.
- Rollback manifest restores exact previous skill state.
- Metadata-only risk/leverage threshold change is rejected without approval manifest.
- Patch adding dependency/import/postinstall/test bypass is rejected.
- Scrubbed-env isolated test proves patch cannot read real secrets.
- Rollback of patch A cannot erase newer patch B; base/head mismatch requires inverse patch path.
- Unknown setup contract or post-outcome A+/5A+ label is rejected.
- Entry/exit/invalidation criteria are frozen pre-entry and cannot move after outcome.
- Multi-setup candidate attribution is deterministic and replayable.
- Retired setup is removed from active recall/ranking and marked stale in dashboard/vault.
- Cold-start setup cannot jump directly to normal allocation.
- Promotion fails without a complete `skill_promotion_manifest` matching candidate digest, test ids, scoring snapshot, approval, and rollback drill.
- Patch touching scorer/evaluator/evidence resolver/fixtures/graders/promotion gates is rejected as judge tampering.
- Candidate cannot modify or add the tests that certify its own promotion.
- Approval quorum/role/TTL/revocation/proposer-approver separation is enforced.
- Poisoned or tainted evidence cannot justify skill patches without objective ledger-backed quorum.
- Semantic conflict detector blocks incompatible concurrent proposals and records precedence.
- Skill registry lineage, compatibility, tombstone, and rollback head are replayable.
- Skill `learning_claim` without before/after deterministic decision diff is downgraded to `hypothesis_only`.

## Done Gate

Skill evolution is observable and behavior-changing, but still paper-only.

## Audit Questions

- Did the skill patch actually affect a decision?
- What evidence justified the patch?
- Did a "metadata" patch silently change risk or execution behavior?
- Who signed this patch, for how long, and how is it rolled back?
- Is patch apply/rollback serialized against current skill head?
- What exact setup contract and quality tier was known before entry?
- Can a subjective LLM/user label become canonical?
- Did the skill patch edit its own judge or tests?
- Is promotion reproducible from a signed promotion manifest?
