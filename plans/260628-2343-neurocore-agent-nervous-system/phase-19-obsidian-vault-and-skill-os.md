# Phase 19: Obsidian Vault And Skill OS

## Overview

Use Obsidian-style Markdown as readable memory/skill OS, not as source of trading truth.

## Related Code

- New `obsidian_vault_writer.py`
- `memory_consolidation_agent.py`
- `skill_forge_agent.py`
- `daily_exam_agent.py`
- `state/agent_memory/`

## Vault Layout

```text
vault/
  daily/
  trades/YYYY-MM-DD/
  skills/
  dont_do/
  experiments/
  source_notes/
  memory/
```

## Implementation Steps

1. Write daily reports from machine outputs.
2. Export each setup skill as versioned Markdown contract.
3. Export DONT_DO and promoted memories with evidence ids.
4. Export experiments and walk-forward outcomes.
5. Mark vault as read-only mirror. Event store/registry remains authoritative; vault skill docs are docs-only and can never mutate matcher/ranker/risk directly.
6. Use one-way generated folders plus separate `inbox/` for human feedback. Human imports create quarantined proposals only, never ground truth.
7. Define `human_feedback.imported` event: signer, scope, note hash, taint class, approval id, expiry, evidence refs, and default quarantine.
8. Add sync/conflict model: generated files include `source_digest`; human edits to generated files emit conflict events and are never imported automatically.
9. Add vault frontmatter: `generated_at`, `as_of_seq`, `source_snapshot_hash`, `expires_at`, `stale_after`, content classification, and source ids.
10. Treat all vault text as untrusted. Import only structured frontmatter plus quoted body; strip/taint YAML, wikilinks, embeds, code fences, Dataview/transclusions, Unicode controls.
11. Add link/evidence manifest: stable slugs, backlinks, tombstones/redirects, evidence id resolution, orphan check, and quarantine on invalid refs.
12. Add vault privacy policy: redaction/classification, sentinel secret scan, `.gitignore`/cloud-sync warning, private/public export modes, encrypted backup handling.
13. Add deterministic Markdown encoding: UTF-8 no BOM, LF, NFC normalization, deterministic YAML, Obsidian link escaping, Vietnamese slug/path tests.
14. Split public/private vault. Private vault is encrypted at rest; hard-fail export inside synced/Git directories unless explicit redacted public mode is selected.
15. Add vault retention/export policy: generated-note max count/size, daily rollups, archive/compress, tombstone propagation, source_digest drift audit, mirror rebuild from event ids, orphan generated file deletion, and stale-note retirement.
16. Define generated artifact policy: committed vs ignored paths, deterministic regeneration command, generated marker, artifact manifest hash, stale-generated diff fail, and cleanup quota.
17. Vault export uses bundle-level secret scan/redaction and never includes raw private/protected Telegram text, screenshots/OCR, `.env`, tokens, or internal strategy unless explicitly redacted.

## Tests

- Vault writer never reads `.env`.
- Markdown includes evidence ids and source paths.
- Human notes are not treated as ground truth unless labeled.
- Skill version export matches setup skill library.
- Edited generated skill Markdown cannot alter matcher/ranker/risk.
- Vault injection fixtures in YAML/wikilinks/embeds/code fences/Dataview are quoted/tainted and cannot reach LLM as trusted instruction.
- Broken link/evidence id fails export or moves note to quarantine.
- Stale vault note cannot be imported as current feedback.
- Vault export passes redaction/sentinel scan and deterministic encoding tests.
- Private vault export refuses cloud/Git-synced path or switches to redacted public mode.
- Vault retention prunes/archives generated notes without breaking evidence links or source digests.
- Rebuild from event ids reproduces generated notes and deletes orphan generated files.
- Generated artifact manifest detects stale generated diffs.
- Vault export bundle-level secret scan passes before share/tag/export.

## Done Gate

User can inspect what the agent learned day by day in Vietnamese-readable notes.

## Audit Questions

- Is the vault a mirror or accidental source of truth?
- Can stale notes override objective metrics?
- Can a Markdown edit, broken link, or prompt injection change trading behavior?
- Would syncing the vault leak private strategy/user data?
- Can generated vault files bloat, drift, or survive tombstone/erasure incorrectly?
