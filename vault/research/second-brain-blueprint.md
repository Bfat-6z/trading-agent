---
tags: [research, memory]
---

# Second Brain Blueprint (15-agent research)

# Second Brain — Design (15-agent Fable-5 research, 2026-07-06)

All 15 writeups converge hard. Synthesis follows.

---

# SECOND BRAIN v1 — Implementation Design (trading-agent)

## 1. VERDICT

Build a **single-file SQLite ground-truth store (`memory/brain.db`) fed by an append-only event log (`memory/events.jsonl`), written exclusively by deterministic Python inside the existing pipeline (`deep_validation.py`, `forward_test.py`, `method_lab_runner.py`), with the LLM confined to read-only MCC tools and a quarantined proposals/annotations layer that never feeds any gate.** MEMORY.md becomes a nightly-regenerated build artifact rendered FROM SQL, never a source of truth. The novelty gate is a three-layer deterministic check (canonical-DSL-AST hash → behavioral/correlation hash → embedding advisory) that runs BEFORE any compute is spent and quotes dead records back verbatim. This is not one writeup's opinion — all 15 independently converged on "LLM proposes, deterministic code disposes," and the literature now formalizes your memory-laundering failure as a documented attack class (MemoryGraft arxiv.org/abs/2512.16962; OWASP ASI06; ACE's "context collapse" arxiv.org/abs/2510.04618; Mem0 itself abandoned LLM UPDATE/DELETE for ADD-only). The decisive extra payoff nobody builds by accident: the negative-results table doubles as the **trial registry required by the Deflated Sharpe Ratio** (Bailey & López de Prado, ssrn.com/abstract=2460551) — without it every future lockbox p-value is a lie. No graph DB, no Mem0/Zep/Letta, no vector DB product: SQLite + JSONL + sqlite-vec/FTS5 is 100% of the need at this scale.

## 2. STORES, SCHEMAS, DATA FLOW

### Layer 0 — Event log (episodic, source of truth)
`memory/events.jsonl` — append-only, hash-chained (`prev_hash` field per record for tamper evidence). Every proposal, validation run, lockbox burn, shadow fill, exit, and state transition is one JSON line written by pipeline code. `brain.db` is a **projection** of this log (event-sourcing pattern, ESAA arxiv.org/abs/2602.23193; OpenHands EventLog) — rebuildable from scratch by the projector, so a corrupt DB is never fatal.

### Layer 1 — `brain.db` ground truth (deterministic writes only)

```sql
PRAGMA journal_mode=WAL;

-- (b) NEGATIVE-RESULTS DB + trial registry. One row per tested method. IMMORTAL, never decays.
CREATE TABLE trials (
  trial_id      TEXT PRIMARY KEY,              -- ULID
  novelty_hash  TEXT NOT NULL UNIQUE,          -- sha256(canonical DSL AST + universe_id + timeframe)
  behavior_hash TEXT,                          -- sha256(quantized sign(daily signal) on FIXED probe window 2023-2024)
  dsl_canonical TEXT NOT NULL,                 -- full definition, verbatim, normalized
  family        TEXT,                          -- 'momentum','meanrev','vol','funding',...
  parent_trial  TEXT REFERENCES trials(trial_id),  -- mutation/failure lineage
  universe TEXT, period_start TEXT, period_end TEXT,
  dataset_hash TEXT, code_git_sha TEXT, seed INTEGER,   -- exact reproducibility
  n_trades INTEGER, is_sharpe REAL, oos_sharpe REAL,
  bootstrap_p REAL, n_bootstrap INTEGER, block_len INTEGER,
  lockbox_used INTEGER DEFAULT 0, lockbox_sharpe REAL,  -- lockbox burns are LOGGED
  n_trials_family INTEGER,                     -- running count → Deflated Sharpe input
  verdict TEXT CHECK(verdict IN ('DEAD','LOCKBOX_PASS','PENDING')),
  failure_mode TEXT CHECK(failure_mode IN ('no_signal','overfit_is','died_oos','died_lockbox',
      'cost_eaten','capacity','regime_only','low_n','sign_flip_oos','dup_of','alpha_decay',NULL)),
  dup_of TEXT REFERENCES trials(trial_id),
  returns_path TEXT,                           -- parquet of daily returns (DSR recompute later)
  as_of_data_cutoff TEXT,                      -- temporal-leak guard
  prev_row_hash TEXT, row_hash TEXT,           -- hash chain
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TRIGGER trials_no_upd BEFORE UPDATE ON trials BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER trials_no_del BEFORE DELETE ON trials BEGIN SELECT RAISE(ABORT,'append-only'); END;

-- (a) VALIDATED METHODS with bi-temporal state (Graphiti pattern: invalidate, never delete)
CREATE TABLE method_state (
  id INTEGER PRIMARY KEY,
  novelty_hash TEXT NOT NULL REFERENCES trials(novelty_hash),
  state TEXT CHECK(state IN ('lockbox_validated','shadow','live','probation','retired')),
  valid_at TEXT NOT NULL, invalid_at TEXT,     -- NULL = currently believed
  reason TEXT,                                  -- 'cusum_break','rolling_sharpe<0.5x_validation',...
  supersedes INTEGER REFERENCES method_state(id)
);

-- (c) LOSS LESSONS as NUMBERS. Two tables: raw per-trade autopsy + deterministic aggregates.
CREATE TABLE trade_autopsy (                   -- written on every shadow-ledger close
  trade_id TEXT PRIMARY KEY, novelty_hash TEXT, symbol TEXT, side TEXT,
  entry_ts TEXT, exit_ts TEXT, r_multiple REAL,
  mae_bps REAL, mfe_bps REAL, slippage_bps REAL, funding_paid_bps REAL,
  regime_tag TEXT, setup_tag TEXT, gate_flags_at_entry TEXT,
  exit_reason TEXT CHECK(exit_reason IN ('tp','sl','trail','time','manual','liq'))
);
CREATE TABLE lessons (                         -- DERIVED rows, recomputed by cron; machine-checkable
  lesson_id TEXT PRIMARY KEY,
  gate_expr TEXT NOT NULL,       -- executable predicate: 'ch24 > 0.12 AND side="LONG"'
  n INTEGER, win_rate REAL, avg_r REAL, worst_r REAL, mae_p95_bps REAL, p_value REAL,
  status TEXT CHECK(status IN ('candidate','active','retired')),  -- candidate→active only when n>=5, consistent sign
  evidence_trade_ids TEXT,       -- citation tree: leaves are ledger rows
  valid_at TEXT, invalid_at TEXT
);

-- LLM quarantine: writable, ZERO evidential weight, never consulted by any gate
CREATE TABLE annotations (id TEXT PRIMARY KEY, ref_id TEXT, author TEXT, text TEXT, created_at TEXT);
CREATE TABLE proposals   (id TEXT PRIMARY KEY, dsl TEXT, rationale TEXT, gate_result TEXT, created_at TEXT);
```

### Layer 2 — Indexes (derived, rebuildable)
FTS5 over `dsl_canonical` + `failure_mode`; sqlite-vec embeddings of the **canonical DSL text** (never the LLM's description — descriptions are what launder). Advisory only.

### Layer 3 — Working memory (rendered view)
Nightly deterministic cron regenerates `MEMORY.md` sections + `DEAD_IDEAS.md` + `LESSONS.md` from SQL templates with record-ID citations. If they drift: delete and regenerate. The existing `memory_consolidation_agent.py` (LLM consolidator) is **retired from the write path** — LLM consolidation is read-time briefing generation only (verbatim beats LLM-extracted memory: arxiv.org/pdf/2601.00821).

### Flow
```
method_lab proposes DSL ──> novelty_gate (deterministic, pre-compute) ──REJECT──> tombstone quoted verbatim into proposer prompt
        │ PASS                                                                      + logged to proposals.gate_result
        v
deep_validation runs ──> writer.py appends event → trials row (verdict, p, failure_mode, n_trials_family++)
lockbox pass         ──> method_state row (state='lockbox_validated', valid_at=now)
forward_test close   ──> trade_autopsy row → nightly cron recomputes lessons + staleness monitor
staleness (rolling 60-trade Sharpe < 0.5x validation, or CUSUM break) ──> method_state invalid_at stamped
                                                                          + NEW trials row failure_mode='alpha_decay'
                                                                            (so the proposer can never resurrect it)
```

## 3. READ/WRITE BOUNDARY + ANTI-LAUNDERING

**Single writer:** `memory/writer.py`, called only from `deep_validation.py`, `forward_test.py`, `method_lab_runner.py`, and the cron projector. It validates against JSON Schema/pydantic, computes hashes, extends the hash chain, `BEGIN IMMEDIATE`, appends JSONL, commits.

**LLM surface (Claude Code + Codex, shared MCP server, DB opened `SQLITE_OPEN_READONLY`):**
- READ tools: `check_novelty(dsl)`, `query_graveyard(filters)`, `get_lessons(regime_features)`, `get_lockbox_methods()`, `query_trials(sql_select_whitelist)`.
- WRITE tools: exactly two — `submit_proposal(dsl, rationale)` and `add_annotation(ref_id, text)` — both landing in quarantine tables. **There is no write tool to ground truth; the boundary is enforced at the MCP tool level, not by prompting.**

**Anti-laundering mechanisms (all deterministic):**
1. **Canonical AST hash is the hard gate.** Parse DSL → sort commutative ops → positional param renaming → bucket numeric params (lookback 20 ≈ 21). The LLM can rephrase prose; it cannot rephrase a canonical AST hash.
2. **Behavioral hash + correlation gate** (AlphaEvolve/FactorMiner pattern): signal correlation > 0.9 vs any DEAD trial on a fixed 2023–2024 probe window = duplicate, regardless of textual novelty. This is the workhorse; textual novelty with statistical identity is the main laundering escape route.
3. **Rejections quote raw rows verbatim** — an LLM-paraphrased rejection is itself a laundering channel (failure-modes writeup).
4. **Append-only triggers + hash chain + nightly audit** (recompute row hashes, diff index vs source). Any mutation of a trials row aborts.
5. **Laundering canary metric:** log every gate rejection; if the proposer re-submits a rejected idea reworded (behavioral-hash hit after a FLAG), that counter is a P0 alarm.
6. **Lesson promotion is mechanical:** candidate → active only after n≥5 occurrences, consistent sign (ExpeL upvoting made deterministic) — kills Reflexion-style causal confabulation on single trades.
7. **Bi-temporal + `as_of_data_cutoff`** on every record so replayed/backtested decisions can only see lessons derivable at decision time (kills self-inflicted lookahead, "Alpha Illusion" arxiv.org/html/2605.16895v1).

## 4. RETRIEVAL DESIGN

**Proposer gate (mandatory, pre-LLM-feedback, runs before ANY backtest compute):**
```python
def gate(dsl):
    canon = canonicalize(dsl)
    if sha256(canon) in exact_index:            return REJECT(row_verbatim)          # hard block, no appeal
    if behavior_corr_vs_dead(canon) > 0.90:     return REJECT(row_verbatim)          # statistical dup
    near = embed_knn(canon, k=5, min_cos=0.92)
    if any(n.verdict=='DEAD' for n in near):    return FLAG(near_rows_verbatim)      # proposer must state the
                                                # structural difference; verified by AST diff, not its own prose
    return PASS  # + inject 5 nearest DEAD rows into proposer prompt anyway (AlphaEvolve: negative context steers)
```

**Decision time (`mech_sizing` / futures_watch):**
- Evaluate all `active` lessons' `gate_expr` mechanically against candidate features — hard gates, not retrieved-by-vibes (your falling-knife ≥$50M rule becomes one row).
- Retrieve top-k=3–5 similar past episodes by (regime_tag, setup_tag, universe) — SQL keys first, embeddings fallback — as a compact numbers table, never narrative.
- **Negative-balance quota: force ≥20% of retrieved episodes to be losses.** The one live-measured intervention in the whole corpus (profit factor 2.42→0.94 with naive similarity retrieval; fixed by loss quota — temporal-kg writeup).
- Fixed context budget (~4k tokens), highest-relevance at start AND end (lost-in-the-middle), replace-don't-append, cap k≤5 (Chroma context-rot).
- **Abstention verdict:** if no match after one widened re-query → return "no relevant history; treat as novel" — never a spurious neighbor. Abstain = NO TRADE is cheap here (arxiv.org/abs/2411.06037).

**Eval harness (~200 lines, weekly):** (1) dead-idea re-test rate (exact collisions must be 0; near-dup proposals <5% and falling); (2) lesson-recall-at-decision vs gold labels from your own history; (3) abstention correctness; (4) memory-on vs memory-off shadow ablation vs the **full-context baseline** (Mem0's own paper: full context beat their memory system).

## 5. BUILD ORDER (ranked, effort in focused sessions)

1. **`memory/writer.py` + `brain.db` schema + append-only triggers + wire into `deep_validation.py`/`forward_test.py`** — the whole design collapses without the deterministic writer. (~1 session)
2. **Canonicalizer + exact novelty hash + gate in `method_lab_runner.py`** — immediate compute savings; starts the DSR trial count from day one (retrofitting the registry is impossible). (~1 session)
3. **Backfill:** parse existing MEMORY.md/session logs + past validation artifacts into `trials`/`trade_autopsy`; every un-migrated dead method is a future laundering hole. (~1 session)
4. **`trade_autopsy` + lessons cron + mechanical promotion + gate_expr evaluation in `mech_sizing.py`.** (~1 session)
5. **Read-only MCP server (5 read tools, 2 quarantine write tools) shared Claude Code + Codex; retire `memory_consolidation_agent.py` from write path.** (~1 session)
6. **Behavioral hash + probe-window correlation gate.** (~1 session)
7. **MEMORY.md/DEAD_IDEAS.md nightly renderer + hash-chain audit job.** (~0.5 session)
8. **Embedding advisory layer (sqlite-vec) + FLAG flow.** (~0.5 session)
9. **Staleness state machine (probation/retire on rolling stats) + DSR computation from `n_trials_family`.** (~1 session)
10. **Eval harness (4 metrics) + negative-balance retrieval quota.** (~1 session)

Items 1–4 are the minimum viable brain. Everything after is hardening.

## 6. HYPE vs REAL — and DO NOT BUILD

**Real (adopt):** append-only event log + projections; canonical-AST + behavioral hashing; bi-temporal invalidate-never-delete (steal Graphiti's *schema*, two timestamp columns); DSR trial ledger; verbatim tombstone feedback; mechanical lesson promotion; negative-loss retrieval quota; abstention gates; read-only MCP enforcement.

**Hype (for this system):**
- LoCoMo/DMR/LongMemEval vendor scores — conversational recall, contested numbers (Zep vs Mem0 replication war), irrelevant to an experiment registry.
- LLM importance scoring, Reflexion prose lessons, A-MEM/Letta sleep-time "self-editing memory" — every one is the laundering vector with a nicer name.
- Embeddings as the novelty check — paraphrase evades cosine; laundered rewrites embed FAR from the original. Advisory net only; hash + correlation are the gates.
- Naive similarity retrieval of past trades — measured to make trading agents *worse* without the loss quota.

**DO NOT BUILD:** Mem0/Zep/Letta/OpenMemory integration (LLM-mediated write paths by design); a graph database (a JOIN is your graph at hundreds–thousands of rows); a standalone vector DB product; MLflow/W&B (SQLite + parquet is 100% at one-box scale); a feature store; blockchain/immutability theater (hash chain column = 95% for 0.1% of complexity); an LLM "memory curator/librarian" agent; Ebbinghaus decay curves; any decay/TTL/eviction on the negative-results table (deleting a dead trial silently un-deflates every future Sharpe); hand-editing MEMORY.md ever again.

**Files touched:** `E:\keo-moi-mail\trading-agent\deep_validation.py`, `forward_test.py`, `method_lab_runner.py`, `mech_sizing.py`; new: `memory/writer.py`, `memory/brain.db`, `memory/events.jsonl`, `memory/mcp_server.py`, `memory/canonicalize.py`, `memory/render_views.py`. Retire from write path: `memory_consolidation_agent.py`, `llm_trader_memory.py` (audit `memory_retrieval.py` for reuse as the read layer).

Strongest sources: MemoryGraft arxiv.org/abs/2512.16962 · ACE arxiv.org/abs/2510.04618 · Deflated Sharpe ssrn.com/abstract=2460551 · Zep/Graphiti bi-temporal arxiv.org/abs/2501.13956 · AlphaEvolve program DB (deepmind.google) · AlphaAgent arxiv.org/abs/2502.16789 · verbatim>extracted arxiv.org/pdf/2601.00821 · negative-knowledge memory arxiv.org/pdf/2606.21024 · sufficient-context abstention arxiv.org/abs/2411.06037 · live negative-quota experiment (dev.to/mnemox).


# ===== 15 RAW RESEARCH WRITEUPS =====

## taxonomy
# Agent Memory for a Self-Improving Trading Agent: Taxonomy → Schemas → Data Flow

## 1. The taxonomy, as practiced in 2024–2026

Real systems have converged on the four-type split (working / episodic / semantic / procedural), and it maps cleanly to storage tiers:

- **Working memory** = the in-context, size-capped, always-visible blocks. Letta/MemGPT's "memory blocks" are the canonical implementation: labeled, character-limited context segments, rewritten by an async background agent ("sleep-time compute") rather than in the hot path (https://www.letta.com/blog/memory-blocks/, https://www.letta.com/blog/sleep-time-compute/). Your `MEMORY.md` is exactly this — keep it small and treat it as a *rendered view*, never the source of truth.
- **Episodic memory** = raw, timestamped events ("what happened"): trades, validation runs, proposals. Reflexion showed reflections over episodes improve later trials (https://arxiv.org/abs/2303.11366), but the honest 2026 correction is that LLM-extracted summaries are lossy — a controlled ablation found verbatim chunks beat extracted artifacts (https://arxiv.org/pdf/2601.00821). Store raw events; derive summaries.
- **Semantic memory** = distilled facts with provenance and time-validity. Zep/Graphiti's bi-temporal knowledge graph is the strongest production pattern: every fact carries valid-time *and* ingestion-time, and superseded facts are **invalidated, not deleted** (https://arxiv.org/abs/2501.13956, https://www.getzep.com/ai-agents/temporal-knowledge-graph/).
- **Procedural memory** = executable, verified skills, not prose. Voyager's skill library — runnable code indexed by description, added only after environment verification (https://arxiv.org/html/2305.16291) — is the right model for your lockbox-passed methods. LangMem formalizes the same three-way split for LangGraph agents (https://www.langchain.com/blog/langmem-sdk-launch).

Finance-specific precedents: FinMem's layered memory with novelty/relevance/importance-scored retrieval (https://arxiv.org/abs/2311.13743), TradingAgents (https://arxiv.org/abs/2412.20138), and QuantAgent, which encodes **failure cases** into its knowledge base so the proposer avoids known pitfalls (https://arxiv.org/html/2502.16789v2 covers the related AlphaAgent regularized-exploration idea).

## 2. Your hard constraint is validated by the literature

Memory-poisoning work (MemoryGraft, https://arxiv.org/html/2512.16962v1; https://arxiv.org/html/2601.05504v2) shows agents treat retrieved memories as ground truth with no provenance checks — your "memory laundering" failure is the benign-self-inflicted version of this attack. Even Mem0, whose original design used an LLM router to ADD/UPDATE/DELETE memories (https://github.com/mem0ai/mem0), moved in 2026 to **ADD-only accumulation** because LLM-driven UPDATE/DELETE loses information. Industry converged on your rule: **LLM proposes, deterministic code disposes.**

## 3. Concrete schemas (SQLite + append-only JSONL, no new infra)

```sql
-- SEMANTIC + PROCEDURAL ground truth. Writes: pipeline code ONLY.
CREATE TABLE methods (
  method_id TEXT PRIMARY KEY,          -- ULID
  dsl_canonical TEXT NOT NULL,         -- normalized DSL/AST (sorted params, renamed vars)
  novelty_hash TEXT UNIQUE NOT NULL,   -- sha256(canonical AST) — structural identity
  embedding BLOB,                      -- for near-dup similarity, advisory only
  status TEXT CHECK(status IN ('proposed','testing','dead','lockbox_validated','live','retired')),
  parent_method_id TEXT,               -- mutation lineage
  created_by TEXT, created_at TEXT);   -- provenance: which proposer session

CREATE TABLE validations (             -- THE NEGATIVE-RESULTS DB (episodic→semantic)
  validation_id TEXT PRIMARY KEY,
  method_id TEXT REFERENCES methods,
  universe TEXT, period_start TEXT, period_end TEXT,
  bootstrap_pvalue REAL, n_bootstrap INTEGER, block_len INTEGER,
  oos_sharpe REAL, lockbox_sharpe REAL, lockbox_used INTEGER,  -- lockbox burns are logged!
  failure_mode TEXT CHECK(failure_mode IN ('no_signal','overfit_is','died_oos',
    'died_lockbox','capacity','costs','regime_dependent',NULL)),
  artifacts_path TEXT, code_git_sha TEXT, data_snapshot_id TEXT,  -- full reproducibility
  created_at TEXT);

CREATE TABLE trade_lessons (           -- distilled losses as NUMBERS
  lesson_id TEXT PRIMARY KEY, trade_id TEXT, method_id TEXT,
  metric TEXT,                         -- e.g. 'slippage_bps','adverse_excursion_R','gap_through_stop_bps'
  expected REAL, realized REAL, n_occurrences INTEGER,
  gate_expr TEXT);                     -- machine-checkable: 'liq_usd_24h >= 50e6'
```

Plus `events.jsonl` — append-only, every proposal/test/fill/exit, hash-chained (prev-hash field) so tampering is detectable; this is your episodic store and replay source (pattern: https://www.sakurasky.com/blog/missing-primitives-for-trustworthy-ai-part-8/).

## 4. Data flow: event → store → retrieval → decision

**Write path (deterministic):** `deep_validation` finishes → pipeline code (not the LLM) computes canonical AST → `novelty_hash` → INSERT into `validations`, UPDATE `methods.status`. Trade closes in `forward_test` → code computes realized-vs-expected deltas → upsert `trade_lessons`. A nightly "sleep-time" job re-renders `MEMORY.md` sections *from SQL* — the file becomes a build artifact.

**Read path (proposer gate):**
```python
def gate_proposal(dsl):
    h = novelty_hash(canonicalize(dsl))
    if db.exists("methods WHERE novelty_hash=?", h):
        return REJECT(prior=db.get_validations(h))     # exact dead idea
    for m in db.knn(embed(dsl), k=5, min_sim=0.92):    # near-dup: advisory
        if m.status == 'dead':
            return FLAG_FOR_DIFF(m)   # proposer must state the structural difference,
                                      # verified by AST diff — not by its own summary
    return PASS
```
**Read path (decision time):** before sizing, `mech_sizing` queries `trade_lessons` for gates matching the candidate's features (`gate_expr` evaluated mechanically), and retrieves the k most similar past episodes by (regime tag, universe, setup features) — FinMem-style scored retrieval, but the *numbers* go into the prompt, not narratives.

**Boundary:** expose two MCP servers to Claude Code/Codex — `memory-read` (SQL SELECT, kNN, event replay) and `memory-propose` (writes only to a `proposals` staging table). Ground-truth tables have no LLM-writable path; the pipeline promotes staged rows after validation. This is enforceable at the MCP tool level, not by prompting.

## 5. Hype vs. real

**Real:** append-only event logs; bi-temporal invalidation; structural hashes for dedupe; Voyager-style verified skill libraries; QuantAgent-style failure encoding; Letta-style capped working-memory blocks. **Hype:** LoCoMo/DMR benchmark wins (conversational recall, ~95% scores — irrelevant to whether retrieval improves *trading* decisions); "self-editing memory" for ground truth (that's precisely memory laundering); embeddings as sole novelty check (paraphrased DSL evades similarity — the AST hash is the hard gate, embeddings only advisory); graph databases for your scale (SQLite + JSONL is enough for thousands of methods; Graphiti earns its complexity only at multi-tenant scale). The single highest-leverage change: make `MEMORY.md` a generated view over the SQL ground truth, and give the LLM proposer a rejection *with the prior validation record attached* — negative results only prevent re-testing if they're retrieved at proposal time, not merely stored.

Sources: https://www.letta.com/blog/memory-blocks/ · https://www.letta.com/blog/sleep-time-compute/ · https://arxiv.org/abs/2501.13956 · https://github.com/mem0ai/mem0 · https://arxiv.org/abs/2311.13743 · https://arxiv.org/abs/2412.20138 · https://arxiv.org/html/2502.16789v2 · https://arxiv.org/abs/2303.11366 · https://arxiv.org/html/2305.16291 · https://www.langchain.com/blog/langmem-sdk-launch · https://arxiv.org/html/2512.16962v1 · https://arxiv.org/pdf/2601.00821 · https://www.sakurasky.com/blog/missing-primitives-for-trustworthy-ai-part-8/

---

## reflexion
# Reflexion loops for a trading agent's second brain: what actually works (2024–2026)

## The core pattern and its production mutations

Reflexion (Shinn et al.) is the canonical loop: attempt → scalar/binary outcome → LLM writes a verbal self-reflection → reflection is appended to episodic memory → injected into the next attempt's context. No gradients; learning is context-mediated (https://arxiv.org/abs/2303.11366, code: https://github.com/noahshinn/reflexion). Two production-relevant mutations emerged since:

1. **ExpeL-style distillation**: instead of raw reflections, distill cross-episode *rules* ("insights") with add/upvote/downvote/edit operations on a rule store (https://arxiv.org/abs/2308.10144).
2. **ACE (Agentic Context Engineering, Stanford/SambaNova 2025)**: splits the loop into Generator → Reflector → **Curator**, where the Curator applies *incremental delta updates* with **non-LLM semantic dedup and deterministic merging**. ACE exists precisely because naive LLM rewriting of memory causes "brevity bias" and **context collapse** — details erode with each rewrite (https://arxiv.org/abs/2510.04618). This is the academic confirmation of your "memory laundering" constraint: the field converged on the same fix — *LLM proposes, deterministic code disposes*.

In finance specifically: FinMem uses layered memory (working/shallow/deep, with recency-decay scores per layer) and retrieval-then-reflection at decision time (https://arxiv.org/abs/2311.13743, https://github.com/pipiku915/FinMem-LLM-StockTrading). TradingAgents adds reflection- and debate-driven agents in a ReAct loop (https://arxiv.org/abs/2412.20138). The survey https://arxiv.org/abs/2408.06361 covers the pattern space. Honest call: FinMem/TradingAgents backtests are look-ahead-contaminated in most reproductions (LLM pretraining knows the price history), so treat them as *architecture* references, not evidence of alpha.

## Exact data flow to implement

**Event → Store**: When `forward_test` closes a shadow trade or `deep_validation` finishes a method run, a *deterministic* Python writer (not the LLM) appends an immutable record to an append-only store (SQLite or JSONL; MEMORY.md becomes a rendered view, not the source of truth):

```python
# outcome record — written ONLY by pipeline code
{
 "kind": "method_test",              # or "trade_close"
 "method_dsl_hash": sha256(canonicalize(dsl)),   # novelty hash
 "dsl": "...", "universe": ["BTC","ETH",...], "period": ["2023-01","2026-05"],
 "n_trials_to_date": 137,            # for Deflated Sharpe (see below)
 "block_bootstrap_p": 0.31, "oos_sharpe": -0.2, "lockbox_sharpe": null,
 "verdict": "DEAD", "failure_mode": "decays_after_2024|fee_dominated|regime_dependent",
 "git_commit": "abc123", "data_snapshot_id": "...", "ts": "2026-07-06T..."
}
```

**Reflection step (LLM allowed, quarantined)**: after each loss ≥ threshold, the LLM writes a reflection — but the *stored* artifact is forced through a numeric schema: `{"lesson_id", "trigger_condition": {"feature": "ch24", "op": ">", "value": 12}, "observed_cost_usd": 2.20, "n_occurrences": 3, "linked_trade_ids": [...], "status": "candidate"}`. Free-text rationale goes in a separate non-authoritative field. A lesson only flips `candidate → active` when a deterministic counter confirms it fired ≥N times with the same sign — that's ExpeL's upvote mechanism made mechanical. This kills the known failure where Reflexion's diagnoses are causally wrong under sparse feedback ("memory confabulation," https://arxiv.org/pdf/2605.29463).

**Retrieval → Decision**: two disjoint paths.
- *Proposer path (method_lab)*: before the LLM sees anything, a **deterministic pre-filter** checks the candidate's canonical DSL hash and an embedding-similarity search (cosine > ~0.92) against the negative-results DB; on hit, the proposal is rejected *outside* the LLM with the prior record injected as "already dead because X." This mirrors AI Scientist v2's Semantic-Scholar novelty gate, except against your own graveyard (https://github.com/SakanaAI/AI-Scientist-v2, https://pub.sakana.ai/ai-scientist-v2/paper/paper.pdf). Critically, dedup on the *DSL/definition*, not on the LLM's description — descriptions are exactly what launders.
- *Trader path*: at decision time, retrieve `active` lessons whose `trigger_condition` matches current features (a plain rule-engine scan, no embeddings needed for <500 lessons), plus top-k episodic trades by (asset, regime-tag, setup-type) similarity. Rules become hard gates; episodes become context.

**Multiple-testing accounting**: the `n_trials_to_date` counter feeds a Deflated Sharpe Ratio at lockbox time — the negative-results DB is not just a dedup index, it is the *trial registry* that makes your lockbox p-values honest (Bailey & López de Prado, https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551). This is the single biggest payoff nobody in the LLM-agent literature mentions: without a persistent count of dead ideas, every "significant" lockbox result is uninterpretable.

## Read/write boundaries (the hard constraint, formalized)

- **LLM may WRITE**: proposals, free-text rationale fields, candidate lessons (schema-validated by `pydantic`, rejected on violation).
- **LLM may NEVER WRITE**: outcome records, verdicts, p-values, trial counts, novelty hashes, lesson status transitions. These are produced only by pipeline code with git-commit + data-snapshot provenance.
- **LLM may READ everything**, but retrieval is code-side: the LLM never issues "forget/update memory" tool calls against ground truth. The memory-poisoning literature shows agents treat retrieved memories as ground truth with no provenance checks (MemoryGraft, https://arxiv.org/abs/2512.16962; https://arxiv.org/pdf/2606.04329) — your deterministic-writes rule is the accepted defense, plus provenance fields on every record.

## Tooling: honest calls

Mem0/Zep-Graphiti/Letta (https://github.com/getzep/graphiti, https://github.com/letta-ai/letta, https://github.com/mem0ai/mem0) are built for *conversational* memory with LLM-driven extraction on the write path — exactly what you must avoid for ground truth. Zep's temporal validity windows are the one genuinely relevant idea (facts with `valid_from/invalid_at` — useful for regime-scoped lessons), but for your scale a single SQLite DB + FTS5 + a small embedding index beats all of them: deterministic, diffable, no extraction layer to launder through. MCP fits naturally: expose `search_graveyard(dsl_or_text)`, `get_active_lessons(features)`, `get_lockbox_methods()` as read-only MCP tools shared by Claude Code and Codex; expose **no write tools**.

**Hype vs. real**: Reflexion's 91% HumanEval-style wins do not transfer to trading, where feedback is noisy, delayed, and single-sample per decision — one losing trade proves almost nothing, which is why lessons must be aggregated counters, not per-trade narratives. Negative-results storage is underrated and cheap ("a well-specified null result is a map of where not to spend the next unit of effort" — https://arxiv.org/pdf/2606.04220, https://arxiv.org/html/2406.03980v1). The realistic win: not a smarter agent, but a proposer that stops burning validation budget on resurrected corpses and a lockbox whose statistics finally know how many bodies are buried.

---

## generative-agents
# Generative Agents Memory for a Quant Trading Agent's Second Brain

## 1. The original mechanics, precisely

Park et al.'s Generative Agents ([https://arxiv.org/abs/2304.03442](https://arxiv.org/abs/2304.03442), [ACM full text](https://dl.acm.org/doi/fullHtml/10.1145/3586183.3606763)) store every experience as a **memory object**: `{description, created_at, last_accessed, importance, embedding}` in an append-only stream. At decision time, every memory is scored:

```
score = α_rec·recency + α_imp·importance + α_rel·relevance   (all α = 1)
recency    = 0.995 ^ hours_since_last_accessed      # exponential decay
importance = LLM "poignancy" 1..10, assigned ONCE at write time
             (prompt: "1 = mundane (brushing teeth), 10 = extremely
              poignant (a break up)... Rate: {event}")
relevance  = cosine(embed(query), embedding)
```

Each component is min-max normalized to [0,1]; top-K memories that fit the context window go into the prompt. **Reflection**: when the running sum of importance of recent events exceeds a threshold (150), the agent takes its 100 most recent memories, asks the LLM "what are the 3 most salient high-level questions?", retrieves against each question, then asks for "5 high-level insights (example format: insight (because of 1, 5, 3))". Insights are stored back as first-class memories **with pointers to their evidence**, so reflections can cite reflections — a provenance tree with raw observations at the leaves.

**Data flow**: event → append memory object (importance scored at write) → decision triggers query → weighted top-K retrieval → prompt → action → new observations appended → periodic reflection compresses leaves into cited insights.

**Honest call**: the retrieval formula is real and battle-tested (it's copied everywhere); the LLM importance score is the weak link — scores cluster on 3-4 integers, are prompt-sensitive, and are exactly where subjectivity leaks in. Recency also double-counts (touching a memory refreshes `last_accessed`, creating rich-get-richer loops). Production systems diverged: FinMem ([https://arxiv.org/abs/2311.13743](https://arxiv.org/abs/2311.13743), code [https://github.com/pipiku915/finmem-llm-stocktrading](https://github.com/pipiku915/finmem-llm-stocktrading)) replaced the single stream with **layered decay** — daily news in a shallow fast-decay layer, quarterly filings deep with slow decay, plus an access counter that promotes memories validated by profitable trades; FinCon ([NeurIPS 2024](https://proceedings.neurips.cc/paper_files/paper/2024/file/f7ae4fe91d96f50abc2211f09b6a7e49-Paper-Conference.pdf)) added risk-triggered "verbal reinforcement" updates. Letta/MemGPT, Zep/Graphiti and Mem0 all use **LLM-mediated extraction on the write path** ([comparison](https://forum.letta.com/t/agent-memory-letta-vs-mem0-vs-zep-vs-cognee/88)) — which is precisely what your hard constraint forbids, so don't adopt them wholesale. The one idea worth stealing from Zep/Graphiti is **bi-temporal facts** (`valid_at`/`invalid_at`): a lesson can be true only within a regime.

Your "memory laundering" fear is now formal literature: memory poisoning attacks like MINJA achieve >70% attack success by implanting plausible experiences the agent then trusts ([https://arxiv.org/abs/2601.05504](https://arxiv.org/abs/2601.05504), survey [https://arxiv.org/pdf/2604.16548](https://arxiv.org/pdf/2604.16548)). Self-inflicted laundering (LLM summarizes a dead method into looking novel) is the same failure with no adversary. Deterministic writes for ground truth is the correct, literature-backed defense.

## 2. Mapping onto your second brain

**Split the store by write authority**, not by topic:

- **Tier A — ground truth (deterministic writes only)**: SQLite (`experiments.db`), append-only, written exclusively by `deep_validation` and the shadow ledger. The LLM has read-only access via an MCP tool. Schema for the negative-results table:

```sql
CREATE TABLE experiments (
  novelty_hash TEXT PRIMARY KEY,   -- sha256(canonicalized DSL AST
                                   --   + sorted universe + period bucket)
  dsl TEXT, universe TEXT, period TEXT,
  bb_pvalue REAL, oos_sharpe REAL, lockbox_sharpe REAL,
  verdict TEXT CHECK(verdict IN ('DEAD','LOCKBOX_PASS','FORWARD')),
  failure_mode TEXT,               -- enum: overfit|regime|costs|capacity
  dsl_embedding BLOB, tested_at TEXT, code_commit TEXT
);
```

- **Tier B — trade lessons (deterministic aggregation)**: replace the Generative-Agents LLM reflection with a **nightly batch job** — the reflection tree's *structure* (insights citing evidence rows) without LLM writes. Group closed trades by `(setup_tag, regime, failure_mode)`, emit numeric rows: `{n, win_rate, avg_MAE, avg_MFE, p50_hold, pnl_bps}` each carrying the trade IDs it cites. That IS a reflection tree — leaves are ledger rows, parents are computed aggregates — but the "insight" is a number, not prose. The LLM may *read* these and write free-text hypotheses only into a quarantined Tier C notes file that can never be cited as evidence.

**Retrieval scoring, adapted** (the LLM importance score gets replaced by deterministic importance):

```python
def importance(row):                      # NO LLM — computed at write
    if row.verdict == 'DEAD':  return 1.0 # dead ideas never fade
    return clip(abs(row.pnl_z)/3 + row.lockbox_sharpe/2, 0, 1)

def retrieve(query_emb, regime, k=8):
    for r in rows:
        rec = 1.0 if r.verdict=='DEAD' else 0.995**hours_since(r)
        rel = cos(query_emb, r.dsl_embedding) * regime_match(r, regime)
        r.s = rec + importance(r) + rel
    return topk(rows, k)
```

Key deviation from Park et al.: **negative results get recency = 1 forever**. Decay is for regime-conditioned lessons; graveyards don't decay.

**Proposer gate (the AlphaEvolve pattern)**: DeepMind's AlphaEvolve keeps a program database and always shows the LLM what was already tried, using MAP-Elites niches for diversity ([https://deepmind.google/blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms/](https://deepmind.google/blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms/)). Do the same: before `method_lab` accepts a proposal, run `novelty_check(dsl)` → exact `novelty_hash` match rejects instantly; embedding similarity > 0.92 against DEAD rows returns the corpse's row verbatim ("you proposed this; block-bootstrap p=0.41, failure_mode=overfit"). Feed the 5 nearest DEAD rows *into* the proposer prompt — negative context measurably steers generation away from the graveyard.

**Free payoff**: the experiments table is literally the trial count N required by the Deflated Sharpe Ratio and PBO ([Bailey & López de Prado](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf)). Most shops can't compute DSR because they never logged their trials; your negative-results DB makes multiple-testing correction a one-line query.

**Hype vs real, summarized**: the retrieval triple (recency×importance×relevance) — real, keep it. LLM importance scoring — replace with computed statistics. LLM reflection prose — replace with deterministic aggregation that preserves the citation-tree shape. Off-the-shelf memory platforms (Mem0/Zep/Letta) — wrong fit here because their write path is LLM-mediated; SQLite + JSONL + your existing MCP tooling is the honest answer at your scale. MEMORY.md stays as the human-readable index, regenerated *from* the DB, never hand-edited by the model.

Sources: [Generative Agents paper](https://arxiv.org/abs/2304.03442) · [ACM version](https://dl.acm.org/doi/fullHtml/10.1145/3586183.3606763) · [FinMem](https://arxiv.org/abs/2311.13743) · [FinMem code](https://github.com/pipiku915/finmem-llm-stocktrading) · [FinCon NeurIPS 2024](https://proceedings.neurips.cc/paper_files/paper/2024/file/f7ae4fe91d96f50abc2211f09b6a7e49-Paper-Conference.pdf) · [MINJA memory poisoning](https://arxiv.org/abs/2601.05504) · [Memory security survey](https://arxiv.org/pdf/2604.16548) · [Letta vs Mem0 vs Zep](https://forum.letta.com/t/agent-memory-letta-vs-mem0-vs-zep-vs-cognee/88) · [AlphaEvolve](https://deepmind.google/blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms/) · [Deflated Sharpe Ratio](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf)

---

## negative-results-db
# Storing Failed Experiments and Dead Strategies: Field Report for the Trading Agent's Second Brain

## 1. Key patterns from real practice (2024–2026)

**Pattern A — the trial ledger is the statistic.** In quant research the negative-results DB isn't a nice-to-have archive; it's an input to the math. Bailey & López de Prado's Deflated Sharpe Ratio requires recording *every* backtest ever run (returns series, not just summaries), because the significance of the surviving strategy depends on N effective trials (https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551, https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf). Practically: if your graveyard is incomplete, your p-values are lies. ~95% of backtested strategies fail live (https://hedgefundalpha.com/education/backtesting-mistakes-kill-quant-strategies-guide/), and there is **no public strategy graveyard** — every shop rolls its own private one. Yours will be bespoke; that's normal, not a gap.

**Pattern B — dedup by content hash + behavioral hash, two layers.** DVC skips recomputation via md5 of code+deps+params (https://dvc.org/doc/user-guide/pipelines/running-pipelines) — that's *syntactic* dedup. DeepMind's AlphaEvolve program database adds *behavioral* hashes (hash of output vectors on a probe set) to kill trivially-rewritten variants (https://gonzoml.substack.com/p/alphaevolve, https://tianpan.co/blog/2026-02-08-alphaevolve-evolutionary-coding-agent-algorithm-discovery). AlphaAgent (https://arxiv.org/abs/2502.16789) enforces originality via AST similarity against existing alphas plus correlation-of-signal filtering — LLMs left unregularized generate homogeneous, crowded factors. The consensus stack is three novelty gates: (1) canonical-DSL hash (exact re-test), (2) AST/edit-distance similarity (cosmetic variant), (3) signal correlation on a fixed probe window (behavioral duplicate).

**Pattern C — experiment trackers are queryable, but only for structured fields.** MLflow's `search_runs` supports `attributes.status = "FAILED" and tags.family = "meanrev"` (https://mlflow.org/docs/latest/ml/search/search-runs/). W&B/MLflow both track params/metrics/artifacts fine (https://uplatz.com/blog/the-2025-mlops-landscape-a-comparative-analysis-of-mlflow-weights-biases-and-neptune/); neither gives you novelty hashing or "has this died before" — you build that as a thin layer on top, or in plain SQLite.

**Pattern D — LLM memory writes are an attack surface, even against yourself.** MemoryGraft (https://arxiv.org/html/2512.16962v1) and the memory-lifecycle security survey (https://arxiv.org/html/2604.16548v1) show agents treat retrieved experience as procedural ground truth, with no provenance checks; poisoned or laundered memories persist and compound. Your "memory laundering" constraint is exactly what this literature predicts: an LLM summarizing its own failures will soften them into novelty. The defense they converge on is the same one you learned the hard way — **provenance-stamped, deterministic writes; LLM gets read-only access.**

## 2. Data flow for the second brain

```
EVENT: deep_validation completes (any verdict)
  └─> DETERMINISTIC WRITER (Python, in deep_validation itself — never the LLM):
      1. canonicalize DSL (sort params, normalize names, strip comments)
      2. h_syn  = sha256(canonical_dsl + universe_id + timeframe)
      3. h_beh  = sha256(sign(daily_signal) on fixed 2023-2024 probe window, quantized)
      4. INSERT INTO trials (append-only; UPDATE forbidden by trigger)
  └─> if verdict == PASS: also INSERT INTO lockbox_methods (provenance row)

EVENT: real trade closes (forward_test shadow ledger)
  └─> DETERMINISTIC WRITER: INSERT INTO trade_lessons — numbers only
      (method_id, slippage_bps, realized_vs_expected_R, MAE, MFE, regime_tag,
       funding_paid, exit_reason_enum). No free-text field exists in the schema.

DECISION TIME: LLM proposer emits candidate DSL
  └─> GATE (deterministic Python, pre-LLM-feedback):
      exact:      SELECT * FROM trials WHERE h_syn = ?
      behavioral: SELECT * FROM trials WHERE h_beh = ?
      near-dup:   corr(candidate_signal, dead_signal) > 0.9 on probe window
                  for the top-k trials by family tag
      → REJECT with the dead trial's numbers injected into the proposer's
        next prompt ("tried 2026-03-12, bootstrap p=0.41, lockbox Sharpe -0.3,
        failure_mode=COST_EATEN"). Retrieval = LLM reads; verdict = code decides.
```

## 3. Concrete schema (SQLite — you don't need more)

```sql
CREATE TABLE trials (
  id INTEGER PRIMARY KEY,
  h_syn TEXT NOT NULL, h_beh TEXT,            -- novelty hashes, indexed
  dsl_canonical TEXT NOT NULL,                 -- full definition, verbatim
  universe TEXT, period_start TEXT, period_end TEXT,
  n_trades INTEGER, bootstrap_p REAL,          -- block-bootstrap p-value
  oos_sharpe REAL, lockbox_sharpe REAL,
  verdict TEXT CHECK(verdict IN ('PASS','FAIL_P','FAIL_OOS','FAIL_LOCKBOX','FAIL_COST')),
  failure_mode TEXT CHECK(failure_mode IN     -- enum, NOT free text
    ('COST_EATEN','REGIME_ONLY','LOW_N','SIGN_FLIP_OOS','DUP_OF','NONE')),
  dup_of INTEGER REFERENCES trials(id),
  returns_path TEXT,                           -- parquet of daily returns → DSR needs this
  git_sha TEXT, created_at TEXT
);
-- Enforce append-only:
CREATE TRIGGER trials_no_update BEFORE UPDATE ON trials
  BEGIN SELECT RAISE(ABORT,'trials is append-only'); END;
```

`trade_lessons` mirrors this: all-numeric columns plus enums. Aggregate lessons are *views*, not LLM summaries: `SELECT regime_tag, avg(realized_vs_expected_R), avg(slippage_bps) FROM trade_lessons GROUP BY regime_tag`. The LLM reads the view output; it never authors the row.

## 4. Hype vs. real

- **Real:** DSR trial-counting; content-hash caching (DVC-style); behavioral hashing (AlphaEvolve); AST+correlation dedup (AlphaAgent); MLflow-style structured queries. All boring, all proven.
- **Hype:** vector-DB "semantic memory of failures." Embedding similarity retrieval is precisely the mechanism MemoryGraft exploits — no provenance, fuzzy matches, and a laundered rewrite of a dead idea embeds *far* from the original. For "has this been tried," exact/behavioral hashing beats embeddings. Also hype: needing W&B/MLflow at your scale — a self-hosted tracker is 80% of value at near-zero cost (https://contracollective.com/blog/weights-biases-vs-mlflow-mlops-experiment-tracking-2026), and at one-agent scale SQLite + parquet is 100% of it.
- **Honest gap:** behavioral hashing on noisy crypto signals has false negatives (same idea, different quantization). The correlation gate (>0.9 on a fixed probe window) is the workhorse; hashes are just the fast path.

## 5. Application summary

Store: (a) `lockbox_methods` with full provenance (git SHA, data hash, DSR-adjusted stats given `SELECT count(*) FROM trials` as N); (b) `trials` append-only with dual hashes, enum failure modes, and *the daily returns parquet* (future DSR recomputation); (c) `trade_lessons` numeric-only. Keep MEMORY.md as LLM-writable *narrative* scratch space, but make the SQLite DB the sole source consulted by the gate — narrative can inspire, only the ledger can veto or approve. Expose read-only MCP tools (`query_graveyard(dsl)`, `lessons_by_regime(tag)`) to Claude Code and Codex; the sole writer is `deep_validation`/`forward_test` Python code. Write boundary in one sentence: **the LLM proposes and reads; only code that ran the experiment writes.**

Sources: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551 · https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf · https://hedgefundalpha.com/education/backtesting-mistakes-kill-quant-strategies-guide/ · https://dvc.org/doc/user-guide/pipelines/running-pipelines · https://gonzoml.substack.com/p/alphaevolve · https://tianpan.co/blog/2026-02-08-alphaevolve-evolutionary-coding-agent-algorithm-discovery · https://arxiv.org/abs/2502.16789 · https://arxiv.org/html/2505.11122v2 · https://mlflow.org/docs/latest/ml/search/search-runs/ · https://uplatz.com/blog/the-2025-mlops-landscape-a-comparative-analysis-of-mlflow-weights-biases-and-neptune/ · https://arxiv.org/html/2512.16962v1 · https://arxiv.org/html/2604.16548v1 · https://contracollective.com/blog/weights-biases-vs-mlflow-mlops-experiment-tracking-2026

---

## vector-rag-experience
# Vector RAG Over an Agent's Own Experience: What Actually Works (2024–2026)

## 1. The core pattern split

Real production consensus by 2026: **semantic retrieval wins only where the query is fuzzy language and the corpus is fuzzy language.** For everything with a schema — numbers, hashes, timestamps, pass/fail outcomes — an exact structured query is strictly better: deterministic, auditable, zero recall risk. The field converged on "SQL/graph for facts, vectors for meaning": vector stores are effective for episodic lookup, but structured/graph memory beats them on data with inherent structure — benchmarks show ~92%/88% recall/precision for structured-graph retrieval vs ~85%/75% for pure vector RAG on relational data ([machinelearningmastery.com](https://machinelearningmastery.com/vector-databases-vs-graph-rag-for-agent-memory-when-to-use-which/), [sparkco.ai](https://sparkco.ai/blog/ai-agent-memory-in-2026-comparing-rag-vector-stores-and-graph-based-approaches)). The pro-embedding counterargument (embed everything, ratings included, to get "gradients not thresholds" — [decodingai.com](https://www.decodingai.com/p/stop-using-text-to-sql-for-search)) is a *product-search* argument. For ground-truth experiment records it's exactly wrong: you never want a p-value=0.62 record to "semantically drift" into looking retrievable as a success. **Do not vectorize numeric records. Vectorize only their natural-language description, and store numbers as filterable payload columns.**

Second consensus: hybrid search is a pipeline, not a toggle. BM25 + dense vectors as parallel first-stage retrievers, fused with Reciprocal Rank Fusion (rank-based, so incompatible score scales don't matter), optional cross-encoder rerank on the shortlist ([tianpan.co](https://tianpan.co/blog/2026-04-12-hybrid-search-production-bm25-dense-embeddings), [weaviate.io](https://weaviate.io/blog/hybrid-search-explained), [qdrant.tech](https://qdrant.tech/documentation/advanced-tutorials/reranking-hybrid-search/)). Anthropic's contextual retrieval — prepend 50–100 tokens of document context to each chunk before embedding AND before BM25 indexing — cut top-20 retrieval failures 49%, 67% with rerank ([anthropic.com](https://www.anthropic.com/engineering/contextual-retrieval)). For your corpus sizes (hundreds of methods, not millions of docs), chunking is trivial: **one record = one chunk**; never split a method definition.

Third: your "no LLM writes to ground truth" constraint is now literature-validated. MemoryGraft shows agents replicate poisoned "successful experiences" from their own memory ([arxiv.org/abs/2512.16962](https://arxiv.org/abs/2512.16962)); OEP poisons self-evolving agents with locally-correct-but-non-transferable experiences ([arxiv.org/pdf/2605.18930](https://arxiv.org/pdf/2605.18930)); MemEvoBench benchmarks "memory misevolution" ([arxiv.org/pdf/2604.15774](https://arxiv.org/pdf/2604.15774)). Your "memory laundering" failure mode is the self-inflicted version of these attacks. Deterministic writes from the validation pipeline are the defense.

## 2. Data flow for the trading agent

```
EVENT (deep_validation completes / trade closes)
  → deterministic writer (Python, in-pipeline, NO LLM in the write path)
     method_id = sha256(canonical_DSL + universe + period)   # novelty hash
     INSERT INTO methods(...) — append-only, provenance = git SHA + config hash
     embed(description_text) → vec column                    # embed ONLY prose
  → RETRIEVAL, two disjoint paths:
     [A] EXACT GATE (pre-proposal, mandatory, SQL only):
         reject if novelty_hash exists;
         else k-NN on DSL-AST embedding, cos > 0.85 → surface top-3 dead
         neighbors + their failure_mode INTO the proposer prompt as
         "already dead, differ or abandon"
     [B] SEMANTIC RECALL (decision-time, advisory):
         hybrid FTS5+vec RRF over lesson/failure-mode prose,
         SQL-prefiltered by regime/coin-liquidity payload
  → DECISION: proposer/sizer reads; may write only to a *candidate notes*
     table, never to methods/trades/lessons.
```

Schema (SQLite — you're file-based already; sqlite-vec + FTS5 gives hybrid RRF in one file, no server: [alexgarcia.xyz](https://alexgarcia.xyz/blog/2024/sqlite-vec-hybrid-search/index.html), [github.com/sqliteai/sqlite-rag](https://github.com/sqliteai/sqlite-rag)):

```sql
CREATE TABLE methods(          -- negative results ARE the main table
  method_id TEXT PRIMARY KEY,  -- novelty hash
  dsl TEXT, universe TEXT, period TEXT,
  bb_pvalue REAL, oos_sharpe REAL, lockbox_verdict TEXT,  -- numbers: SQL only
  failure_mode TEXT,           -- prose: FTS5 + embedded
  status TEXT CHECK(status IN ('dead','lockbox_pass','shadow','live')),
  provenance_git TEXT, created_at TEXT);
CREATE TABLE loss_lessons(     -- NUMBERS not narrative
  trade_id TEXT, rule_expr TEXT,          -- e.g. 'ch24 > 0.12 AND side=LONG'
  n_trades INT, hit_rate REAL, avg_pnl_R REAL, p_value REAL);
```

`loss_lessons` should never be embedded at all — lessons like your falling-knife gate ("liquid ≥ $50M") are *executable predicates* evaluated in the pretrade checklist, not retrieved by vibes. This is the FinMem lesson inverted: FinMem's layered decaying memory ([arxiv.org/abs/2311.13743](https://arxiv.org/abs/2311.13743)) works for news/sentiment streams, but its "reflections" are LLM-written narrative — precisely what mutated your ground truth. Keep FinMem-style layering only for market commentary, if at all.

## 3. Where vectors genuinely earn their keep here

Exactly two places. (1) **Near-duplicate idea detection past the exact hash**: the novelty hash catches byte-identical resubmission; embeddings catch "RSI(14)<30 → long" vs "buy oversold momentum reversal 14-period". Use lexical MinHash first, embedding cosine second (thresholds ~0.85 strict / 0.75 balanced are the empirical norms — [arxiv.org/pdf/2605.09611](https://arxiv.org/pdf/2605.09611), [milvus.io](https://milvus.io/blog/minhash-lsh-in-milvus-the-secret-weapon-for-fighting-duplicates-in-llm-training-data.md)). This is AlphaEvolve's program-database pattern: an archive of evaluated programs fed back into proposer prompts so the LLM builds on, rather than re-derives, past attempts ([deepmind.google](https://deepmind.google/blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms/)). (2) **Voyager-style retrieval of validated methods**: index lockbox-passed methods by embedded NL description, retrieve top-k when the proposer faces a new regime — retrieval of *verified* artifacts, never re-generation ([voyager.minedojo.org](https://voyager.minedojo.org/)).

## 4. Hype vs real

**Real:** hybrid RRF (cheap, config-not-code); contextual chunk headers; append-only deterministic writes; embeddings-for-dedup. **Overhyped for you:** managed memory platforms (Mem0/Zep/Letta — built for chat personalization; Zep's temporal graph is genuinely good engineering but solves fact-supersession you don't have: [medium comparison](https://medium.com/@wasowski.jarek/i-compared-5-ai-agent-memory-systems-across-6-dimensions-none-wins-6a658335ed0a)); LLM-extracted "memories" (the laundering vector); embedding numeric outcomes; graph RAG at your scale (hundreds of records — a JOIN is your graph); agentic memory self-editing (MemEvoBench exists because it fails). **Verdict:** one SQLite file with sqlite-vec + FTS5, deterministic writers inside deep_validation/forward_test, exact-gate-before-semantic-recall, LLM read-only on ground truth with a quarantined candidate-notes table. MEMORY.md stays as the human-readable projection, regenerated from SQL — never the source of truth.

---

## temporal-kg
# Temporal KG Memory (Zep/Graphiti) for a Quant Agent's Second Brain

## What Zep/Graphiti actually is

Zep's engine, Graphiti ([paper](https://arxiv.org/abs/2501.13956), [repo, 20k+ stars](https://github.com/getzep/graphiti)), is a three-layer graph: an **episode subgraph** (raw events, immutable), an **entity subgraph** (deduplicated entities + relation edges), and a **community subgraph** (label-propagation clusters). Its differentiator is **bi-temporal modeling**: every edge carries four timestamps — `valid_at`/`invalid_at` (when the fact was true in the world) and `created_at`/`expired_at` (when the system learned/superseded it) ([Zep docs](https://www.getzep.com/ai-agents/temporal-knowledge-graph/)). Contradiction handling is **invalidation, not deletion**: a new fact sets `invalid_at` on the old edge, preserving the audit trail and enabling point-in-time queries ("what did we believe on 2026-03-01?"). Ingestion flow per episode: LLM entity extraction → embedding+BM25 dedup resolution → temporal extraction → contradiction detection via `resolve_edge_contradictions` comparing validity windows ([Neo4j writeup](https://neo4j.com/blog/developer/graphiti-knowledge-graph-memory/)). Retrieval is hybrid (cosine + BM25 + graph BFS) with reciprocal-rank fusion and **no LLM at query time** — P95 ~300ms ([independent assessment](https://codex.danielvaughan.com/2026/03/30/graphiti-agent-memory-store/)).

## Honest hype-vs-real

- **Benchmark numbers are unreliable.** Zep claimed 94.8% DMR / 84% LoCoMo; Mem0's replication got [58.44%](https://github.com/getzep/zep-papers/issues/5), Zep [counter-attacked Mem0's numbers](https://blog.getzep.com/lies-damn-lies-statistics-is-mem0-really-sota-in-agent-memory/), and an [audit found 6.4% of LoCoMo's answer key is wrong](https://dev.to/penfieldlabs/we-audited-locomo-64-of-the-answer-key-is-wrong-and-the-judge-accepts-up-to-63-of-intentionally-33lg). Ignore the leaderboard war; the architecture ideas are what's real.
- **LLM-in-the-write-path is the weak point.** Every `add_episode` fires multiple LLM calls (extraction, dedup, summarization) — cost, latency, and nondeterminism ([MCP server docs](https://github.com/getzep/graphiti/blob/main/mcp_server/README.md)). Community bug reports show real temporal-correctness gaps under backfill ([issue #1489](https://github.com/getzep/graphiti/issues/1489)).
- **Your "memory laundering" fear is validated in the literature.** [MemoryGraft](https://arxiv.org/abs/2512.16962) shows agents imitate retrieved memories with no provenance checks; [MemGuard](https://arxiv.org/pdf/2605.28009) and OWASP's new [ASI06 Memory & Context Poisoning](https://christian-schneider.net/blog/persistent-memory-poisoning-in-ai-agents/) formalize contamination; summarization can launder toxic/false state past detectors.
- **Naive memory makes trading agents WORSE.** A documented [live experiment](https://dev.to/mnemox/i-gave-my-trading-agent-memory-and-it-made-everything-worse-28a3): adding similarity retrieval dropped profit factor 2.42→0.94 because winners cluster in "typical" market states and dominate top-K similarity — geometric bias, not data bias. Fix: `ensure_negative_balance` forcing ≥20% of retrieved memories to be losses (+$29 vs −$154 over 200 decisions). Also confirmed by [arXiv:2605.28359](https://arxiv.org/html/2605.28359v1) (knowing ≠ doing in LLM trading memory).

## The design: steal the schema, not the pipeline

**Do NOT use Graphiti's LLM extraction path for ground truth.** Use Graphiti's `add_triplet` (deterministic direct write, [recently hardened against edge overwrites](https://github.com/getzep/graphiti/releases)) or skip Graphiti entirely and implement bi-temporal edges in SQLite/Neo4j yourself. Your validation pipeline already produces structured records — LLM extraction adds risk with zero benefit.

**Entity types** (Pydantic, per [Graphiti custom types](https://help.getzep.com/graphiti/core-concepts/custom-entity-and-edge-types)): `Method(dsl_hash, dsl_text, family, params)`, `Experiment(universe, period, n_trials, bootstrap_p, oos_sharpe, lockbox_sharpe, verdict, failure_mode, code_commit)`, `Trade(entry, exit, pnl, mae, mfe, regime_snapshot)`, `Regime(vol_bucket, trend_bucket, funding_bucket)`, `Lesson(rule_id, trigger_condition, numeric_threshold, sample_n, effect_size)`.

**Edges**: `TESTED_IN(Method→Experiment)`, `KILLED_BY(Method→Experiment, valid_at=test_date, invalid_at=NULL)`, `VALIDATED_BY(Method→Experiment)`, `VARIANT_OF(Method→Method, similarity_score)`, `TRADED_UNDER(Trade→Regime)`, `SUPPORTS/REFUTES(Trade→Lesson)`. Bi-temporality matters concretely: a LOCKBOX-validated method whose forward-test shadow ledger later decays gets `invalid_at` stamped on `VALIDATED_BY` — the method's history stays queryable but it's excluded from "currently believed" retrieval. That's the invalidation pattern doing real work.

**Data flow (event → store → retrieval → decision):**
1. `deep_validation` finishes → Python writes an `Experiment` node + `KILLED_BY`/`VALIDATED_BY` edge **deterministically** (no LLM). Novelty hash = SHA256 of canonicalized DSL AST; also store a DSL embedding for near-duplicate detection.
2. Trade closes → `forward_test` writes `Trade` + `TRADED_UNDER` edges with numbers only. Lessons are **aggregations computed by code** (e.g., "LONG when ch24>+12%: n=14, mean_pnl=−0.8%, p=0.03"), never LLM prose.
3. **Proposer gate (pre-LLM, deterministic):** before any candidate reaches the LLM, `novelty_check(dsl)` = exact-hash lookup + embedding kNN against dead methods; cosine >0.92 → auto-reject with the killing experiment attached. The LLM never gets the chance to relabel a corpse.
4. **Decision-time retrieval:** hybrid search over `Lesson` + `Trade` filtered to `invalid_at IS NULL`, with the mandatory negative-balance quota from the dev.to fix. Retrieved facts go into the prompt **with provenance strings** (experiment ID, p-value, n) so the LLM sees evidence, not vibes.

```python
def record_experiment(method, result):   # ONLY code writes ground truth
    mid = upsert_method(sha256(canon(method.dsl)))
    eid = insert_experiment(result)      # append-only
    edge = "VALIDATED_BY" if result.lockbox_pass else "KILLED_BY"
    add_edge(mid, eid, edge, valid_at=now())
    if edge == "KILLED_BY":
        for e in active_edges(mid, "VALIDATED_BY"):
            e.invalid_at = now()         # supersede, never delete
```

**Read/write boundary:** LLM has **read-only** MCP tools (`search_facts`, `search_nodes`); write tools (`add_episode`, `delete_*`) are simply not mounted. Ground-truth writes go through the Python pipeline only. The one LLM-writable surface: a quarantined `Hypothesis` scratch subgraph that never feeds the novelty gate. The [Graphiti MCP server](https://help.getzep.com/graphiti/getting-started/mcp-server) (FalkorDB or Neo4j backend; Kuzu deprecated) gives Claude Code + Codex a shared read view — mount it with search tools only.

**Migration from MEMORY.md:** keep it as human-readable digest, generated FROM the graph by a nightly deterministic job — never the reverse direction.

Bottom line: Graphiti's bi-temporal edge schema, invalidation-over-deletion, and hybrid no-LLM retrieval are genuinely good and battle-tested; its LLM extraction pipeline is exactly the memory-laundering vector you already got burned by. Adopt the former, amputate the latter, and enforce the negative-retrieval quota — that single number (≥20% losses in every retrieval) is the most empirically supported intervention in this entire space.

---

## consolidation
All research done (6 distinct searches). Report follows.

---

# Memory Consolidation Without Laundering: Field Report for the Trading Agent's Second Brain

## The key patterns from 2024–2026 practice

**1. Event sourcing beat "LLM manages its own memory" for ground truth.** The strongest recent convergence is separating cognition from state mutation: agents emit structured *intentions*, and a deterministic orchestrator validates, appends to an append-only log, and projects materialized read-views. That's the explicit architecture of ESAA (https://arxiv.org/abs/2602.23193), its conversational follow-up (https://arxiv.org/abs/2606.23752), and the OpenHands SDK, which treats all interactions as immutable events in an append-only EventLog (https://arxiv.org/pdf/2511.03690). This is precisely your anti-laundering constraint, now validated as a published pattern: LLM proposes, deterministic code writes.

**2. LLM consolidation is lossy — and measurably worse than verbatim storage for factual recall.** A controlled ablation found verbatim chunks beat LLM-extracted "artifacts" for long-conversation memory (https://arxiv.org/pdf/2601.00821), and RecMem notes that summarizing at write time "collapses distinct episodes into semantic generalizations, destroying the episodic signal" (https://arxiv.org/html/2605.16045). Letta's sleep-time compute — an idle-time agent that rewrites memory blocks (https://www.letta.com/blog/sleep-time-compute/) — and A-MEM's "memory evolution," where new memories trigger LLM updates to *old* memories (https://arxiv.org/abs/2502.12110), are real and win dialogue benchmarks, but both are mutation-by-LLM. Fine for persona/preference memory; disqualifying for an experiment registry.

**3. Invalidate, never delete.** Zep's Graphiti temporal knowledge graph stores facts as edges with `valid_at`/`invalid_at` windows — superseded facts get invalidated, not erased — and constrains dedup to edges between the same entity pair to prevent erroneous merges (https://arxiv.org/abs/2501.13956). Steal the pattern, not the product: SQLite + JSONL suffices at your scale.

**4. Novelty gating at propose time is now standard in LLM alpha mining.** The MCTS factor-mining framework uses frequent-subtree avoidance to stop re-exploring equivalent formula structures (https://arxiv.org/html/2505.11122v1); AlphaAgent enforces originality against previously mined alphas (https://arxiv.org/html/2502.16789v2); QuantaAlpha constrains redundancy across hypothesis/expression/code (https://arxiv.org/abs/2602.07085); SAGE gates memory writes on novelty (https://arxiv.org/pdf/2605.30711).

**5. Provenance is a security boundary.** MemoryGraft shows agents imitate retrieved memories as ground truth because retrieval is similarity-only with no provenance check (https://arxiv.org/abs/2512.16962); the memory-security survey identifies the Store phase — promotion via compression/reflection, retention, auditability — as where corruption happens (https://arxiv.org/html/2604.16548v1). A laundered dead method re-entering via a plausible-sounding summary is the *same failure class* as a poisoning attack, just self-inflicted.

## Data flow for your second brain

```
EVENT (deep_validation run ends / shadow trade closes)
  → deterministic writer (Python, inside the pipeline) appends to events.jsonl
  → projector (pure function of the log) rebuilds brain.sqlite views:
      negative_results, lockbox_methods, loss_stats
  → MEMORY.md regenerated from SQL by template (narrative section separate, non-authoritative)
RETRIEVAL (proposer about to spend compute)
  → gate: exact hash → hard reject; near-dup → verbatim original record shown
  → loss_stats injected as a numbers table into the proposer prompt
  → decision
```

**Schema — one event, append-only (only method_lab/deep_validation/forward_test hold write access):**

```json
{"ts":"2026-07-06T04:11:00Z","type":"method_tested",
 "method_id":"sha256(canonical_dsl_ast)","dsl":"...","dsl_canonical":"...",
 "universe":["BTC","ETH"],"period":["2023-01-01","2026-04-01"],
 "bootstrap_p":0.41,"oos_sharpe":-0.2,"lockbox":"FAIL",
 "failure_mode":"decays_post_2024","code_commit":"abc123","parent":null}
```

**Novelty gate — deterministic core, embedding advisory only:**

```python
def canonicalize(dsl):   # parse to AST, sort commutative ops,
    ...                  # rename params positionally, bucket numerics (lookback 20≈21)
def gate(dsl):
    h = sha256(canonicalize(dsl))
    if h in neg_index:                      # exact: HARD BLOCK, no LLM appeal
        return REJECT(neg_index[h])
    for mid, sim in ann_search(embed(canonicalize(dsl)), k=5):
        if sim > 0.93:                      # near-dup: show ORIGINAL record verbatim
            return FLAG(mid)                # proposer must state the material difference
    return PASS
```

The Graphiti-style trick applies to FLAG resolution: if the proposer argues a genuine difference and it later fails too, link `parent` to the old `method_id` — you get failure lineages, not duplicates.

**Loss lessons as numbers:** a deterministic rollup over the shadow ledger — `(regime, ch24_bucket, session_hour) → {n, win_rate, avg_R, p}` — recomputed on every close. Narrative in MEMORY.md is generated *from* this table with event-id citations, never written first. This matches the survey distinction that episodic evidence ("corrected on Jan 5, 12, Feb 1") must survive underneath any semantic distillate (https://arxiv.org/pdf/2512.13564, https://arxiv.org/html/2603.07670v1).

**Read/write boundary:** LLM (Claude Code + Codex via MCP) gets read-only tools over `events.jsonl` + views, and write access only to `proposals/` and a scratch `notes/` layer that never feeds the gate. Compaction trigger = ledger growth or session start, and compaction *means* re-running the projector — never an LLM rewrite. Claude Code's own auto-compact is documented lossy compression (https://okhlopkov.com/claude-code-compaction-explained/, https://platform.claude.com/cookbook/tool-use-automatic-context-compaction), so durable state must live in files the agent re-reads, not in conversation summaries.

## Honest calls

- **Real:** event-sourced logs + deterministic projections; verbatim-over-summary storage; hash/AST novelty gates from alpha-mining literature; invalidate-don't-delete temporality.
- **Hype for your use case:** sleep-time "reflection" rewriting memories, A-MEM-style memory evolution, and Mem0's LLM-decided ADD/UPDATE/DELETE ops (https://vectorize.io/articles/mem0-vs-zep) — all reintroduce the laundering vector for ground-truth records. Use LLM consolidation only at *read* time (generating a briefing from immutable records), never at write time.
- **Underrated:** the poisoning literature (MemoryGraft, MemAudit https://arxiv.org/pdf/2605.23723) as a design checklist — treat your own proposer as a semi-trusted writer.

Sources: [ESAA](https://arxiv.org/abs/2602.23193), [ESAA-Conversational](https://arxiv.org/abs/2606.23752), [OpenHands SDK](https://arxiv.org/pdf/2511.03690), [Verbatim vs extracted](https://arxiv.org/pdf/2601.00821), [RecMem](https://arxiv.org/html/2605.16045), [Letta sleep-time compute](https://www.letta.com/blog/sleep-time-compute/), [Letta memory blocks](https://www.letta.com/blog/memory-blocks/), [A-MEM](https://arxiv.org/abs/2502.12110), [Zep/Graphiti](https://arxiv.org/abs/2501.13956), [Mem0 vs Zep](https://vectorize.io/articles/mem0-vs-zep), [Alpha MCTS](https://arxiv.org/html/2505.11122v1), [AlphaAgent](https://arxiv.org/html/2502.16789v2), [QuantaAlpha](https://arxiv.org/abs/2602.07085), [SAGE novelty gate](https://arxiv.org/pdf/2605.30711), [MemoryGraft](https://arxiv.org/abs/2512.16962), [Memory security survey](https://arxiv.org/html/2604.16548v1), [MemAudit](https://arxiv.org/pdf/2605.23723), [Memory in the Age of AI Agents survey](https://arxiv.org/pdf/2512.13564), [Memory mechanisms survey](https://arxiv.org/html/2603.07670v1), [Claude Code compaction](https://okhlopkov.com/claude-code-compaction-explained/), [Anthropic compaction cookbook](https://platform.claude.com/cookbook/tool-use-automatic-context-compaction).

---

## provenance-gating
# Write-Gating + Provenance for a Trading Agent's Second Brain

## Key patterns from 2024-2026 practice

**1. Deterministic memory kernels (no LLM in the mutation path).** The clearest working example is open-second-brain (https://github.com/itechmeat/open-second-brain): a plain-Markdown Obsidian vault where agents append *signals* (raw observations), and a nightly "dream pass" promotes repeated signals into confirmed preferences using **counters and atomic file moves — no LLM inside the algorithm**. Confidence comes from repetition count + recency decay, and unused rules are retired deterministically. This is exactly your anti-"memory-laundering" constraint, already shipped as an MCP-compatible tool shared across Claude Code/Codex. Letta's sleep-time compute (https://www.letta.com/blog/sleep-time-compute/) is the LLM-flavored version — a background agent rewrites memory blocks during idle time — useful for prose distillation but it is the *wrong* pattern for ground-truth records because the rewriter can hallucinate.

**2. Bi-temporal provenance, invalidate-don't-delete.** Zep/Graphiti (https://arxiv.org/abs/2501.13956, https://github.com/getzep/graphiti via https://help.getzep.com/graphiti/getting-started/overview) attaches four timestamps to every fact edge: `t_valid`/`t_invalid` (true in the world) and `t_created`/`t_expired` (known to the system). Superseded facts are invalidated, never deleted — history stays queryable. For a trading brain: a method that worked in the 2025 regime and died in 2026 keeps both records; nothing is summarized away.

**3. Write-gating as a privileged state transition.** The 2026 security literature converged on treating every memory write like a syscall: verify provenance, check consistency against existing memory, enforce authorization before commit. See the SSGM governance framework (https://arxiv.org/html/2603.11768v1), the memory-lifecycle security survey (https://arxiv.org/pdf/2604.16548), and the evidence-tracing survey (https://arxiv.org/pdf/2606.04990). Motivation is not academic: MINJA shows query-only memory injection with >95% injection success (https://openreview.net/forum?id=QINnsnppv8), and MemoryGraft shows poisoned *experience retrieval* persistently compromising agents (https://arxiv.org/html/2512.16962v1). Your "failed method re-summarized as novel" is self-inflicted MemoryGraft.

**4. Asymmetric negative memory — the strongest finding for you.** AlphaMemo (https://arxiv.org/pdf/2606.20625) found that in alpha mining, **negative search evidence is more stable than positive alpha signals**, so it stores failure motifs (extracted deterministically from AST diffs of factor expressions) and gives high-confidence negative patterns a **hard veto** over new candidates, while positive patterns only get a soft boost. FactorMiner (https://arxiv.org/pdf/2602.14670, code: https://github.com/minihellboy/factorminer) stores "forbidden regions" plus a validation pipeline (IC screen → correlation/dedup check → full validation). This is your negative-results DB, published and working.

## Data flow (event → store → retrieval → decision)

```
EVENT: deep_validation completes method M
  → Python code (NOT LLM) emits a record:
    {method_id: sha256(canonical_DSL_AST),   # novelty hash on normalized AST
     dsl: "...", universe: [...], period: [t0,t1],
     bootstrap_p: 0.31, oos_sharpe: -0.4, lockbox: "FAIL",
     failure_mode: "decays_after_cost", n_trials_family: 47,
     status: OBSERVED, t_created: now, source_run: run_id}
  → append-only JSONL + SQLite index (kernel does dedup via method_id)

DREAM PASS (cron, deterministic Python):
  → GROUP BY failure_mode/family; increment counters;
    confidence = f(repetitions, recency); promote to DERIVED rules
    ("momentum variants on <$50M universe: 0/23 pass lockbox")
  → loss lessons: aggregate shadow-ledger fills into numbers
    (avg slippage bps, MAE before stop, win-rate by regime tag)

RETRIEVAL (before LLM proposes):
  → proposer drafts DSL → kernel canonicalizes AST → exact-hash hit
    OR AST-edit-distance/correlation ≥ threshold vs dead pool
    → HARD VETO, return the graveyard record to the prompt
  → top-k DERIVED rules + matching negative records injected as
    read-only context ("do not re-derive; cite record IDs")

DECISION: mech_sizing reads only OBSERVED numbers (never LLM prose).
```

## Read/write boundary for your stack

- **LLM may write:** `hypothesis`-status notes only, into a quarantine file (`proposals/*.md`). Never into `results.jsonl`.
- **Only Python writes:** `OBSERVED` (backtest/lockbox/forward-test outputs, trade fills) and `DERIVED` (dream-pass aggregations). Guard against selection-bias laundering with deflated Sharpe / PBO per family — record `n_trials_family` so DSR is computable (https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551, https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf).
- **Everything reads:** MEMORY.md becomes a generated view (rendered from JSONL by the kernel), not the source of truth. Claude/Codex share it over MCP read-only tools; the write tool is one function: `record_result(payload)` with schema validation, called by pipeline code.
- **Invalidate, don't edit:** if a lockbox pass later fails in forward test, append a new record with `supersedes: <id>`, Graphiti-style bi-temporal fields.

## Hype vs real

- **Real:** deterministic counter-based promotion (open-second-brain), bi-temporal invalidation (Graphiti), negative-pattern veto (AlphaMemo/FactorMiner), DSR/PBO math. All boring, all robust.
- **Half-real:** Letta sleep-time compute improves context quality but reintroduces an LLM mutator; use only for the *narrative* layer, never numbers. Graphiti itself uses an LLM for entity extraction — for your structured records you don't need it; a SQLite/JSONL schema beats a knowledge graph here.
- **Hype:** "self-evolving agent memory" papers that let the model rewrite its own experience store show exactly the drift SSGM warns about; multi-agent memory surveys (https://arxiv.org/pdf/2605.06716) document confidence/provenance metadata as aspiration more than shipped practice. Also, vector-similarity dedup on prose descriptions is too weak for your novelty check — hash the **canonical DSL AST** and additionally compute return-stream correlation vs the dead pool (a method can be textually novel and statistically identical).

The one-sentence architecture: LLM proposes into quarantine; deterministic kernel validates, hashes, vetoes against the graveyard, records outcomes bi-temporally, and compiles numeric lessons on a cron; the LLM only ever *reads* ground truth it can cite by record ID.

---

## retrieval-assembly
# Retrieval + Context Assembly for the Trading Agent's Second Brain

## Key patterns from 2024-2026 practice

**1. Index-first / progressive disclosure, not bulk RAG.** The consensus pattern (Anthropic's context-engineering post, https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents; claude-mem's docs, https://docs.claude-mem.ai/context-engineering; MindStudio's writeup, https://www.mindstudio.ai/blog/progressive-disclosure-ai-agents-context-management) is a 3-layer disclosure: (L0) compact index cards ~50-100 tokens each (title, verdict, date, token cost), (L1) structured summary ~300 tokens, (L2) full record fetched on demand. The agent scans headlines, then pulls detail. claude-mem's motivating number: naive startup loading was 35k tokens with ~6% relevance. This maps directly onto your file-based MEMORY.md + per-topic files layout — you're already accidentally doing L0/L2; formalize L1.

**2. Hybrid retrieval with rank fusion + rerank.** Production standard is BM25 + dense vectors fused with Reciprocal Rank Fusion (rank-only, no score calibration), then a cross-encoder reranks top-100 → top-10 (https://www.digitalapplied.com/blog/hybrid-search-bm25-vector-reranking-reference-2026, https://glaforge.dev/posts/2026/02/10/advanced-rag-understanding-reciprocal-rank-fusion-in-hybrid-search/). For your corpus (hundreds-to-thousands of method records, not millions), BM25 alone over structured fields (indicator names, coin universe, timeframe) plus exact novelty-hash lookup does most of the work; embeddings only catch paraphrased re-proposals.

**3. Lost-in-the-middle: edge placement + fixed budgets.** Liu et al.'s finding (15-20% accuracy drop for middle positions) still holds; mitigations that work without model surgery: put highest-relevance items at the **start and end**, low-relevance in the middle; repeat the query after the context ("query-aware contextualization"); and cap evidence to a fixed budget, replacing rather than appending (ICLR 2025 long-context-meets-RAG, https://proceedings.iclr.cc/paper_files/paper/2025/file/5df5b1f121c915d8bdd00db6aac20827-Paper-Conference.pdf; "Replace, Don't Expand" fixed-budget assembly, https://arxiv.org/pdf/2512.10787; survey of mitigation, https://www.getmaxim.ai/articles/solving-the-lost-in-the-middle-problem-advanced-rag-techniques-for-long-context-llms/).

**4. Recall-adequacy verdicts.** Three concrete mechanisms: Self-RAG's reflection tokens [Retrieve]/[IsRel]/[IsSup] (https://selfrag.github.io/); CRAG's lightweight evaluator producing Correct/Ambiguous/Incorrect → refine / re-retrieve / fall back (https://www.kore.ai/blog/corrective-rag-crag); and Google's "sufficient context" autorater — key finding: **strong models answer wrongly instead of abstaining when context is insufficient**, and a guided-abstention gate improved correctness 2-10% (https://arxiv.org/abs/2411.06037). For a trading agent, abstain = NO TRADE / NO PROPOSAL, which is cheap. Bias hard toward abstention.

**5. Negative-knowledge banks are now a named pattern.** "Negative Knowledge as Failure-aware Shared Memory for AutoResearch" (https://arxiv.org/pdf/2606.21024) proposes a curator converting failed attempts into **bounded, typed records**; AgentX (https://arxiv.org/pdf/2606.26859) indexes failed experiments by root cause across multiple dimensions so agents don't rediscover dead paths; "Dead Science Walking" (https://arxiv.org/pdf/2606.04220) argues AI-scientist pipelines need machine-readable null results ("a map of where not to spend effort"). Your NEGATIVE-RESULTS DB is exactly this — and your deterministic-write constraint is *stricter* than the literature (their "curator agent" is an LLM; yours must not be, and you're right: an LLM curator is precisely where memory laundering happens).

## Data flow (event → store → retrieval → decision)

```
EVENT (deep_validation completes / trade closes)
  → deterministic writer (Python, no LLM):
      record = canonical JSON, append-only (JSONL/SQLite)
      novelty_hash = sha256(normalize(method_DSL) + universe + timeframe)
  → indexer: BM25 over DSL tokens+fields; hash table on novelty_hash;
      optional embeddings of the DSL text (not the LLM's summary!)
DECISION TIME (proposer wants to test idea X):
  1. exact gate: hash(normalize(X)) in dead_hashes → REJECT, cite record ID
  2. near-dup: BM25+embedding top-20 → RRF → cross-encoder or LLM-as-relevance-judge
  3. assemble context under budget (see below)
  4. adequacy verdict: sufficient / re-recall (relax filters, widen k) / abstain
  5. proposer answers WITH the retrieved cards in-window; verdict + card IDs
     logged to the audit trail
```

Negative-result record (deterministic fields only):

```json
{"id":"NR-0173","novelty_hash":"sha256:...","dsl":"zscore(close,48)<-2 & vol_pctl>90",
 "universe":"top100_liq50M","period":"2024-01..2026-03","bootstrap_p":0.41,
 "oos_sharpe":-0.3,"lockbox_verdict":"FAIL","failure_mode":"decay_after_2025q2",
 "n_trades":312,"tested_at":"2026-07-01","code_ref":"method_lab/runs/173"}
```

Context assembly (fixed budget, edge placement):

```
budget = 4000 tokens
cards = rerank(rrf(bm25, dense), k=12)          # L0 cards, ~80 tok each
slots: [top1, top3..k (middle), top2], query restated at the very end
expand to L1 only for top-3; L2 (full record) only on explicit tool call
verdict = sufficient if (top1_score > τ AND covers universe+timeframe of query)
        | re_recall  if best score < τ but corpus nonempty (widen once, max 2)
        | abstain    if two re-recalls fail → "no relevant history; treat as novel"
```

## Hype vs real

Real: progressive disclosure, RRF hybrid search, edge placement, abstention gates, deterministic append-only stores. All boring, all proven. Overhyped for your scale: temporal knowledge graphs (Zep/Graphiti, https://www.agenticwire.news/article/mem0-zep-letta-agent-memory) and managed memory layers (Mem0, Letta — https://tokenmix.ai/blog/ai-agent-memory-mem0-vs-letta-vs-memgpt-2026) — their LoCoMo/LongMemEval numbers are conversational-recall benchmarks, not experiment registries, and their **LLM-driven extraction pipelines violate your no-LLM-mutation constraint by design**. FinMem's recency/relevance/importance-scored layered memory (https://arxiv.org/abs/2311.13743) is worth stealing the *scoring triple* from, but its trading results are backtest-era and not evidence of live edge.

## What this means for your stack

Store: (a) LOCKBOX-validated methods as full L2 records + auto-generated L0 cards (verdict, p-value, sharpe, provenance pointer); (b) negative results in SQLite (JSONL is fine to start; you need the hash index more than the DB); (c) loss lessons as numeric rows (entry slippage bps, MAE/MFE, funding paid, gate that should have fired) generated by a deterministic post-trade script from the shadow ledger — never by the LLM. Write boundary: only `deep_validation` and `forward_test` processes hold write handles; expose read-only MCP tools (`recall_methods`, `check_novelty`, `recall_losses`) to Claude Code/Codex; the proposer's *only* write path is proposing a new DSL, which the pipeline hashes and checks before spending compute. MEMORY.md stays as the human-readable L0 index, regenerated from the DB — not hand-edited by the model.

Sources: [Anthropic context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents), [claude-mem](https://docs.claude-mem.ai/context-engineering), [MindStudio progressive disclosure](https://www.mindstudio.ai/blog/progressive-disclosure-ai-agents-context-management), [Sufficient Context (arXiv 2411.06037)](https://arxiv.org/abs/2411.06037), [Self-RAG](https://selfrag.github.io/), [CRAG](https://www.kore.ai/blog/corrective-rag-crag), [Negative Knowledge for AutoResearch](https://arxiv.org/pdf/2606.21024), [AgentX](https://arxiv.org/pdf/2606.26859), [Dead Science Walking](https://arxiv.org/pdf/2606.04220), [Replace Don't Expand](https://arxiv.org/pdf/2512.10787), [ICLR 2025 long-context RAG](https://proceedings.iclr.cc/paper_files/paper/2025/file/5df5b1f121c915d8bdd00db6aac20827-Paper-Conference.pdf), [lost-in-the-middle mitigations](https://www.getmaxim.ai/articles/solving-the-lost-in-the-middle-problem-advanced-rag-techniques-for-long-context-llms/), [hybrid search reference](https://www.digitalapplied.com/blog/hybrid-search-bm25-vector-reranking-reference-2026), [RRF explainer](https://glaforge.dev/posts/2026/02/10/advanced-rag-understanding-reciprocal-rank-fusion-in-hybrid-search/), [FinMem](https://arxiv.org/abs/2311.13743), [memory platform comparison](https://tokenmix.ai/blog/ai-agent-memory-mem0-vs-letta-vs-memgpt-2026), [Zep/Mem0/Letta](https://www.agenticwire.news/article/mem0-zep-letta-agent-memory)

---

## forgetting-decay
# Forgetting, Decay, TTL, and Retirement for the Trading Agent's Second Brain

## What real systems actually do (2024–2026)

Production practice has converged on a critical distinction: **decay is a retrieval-time re-ranking layer; eviction is deletion — and they are different mechanisms with different risk profiles.** Mem0's "Memory Decay" ([mem0.ai/blog/introducing-memory-decay-in-mem0](https://mem0.ai/blog/introducing-memory-decay-in-mem0)) deletes nothing: recently-accessed memories get up to 1.5x score boost, idle ones dampen toward 0.3x, tracked via last-20 access timestamps. Actual eviction (TTL, LRU, supersession-on-contradiction) is separate ([mem0.ai/blog/memory-eviction-and-forgetting-in-ai-agents](https://mem0.ai/blog/memory-eviction-and-forgetting-in-ai-agents)). The recommended production stack there is: TTL on long-tail entries to bound storage, LRU-style decay on retrieval scores to bound interference, supersession on every write so contradictions never accumulate.

Second pattern: **invalidate, don't delete.** Zep/Graphiti's bi-temporal graph ([arxiv.org/abs/2501.13956](https://arxiv.org/html/2501.13956v1), [github.com/getzep/graphiti](https://github.com/getzep/graphiti)) stamps every fact with `valid_at`/`invalid_at` plus ingestion timestamps; a superseding fact sets `invalid_at` on the old edge instead of deleting it. This answers archive-vs-delete cleanly: current-state queries filter on `invalid_at IS NULL`, history remains auditable. Kafka log compaction is the infra-level analogue — keep latest value per key, tombstones themselves expire after `delete.retention.ms` ([docs.confluent.io/kafka/design/log_compaction.html](https://docs.confluent.io/kafka/design/log_compaction.html)).

Third: **tiering with deterministic eviction.** Letta/MemGPT's core/recall/archival tiers evict from the context window by recursive summarization, never destroying the underlying record ([letta.com/blog/agent-memory](https://www.letta.com/blog/agent-memory/)). Fourth: **outcome-driven skill retirement.** Recent work (Ratchet, [arxiv.org/html/2605.22148v1](https://arxiv.org/html/2605.22148v1); SkillBrew, [arxiv.org/pdf/2605.29440](https://arxiv.org/pdf/2605.29440)) fixes Voyager's append-only skill library by having a curator retire underperformers on live scores — retirement as a state machine, not a delete.

**Hype-vs-real calls.** (1) Ebbinghaus-curve forgetting (MemoryBank's `R = e^(-t/S)`, [arxiv.org/abs/2305.10250](https://arxiv.org/abs/2305.10250)) is cited constantly but is cosmetic in production — what ships is TTL + supersession + recency re-rank. (2) Benchmarks show systems that ace passive recall drop to 40–60% on *active* selective forgetting ([arxiv.org/html/2603.07670v1](https://arxiv.org/html/2603.07670v1)) — don't trust "the LLM will know what to forget." (3) Several papers (e.g., SIMPLIFY-style LLM pruning that "categorizes records for deletion/merging/refinement") put the LLM in the mutation path. For your system that is precisely the memory-laundering vector; the research trend does NOT override your constraint. (4) The finance literature gives the decisive argument for why the negative-results DB must never decay: the Deflated Sharpe Ratio requires a complete count of trials ever run to correct for selection bias ([ssrn.com/abstract=2460551](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551), [en.wikipedia.org/wiki/Deflated_Sharpe_ratio](https://en.wikipedia.org/wiki/Deflated_Sharpe_ratio)). Deleting a dead trial silently un-deflates every future Sharpe estimate.

## Lifecycle rules per record class

**Negative-results DB: append-only, immortal, never decayed at retrieval.** It is simultaneously your dedup gate and your multiple-testing trial registry. Bound it by compacting *artifacts*, not rows: the summary row (~1 KB: DSL hash, universe, period, p-value, OOS/lockbox outcome, failure_mode) is eternal — 50k dead methods ≈ 50 MB, storage is a non-problem over years. Heavy artifacts (full bootstrap distributions, per-trade arrays) go to a cold parquet tier with a 12-month TTL; keep only summary stats after that (mean, sd, p-value, n_boot).

**LOCKBOX-validated methods: these DO decay — alpha decay is real** ([mavensecurities.com/alpha-decay-what-does-it-look-like](https://www.mavensecurities.com/alpha-decay-what-does-it-look-like-and-what-does-it-mean-for-systematic-traders/)). Staleness detection = the forward_test shadow ledger, not timestamps: state machine `active → probation → retired` driven by rolling live stats (e.g., probation when 60-trade rolling Sharpe < 0.5× validation Sharpe; retire when a CUSUM on live-vs-expected returns breaks, mirroring PSI-style drift monitoring in feature stores, [labelyourdata.com/articles/machine-learning/data-drift](https://labelyourdata.com/articles/machine-learning/data-drift)). Retirement is a Graphiti-style supersession: set `invalid_at`, write a negative-results row with `failure_mode='alpha_decay'` so the proposer can't resurrect it.

**Loss lessons: decay at retrieval only.** Keep the numeric ledger append-only; re-rank lessons by regime similarity plus a reinforcement counter (Mem0-style boost when a lesson actually changed a decision). **Working memory (MEMORY.md): bounded FIFO view, regenerated — never hand-edited by the LLM.** Treat it as Letta core memory: a deterministic compaction job renders the top-K active methods + last-N lessons from SQLite. If it drifts, delete and regenerate; it is a cache, not a store.

## Data flow and schema

```
proposer(LLM) --DSL--> novelty gate --> deep_validation --> deterministic writer --> SQLite
                          |  exact: sha256(canonical_ast+universe+timeframe)
                          |  near:  MinHash/embedding sim > 0.92 -> REJECT w/ prior row
forward_test nightly --> staleness monitor --> state transitions (tombstone rows)
retrieval: LLM gets READ-ONLY MCP tools; MEMORY.md regenerated as materialized view
```

```sql
CREATE TABLE trials (            -- negative results + wins, one registry
  novelty_hash TEXT PRIMARY KEY, dsl_canonical TEXT, universe_id TEXT,
  period TEXT, bb_pvalue REAL, oos_sharpe REAL, lockbox_sharpe REAL,
  verdict TEXT CHECK(verdict IN ('pass','fail')), failure_mode TEXT,
  artifact_uri TEXT,             -- cold tier, TTL 12mo
  created_at TEXT);              -- rows are IMMUTABLE
CREATE TABLE method_state (
  novelty_hash TEXT, state TEXT, valid_at TEXT, invalid_at TEXT,
  reason TEXT);                  -- bi-temporal; retirement = new row, old gets invalid_at
```

**Read/write boundary:** LLM tools = `check_novelty(dsl)`, `query_trials(filters)`, `get_lessons(regime)` — read-only. Writes happen only inside method_lab/deep_validation/forward_test Python code paths. The LLM may append free-text to a separate `annotations` table that is *never* consulted by the novelty gate — that single rule kills memory laundering: a failed method's hash matches regardless of how persuasively it gets re-described.

Sources: [Mem0 decay](https://mem0.ai/blog/introducing-memory-decay-in-mem0), [Mem0 eviction](https://mem0.ai/blog/memory-eviction-and-forgetting-in-ai-agents), [Zep/Graphiti paper](https://arxiv.org/html/2501.13956v1), [Graphiti repo](https://github.com/getzep/graphiti), [Letta agent memory](https://www.letta.com/blog/agent-memory/), [Kafka log compaction](https://docs.confluent.io/kafka/design/log_compaction.html), [MemoryBank](https://arxiv.org/abs/2305.10250), [Ratchet](https://arxiv.org/html/2605.22148v1), [SkillBrew](https://arxiv.org/pdf/2605.29440), [memory survey](https://arxiv.org/html/2603.07670v1), [Deflated Sharpe Ratio](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551), [DSR overview](https://en.wikipedia.org/wiki/Deflated_Sharpe_ratio), [alpha decay](https://www.mavensecurities.com/alpha-decay-what-does-it-look-like-and-what-does-it-mean-for-systematic-traders/), [drift monitoring](https://labelyourdata.com/articles/machine-learning/data-drift)

---

## quant-practice
# Institutional memory for a self-improving quant agent: how real firms store research, backtests, and the dead-strategy graveyard

## What real firms actually do

**1. Versioned, bitemporal data as the foundation.** Man Group built [ArcticDB](https://arcticdb.io/) because reproducibility broke at scale: it versions every DataFrame write automatically, giving point-in-time reads of any prior state ("the backtest I ran in March, on the data as it existed in March"), serverless on object storage ([github.com/man-group/ArcticDB](https://github.com/man-group/ArcticDB), now co-developed with Bloomberg: [man.com press release](https://www.man.com/man-group-brings-powerful-dataframe-database-product-arcticdb-to-market-with-bloomberg)). The general pattern is **bitemporality** — every record carries `valid_time` (when it was true in the market) and `transaction_time` (when your system learned it) — standard in kdb+ shops ([Data Intellect on kdb+ bitemporal](https://dataintellect.com/blog/kdb-temporal-bitemporal-data-kdb-1/), [Wikipedia](https://en.wikipedia.org/wiki/Bitemporal_modeling), [XTDB](https://v1-docs.xtdb.com/concepts/bitemporality/)). This is what makes "re-run the exact backtest" possible at all.

**2. A trials registry, not just a results DB.** The single most important institutional practice: log **every backtest ever run**, including failures, because the Deflated Sharpe Ratio requires the number and variance of trials to deflate the winner ([Bailey & López de Prado, SSRN 2460551](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551), [overview](https://en.wikipedia.org/wiki/Deflated_Sharpe_ratio)). The factor-zoo replication literature shows why: 65–82% of published factors fail replication under multiple-testing correction ([Portfolio123 summary](https://blog.portfolio123.com/thoughts-on-is-there-a-replication-crisis-in-finance/), [corporate-bond replication crisis](https://arxiv.org/pdf/2604.07880)). ML at large has the same disease — ~95% of papers report only positive results ([Questionable Practices in ML](https://arxiv.org/pdf/2407.12220)). Your negative-results DB is exactly this registry; the novel part is wiring it into an LLM proposer.

**3. Experiment tracking + feature stores are real but partial.** MLflow/W&B-style tracking (params, metrics, artifacts, lineage) is standard ([W&B "Architecting Alpha"](https://wandb.ai/site/articles/architecting-alpha-the-modern-quant-lifecycle/)); feature stores add point-in-time-correct joins to kill leakage ([Feast PIT joins](https://docs.feast.dev/getting-started/concepts/point-in-time-joins), [Hopsworks](https://www.hopsworks.ai/post/feature-store-benchmark-comparison-hopsworks-and-feast)). **Hype-vs-real call:** at your scale (one agent, dozens of methods, one machine) a full feature store is overkill — the pattern you steal is PIT-correctness and the trials ledger, implemented as SQLite + Parquet. Likewise lakeFS/Delta time-travel ([comparison](https://lakefs.io/blog/dvc-vs-git-vs-dolt-vs-lakefs/)) collapses to "pin a content-hash of the candle dataset per experiment."

**4. LLM alpha-mining dedup is an active 2025-26 research front.** [AlphaAgent (KDD'25)](https://arxiv.org/pdf/2502.16789) regularizes exploration against alpha decay and crowding; [Alpha Jungle MCTS](https://arxiv.org/abs/2505.11122) uses **frequent-subtree avoidance** on factor expression trees to block re-generating homogenized formulas; [AlphaMemo](https://arxiv.org/pdf/2606.20625) stores the *search process* (failed branches catalogued with semantic dedup so dead paths aren't re-explored). On the agent-memory side, [Zep/Graphiti](https://arxiv.org/abs/2501.13956) and the memory-contamination literature ([MemGuard](https://arxiv.org/pdf/2605.28009), [SSGM](https://arxiv.org/html/2603.11768v1)) validate your hard constraint: LLM-summarized memory drifts and gets poisoned; ground truth must be written deterministically. [FinMem](https://arxiv.org/abs/2311.13743) shows layered retrieval (novelty/relevance/importance scoring) works at decision time — but its memories are text; yours should be rows.

## Data flow for your second brain

```
EVENT                    DETERMINISTIC WRITE (Python, no LLM)        READ (LLM allowed)
─────                    ────────────────────────────────────        ──────────────────
method_lab proposes  →   trials.insert(canonical_dsl, novelty_hash)  proposer gets REJECT
deep_validation runs →   trials.update(pvalue, oos, lockbox, N_trials++)   if hash/embedding hit
lockbox pass         →   lockbox_methods.insert(full provenance)     mech_sizing reads
real trade closes    →   trade_ledger.insert(numeric autopsy)        pre-trade gate query
```

**Schemas (SQLite, append-only; LLM has read-only connection):**

```sql
CREATE TABLE trials (              -- the graveyard + registry, one row per tested method
  novelty_hash TEXT PRIMARY KEY,   -- sha256 of CANONICALIZED DSL (see below)
  dsl TEXT, universe TEXT, period_start TEXT, period_end TEXT,
  dataset_hash TEXT,               -- content-hash of candle data (poor man's lakeFS)
  code_commit TEXT, seed INTEGER,  -- exact reproducibility
  bootstrap_pvalue REAL, oos_sharpe REAL, lockbox_sharpe REAL,
  verdict TEXT CHECK(verdict IN ('DEAD','LOCKBOX_PASS','PENDING')),
  failure_mode TEXT,               -- enum: 'no_edge','overfit','cost_kill','regime_dependent'
  embedding BLOB, tested_at TEXT); -- embedding of DSL for semantic dedup
CREATE TABLE trade_lessons (       -- numbers, not narrative
  trade_id TEXT, method_hash TEXT, r_multiple REAL, slippage_bps REAL,
  mae_bps REAL, mfe_bps REAL, regime_tag TEXT, gate_flags TEXT, ts TEXT);
```

**Novelty check (the anti-re-test gate), run BEFORE any backtest:**

```python
def is_novel(dsl):
    canon = canonicalize(dsl)          # sort commutative ops, normalize param names,
                                       # bucket numeric params (e.g. lookback 20 vs 21 == same)
    h = sha256(canon)
    if db.exists(h): return False, db.row(h)            # exact dead match
    sims = db.knn(embed(canon), k=5)                    # semantic near-dupes
    for s in sims:
        if s.cos > 0.93 and s.verdict == 'DEAD':
            return False, s                             # AlphaMemo/Alpha-Jungle pattern
    return True, None
```

Count `N_trials` per family and feed it into deflated-Sharpe/your bootstrap threshold — the significance bar must **rise** as the graveyard grows (this is the López de Prado bookkeeping most hobby systems skip).

**Retrieval at decision time:** the proposer prompt gets (a) the top-k nearest graveyard rows to its draft idea, verbatim (`DEAD: momentum_5m/alt-universe, p=0.41, failure=cost_kill`), and (b) aggregate lesson numbers (`median slippage on <$50M-liquidity coins: 38bps; win-rate in regime=chop: 22%`) computed by SQL, not summarized by an LLM. This kills memory laundering structurally: the LLM never rewrites the record, it only reads rows and may write *proposals* into a separate `hypotheses` table that carries zero evidential weight.

**Concretely for your stack:** keep MEMORY.md for human-facing narrative only; move all ground truth into `research.db` (SQLite) + Parquet artifacts per trial (equity curve, bootstrap distribution) keyed by `novelty_hash`; expose it to Claude Code/Codex via a read-only MCP tool (`query_graveyard`, `get_lessons`) and a single deterministic writer inside `deep_validation`/`forward_test`. That is the entire institutional pattern — ArcticDB-style pinned data, DSR-style trials ledger, AlphaMemo-style dead-branch dedup — scaled down to one box.

---

## multi-agent-shared
# Multi-Agent Shared Memory for a Self-Improving Trading Agent: Field Report

## 1. What real systems actually do (2024–2026)

**Pattern A — Single deterministic writer, many LLM readers.** The strongest recent finding is that your hard constraint is now research-validated. Memory-poisoning work shows self-evolving agents get persistently compromised through their own experience stores: [MemoryGraft](https://arxiv.org/html/2512.16962v1) implants records that survive across sessions, and [OEP](https://arxiv.org/pdf/2605.18930) poisons self-evolving agents with "locally correct but non-transferable experiences" that get over-generalized during reflection — which is exactly "memory laundering" (a failed method re-summarized until it looks novel). OWASP now classifies memory poisoning as agentic risk ASI06 ([survey](https://arxiv.org/html/2606.04329v1)). The defense the literature converges on is a trust boundary at write time: LLMs propose, deterministic code commits.

**Pattern B — Git-backed SQLite+JSONL as the shared store.** Steve Yegge's [Beads](https://github.com/steveyegge/beads) ([intro post](https://steve-yegge.medium.com/introducing-beads-a-coding-agent-memory-system-637d7d92514a)) is the most battle-tested multi-agent shared store in the coding-agent world: writes go to SQLite immediately, export to JSONL, git is the sync/merge layer, and **hash-based IDs** (`bd-a3f2`) eliminate "both agents created record #10" collisions. Claude Code and Codex both consume it via CLI. This maps 1:1 onto your stack.

**Pattern C — Scoped access control, not free-for-all.** The [Collaborative Memory paper](https://arxiv.org/html/2505.18279v1) formalizes asymmetric read/write permissions per agent with full provenance on every fragment. Heavyweight academically, but the core idea — every record carries `owner`, `writer`, `read_scope` tags — is cheap to implement. Concurrency research ([CoAgent](https://arxiv.org/html/2606.15376), [CodeCRDT](https://arxiv.org/pdf/2510.18893)) explores 2PL, CRDTs, and commit-time validation; the honest call is that at your scale (2 agents, tens of writes/day) **SQLite WAL + `BEGIN IMMEDIATE` is sufficient** and CRDTs are overkill.

**Pattern D — Bitemporal provenance.** [Zep/Graphiti](https://arxiv.org/abs/2501.13956) timestamps every fact with event-time AND ingestion-time. You don't need their SaaS — just steal the two columns. [FinMem](https://arxiv.org/pdf/2311.13743) is the relevant trading-specific design: layered memory with retrieval ranked by novelty × relevance × importance, and immediate-vs-extended reflection on P&L.

**Hype-vs-real:** [OpenMemory MCP](https://mem0.ai/blog/introducing-openmemory-mcp)/Mem0-style vector memory is the wrong tool for ground truth — semantic similarity search over LLM-extracted "facts" is literally a laundering machine (paraphrase changes the embedding; a dead idea retrieves as "related work," not "REJECTED"). Ground-truth records need **exact keys and deterministic joins**. Vector search is fine for the soft layer only (lesson retrieval). [LangGraph Store namespaces](https://docs.langchain.com/oss/python/langchain/long-term-memory) are fine but buy you nothing over SQLite here.

## 2. Concrete architecture for the second brain

One store: `memory/brain.db` (SQLite, WAL mode) + `memory/*.jsonl` append-only exports committed to git (Beads pattern). `MEMORY.md` becomes a **generated view**, never hand-edited.

**Write boundary (deterministic only):** the only code allowed to write is `memory/writer.py`, called by `deep_validation`, `forward_test`, and the trade-close hook. Claude Code and Codex get **read-only MCP tools** (`check_novelty`, `query_negative`, `get_lessons`, `search_methods`). Neither agent ever holds a write handle; there is no `write_memory` tool to poison.

**Schema — negative-results record (the crown jewel):**

```json
{
  "id": "m-8f3ac1",                      // sha256(canonical_dsl)[:6], Beads-style
  "novelty_hash": "8f3ac1...",           // exact-dup key
  "family_sig": [0.12, 0.88, ...],       // feature vector of DSL AST for near-dup
  "dsl": "cross(ema(c,9), ema(c,21)) & vol_z > 1.5",
  "universe": "top50_liquid_gt_50M",     // your falling-knife lesson, encoded
  "period": ["2024-01-01","2026-05-01"],
  "bootstrap_p": 0.31, "n_trials_family": 47,   // feeds Deflated Sharpe
  "oos_sharpe": -0.2, "lockbox_sharpe": null,
  "verdict": "REJECTED", "failure_mode": "decays_after_2024_regime",
  "event_time": "...", "ingest_time": "...",     // bitemporal (Zep)
  "writer": "deep_validation@a1b2c3", "git_commit": "..."
}
```

`n_trials_family` matters: [Bailey & López de Prado's Deflated Sharpe Ratio](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf) requires knowing how many trials you ran — the negative-results DB is *also* your multiple-testing ledger, raising the bar for every future candidate in the same family.

**Novelty check (blocks re-proposal):**

```python
def check_novelty(dsl):
    h = sha256(canonicalize_ast(parse(dsl)))       # sort operands, bucket params
    if exact_match(h): return REJECT(prior_record)
    sims = cosine_topk(ast_features(dsl), family_sigs, k=5)
    if max(sims) > 0.92: return WARN(nearest, its_verdict, its_n_trials)
    return NOVEL
```

Canonicalization (commutative-op sorting, parameter bucketing: `ema(9)`≈`ema(10)`) is what defeats laundering — the LLM can rephrase prose but cannot rephrase a canonical AST hash.

**Loss lessons as numbers, not narrative:** `{regime: "ch24<-15", n: 7, avg_pnl: -0.31, max_dd: -0.89, rule: "block_short_capitulation", threshold: -15.0}` — retrieved by exact regime-tag match, never by embedding.

## 3. Data flow, end to end

1. **Event:** `deep_validation` finishes → emits result dict.
2. **Store:** `writer.py` validates against JSON Schema, computes `id`/`novelty_hash`, `BEGIN IMMEDIATE` → upsert SQLite → append JSONL → `git commit` (audit trail; either agent can `git pull` on another machine).
3. **Retrieval (proposal time):** LLM proposer drafts DSL → **mandatory** `check_novelty` MCP call → exact/near-dup verdict + `n_trials_family` injected into the proposer prompt with the prior failure mode. Reject-before-compute.
4. **Retrieval (decision time):** `futures_watch` queries `lessons WHERE regime_tags MATCH current_state` — deterministic SQL, numbers straight into the gating layer (your Layer-5 counter-momentum block becomes one row, not prose).
5. **Back into decisions:** FinMem-style — immediate reflection on trade close writes a numeric lesson candidate; it only becomes a *rule* after `n≥5` occurrences with consistent sign (deterministic promotion, no LLM vote).

This mirrors [Anthropic's own multi-agent system](https://www.anthropic.com/engineering/multi-agent-research-system): externalize state to durable memory before context dies, and keep coordination artifacts out of the LLM's mutable reach.

**Bottom line:** skip Mem0/Zep/OpenMemory for ground truth; build Beads-shaped (SQLite WAL + JSONL + git + hash IDs, ~300 lines), expose read-only MCP tools to both Claude Code and Codex, make `writer.py` the single deterministic writer, and treat the negative-results table as your Deflated-Sharpe trial ledger. The only vector index in the system is the AST-feature near-dup detector — used to *block* ideas, not to remember them fondly.

---

## failure-modes
# Memory Failure Modes & Defenses for a Self-Improving Trading Agent's Second Brain

## 1. The four failure modes, per 2024-2026 research

**Poisoning.** AgentPoison (NeurIPS 2024, https://arxiv.org/abs/2407.12784) showed a <0.1% poison rate in a RAG memory yields ≥80% attack success; MINJA (https://arxiv.org/abs/2503.03704) showed 98% injection success via *query-only* interaction — the agent itself writes the poisoned reasoning trace into its own memory. Follow-ups: MemoryGraft (arXiv:2512.16962), sleeper-trigger attacks (https://arxiv.org/pdf/2605.28201), and the lifecycle survey https://arxiv.org/pdf/2604.16548. Key lesson: the dominant vector is not an external attacker editing your DB — it's the LLM *self-writing* contaminated experience. For your agent, "attacker" includes noisy market data and the proposer's own motivated reasoning.

**Laundering.** Exactly your hard-won constraint, now formalized: "LLM agents are not always faithful self-evolvers" (https://arxiv.org/pdf/2601.22436) and "Rethinking Experience Utilization" (https://arxiv.org/pdf/2605.07164) document that summarization-based memory consolidation drifts records toward what the model *wants* to believe — failed experiences get re-encoded as plausible. MemGuard (https://arxiv.org/pdf/2605.28009) and SSGM (https://arxiv.org/pdf/2603.11768) both converge on the same defense: LLM proposes, deterministic code *disposes* on writes.

**Temporal contamination.** The strongest recent finance-specific literature: "The Alpha Illusion" (https://arxiv.org/html/2605.16895v1), "All Leaks Count, Some Count More" (https://arxiv.org/html/2602.17234), Look-Ahead-Bench (https://arxiv.org/pdf/2601.13770), and "A Test of Lookahead Bias in LLM Forecasts" (https://arxiv.org/pdf/2512.23847). Consensus: LLM backtest alpha collapses post-training-cutoff; memory that stores outcomes without `as_of` timestamps recreates lookahead bias *inside your own second brain* (e.g., a loss-lesson computed with post-hoc data leaks into a decision replayed at an earlier timestamp).

**Context rot.** Chroma's 18-model study (https://www.trychroma.com/research/context-rot) shows monotonic F1 degradation with input length, with distractor interference the worst failure — semantically similar but irrelevant memories actively mislead. Implication: retrieval must return *few, high-precision* records, not "everything about SOL shorts."

## 2. Data flow (event → store → retrieval → decision)

```
EVENT (deep_validation completes / forward_test trade closes)
  │  deterministic Python only — LLM never on this path
  ▼
WRITE: append-only JSONL ledger (event-sourced; edits = new records superseding old)
  method_registry.jsonl   ← lockbox-validated methods, full provenance
  graveyard.jsonl         ← negative results (see schema)
  loss_ledger.jsonl       ← numeric loss lessons
  │
  ▼
INDEX (derived, rebuildable): novelty-hash set + embedding index over DSL text
  │
  ▼
READ (LLM-facing, read-only):
  proposer  → novelty gate: exact-hash reject, then cosine>0.92 vs graveyard → reject w/ tombstone quote
  decision  → top-k=3 loss_ledger rows matching (regime, setup) as numbers-only table
  │
  ▼
DECISION → outcome → new EVENT (loop closes)
```

**Graveyard schema** (one row per tested method — your item (b), concretized):

```json
{"id":"m_0412","dsl_sha256":"…","dsl_canonical":"cross(ema12,ema48) & vol_z>1.5",
 "universe":"top50_liq_50M","period":["2023-01-01","2026-03-01"],
 "block_bootstrap_p":0.41,"oos_sharpe":-0.3,"lockbox_sharpe":null,
 "verdict":"DEAD","failure_mode":"no_edge_after_costs",
 "tested_at":"2026-06-14T…Z","as_of_data_cutoff":"2026-03-01",
 "embedding_id":"e_0412","superseded_by":null}
```

Canonicalize the DSL (sorted params, normalized names) before hashing, or trivial rewrites defeat the novelty gate — this is the deterministic analogue of the "novelty gate" in SAGE (https://arxiv.org/pdf/2605.30711).

**Novelty gate pseudocode:**

```python
def gate(proposal_dsl):
    canon = canonicalize(proposal_dsl)
    if sha256(canon) in dead_hashes: return REJECT(exact=True)
    hits = graveyard_index.search(embed(canon), k=5)
    near = [h for h in hits if h.score > 0.92]
    if near:  # feed tombstones back verbatim, never summarized
        return REJECT(tombstones=[h.raw_json for h in near])
    return PASS
```

Rejection feedback must quote the raw graveyard JSON — an LLM-paraphrased rejection is itself a laundering channel.

**Loss lessons as numbers** (your item (c)): store `{regime_tag, setup_hash, n_trades, win_rate, avg_R, worst_R, mae_p95, slippage_bps_realized}` aggregated by deterministic code from the shadow ledger. Retrieval renders a 3-row table into the prompt. Narrative "lessons" are where laundering lives; numbers can't be reinterpreted.

**Temporal defense:** adopt Zep/Graphiti's bi-temporal model (https://arxiv.org/abs/2501.13956, https://github.com/getzep/graphiti) — every record carries `event_time` and `as_of_data_cutoff`; supersede, never delete. At retrieval, filter `as_of <= decision_time` so replayed/backtested decisions can't see future-derived lessons. This is the single highest-value idea to steal from the tooling ecosystem.

## 3. Hype vs. real

- **Mem0/Zep/Letta "SOTA memory" claims: mostly hype for your use case.** The Mem0-vs-Zep benchmark fight (https://blog.getzep.com/lies-damn-lies-statistics-is-mem0-really-sota-in-agent-memory/, https://github.com/getzep/zep-papers/issues/5) shows vendor LoCoMo scores swing 20+ points with eval setup; LoCoMo tests conversational recall, not structured provenance. Your workload is a *database problem* — JSONL/SQLite + deterministic writers beats any memory SaaS. Steal Graphiti's bi-temporal schema; skip the products.
- **"Agentic memory" (A-MEM, https://arxiv.org/pdf/2502.12110) where the LLM reorganizes its own memory: real research, wrong for ground truth.** Faithfulness results (arXiv:2601.22436) say exactly what you learned: self-curated memory drifts. Fine for a scratch/hypothesis tier, banned for the registry/graveyard/ledger.
- **Certified poisoning defenses (SMSR, https://arxiv.org/pdf/2606.12703; MEMSAD, https://arxiv.org/pdf/2605.03482): early-stage.** Your deterministic-write boundary is strictly stronger than any detection-based defense — MINJA's authors note Llama Guard-style filtering fails anyway.
- **Context rot mitigation is real and cheap:** cap retrieval at k≤3-5 per tier, render as compact tables, never dump MEMORY.md wholesale into the proposer.

## 4. Concrete recommendations for your stack

1. Split MEMORY.md: keep it as human-readable *index only*; move ground truth to `memory/{method_registry,graveyard,loss_ledger}.jsonl`, append-only, written solely by `deep_validation`/`forward_test` code paths.
2. Expose via MCP as **read-only tools** (`query_graveyard`, `get_loss_stats`, `check_novelty`) shared by Claude Code + Codex; there is no write tool — writes happen inside the Python pipeline.
3. Add `as_of_data_cutoff` to every record now; retrofitting bi-temporality later is painful.
4. Nightly deterministic audit: recompute novelty hashes, verify no record mutated (hash-chain the JSONL), diff embedding-index vs. source — this catches both bugs and any laundering leak.
5. Log every gate rejection with the tombstone shown; if the proposer re-proposes a rejected idea reworded, that's your laundering canary metric.

Sources: [AgentPoison](https://arxiv.org/abs/2407.12784), [MINJA](https://arxiv.org/abs/2503.03704), [memory-lifecycle security survey](https://arxiv.org/pdf/2604.16548), [MemGuard](https://arxiv.org/pdf/2605.28009), [SSGM](https://arxiv.org/pdf/2603.11768), [unfaithful self-evolvers](https://arxiv.org/pdf/2601.22436), [Alpha Illusion](https://arxiv.org/html/2605.16895v1), [temporal contamination detection](https://arxiv.org/html/2602.17234), [Look-Ahead-Bench](https://arxiv.org/pdf/2601.13770), [lookahead bias test](https://arxiv.org/pdf/2512.23847), [Chroma context rot](https://www.trychroma.com/research/context-rot), [Zep bi-temporal KG](https://arxiv.org/abs/2501.13956), [SAGE novelty gate](https://arxiv.org/pdf/2605.30711), [A-MEM](https://arxiv.org/pdf/2502.12110), [Zep benchmark critique](https://blog.getzep.com/lies-damn-lies-statistics-is-mem0-really-sota-in-agent-memory/), [SMSR](https://arxiv.org/pdf/2606.12703).

---

## ground-truth-vs-narrative
# Ground-Truth Numeric Memory vs Narrative Markdown: SQLite-vault design for the trading agent's second brain

## What real practice converged on (2024-2026)

Three findings from recent engineering matter here:

**1. The two-tier split is now standard, and the failure mode you fear is documented.** The memory-security literature calls it exactly what you called it: a poisoned or failed experience gets "compressed into lesson memory" during summarization/reflection and re-emerges looking clean — see the survey on long-term memory security ([arxiv.org/html/2604.16548v1](https://arxiv.org/html/2604.16548v1)), MINJA-style self-reinforcing corrupted precedents ([arxiv.org/html/2601.05504v2](https://arxiv.org/html/2601.05504v2)), and MemEvoBench on "memory misevolution" ([arxiv.org/pdf/2604.15774](https://arxiv.org/pdf/2604.15774)). The AI-scientist world independently hit the same wall: "Dead Science Walking" ([arxiv.org/pdf/2606.04220](https://arxiv.org/pdf/2606.04220)) names **"confident rediscovery"** — a system that retrieves only positive summaries re-proposes known-falsified hypotheses. That is memory laundering by another name. The countermeasure paper, "Negative Knowledge as Failure-aware Shared Memory for AutoResearch" ([arxiv.org/pdf/2606.21024](https://arxiv.org/pdf/2606.21024)), proposes storing failures as **durable, structured constraints on the search space**, not prose. This is the closest published analogue to your negative-results DB and validates the design.

**2. Markdown-only memory doesn't scale; SQLite-hybrid is the pragmatic middle.** Practitioners who started with flat markdown (memweave: [towardsdatascience.com/memweave-zero-infra-ai-agent-memory-with-markdown-and-sqlite-no-vector-database-required/](https://towardsdatascience.com/memweave-zero-infra-ai-agent-memory-with-markdown-and-sqlite-no-vector-database-required/), sqlite-memory: [github.com/sqliteai/sqlite-memory](https://github.com/sqliteai/sqlite-memory)) added SQLite FTS5/vector indexing once retrieval broke. The hosted platforms (Mem0/Zep/Letta comparison: [particula.tech/blog/agent-memory-frameworks-tested-mem0-zep-letta-cognee-2026](https://particula.tech/blog/agent-memory-frameworks-tested-mem0-zep-letta-cognee-2026)) all let the LLM write memory — which is precisely wrong for your ground-truth tier. Honest call: **Mem0/Zep/Letta are built for conversational personalization; none enforce deterministic-writer semantics. Don't adopt them for the vault.** Letta's editable memory blocks are the anti-pattern here.

**3. Read-only enforcement is a solved, boring problem.** Open the DB with `SQLITE_OPEN_READONLY` for the LLM-facing connection (read-only MCP fork: [lobehub.com/mcp/fanom2813-mcp-sqlite-readonly](https://lobehub.com/mcp/fanom2813-mcp-sqlite-readonly); SELECT-only validated FastMCP server: [github.com/hannesrudolph/sqlite-explorer-fastmcp-mcp-server](https://github.com/hannesrudolph/sqlite-explorer-fastmcp-mcp-server)), plus belt-and-suspenders triggers `BEFORE UPDATE/DELETE ... RAISE(ABORT)` on vault tables ([sqlite.org/lang_createtrigger.html](https://www.sqlite.org/lang_createtrigger.html)), plus hash-chaining rows for tamper evidence (event-sourcing/audit pattern: [learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing](https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing), audit-trail framework for LLMs: [arxiv.org/html/2601.20727v1](https://arxiv.org/html/2601.20727v1)).

## Concrete design for the trading agent

**Single file `brain.db`, three write paths, one reader.** Only Python pipeline code (deep_validation, forward_test, mech_sizing) holds a read-write connection. Claude Code/Codex get a read-only MCP connection + generated markdown digests.

```sql
CREATE TABLE trials (            -- every method ever tested, pass or fail
  trial_id INTEGER PRIMARY KEY,
  method_dsl TEXT NOT NULL,       -- canonical DSL, normalized
  novelty_hash TEXT NOT NULL,     -- sha256 of canonicalized DSL AST
  param_fingerprint TEXT,         -- coarse-bucketed params (catches near-dupes)
  universe TEXT, period_start TEXT, period_end TEXT,
  n_trades INTEGER, is_sharpe REAL, oos_sharpe REAL,
  bootstrap_pvalue REAL,          -- block-bootstrap
  lockbox_outcome TEXT CHECK(lockbox_outcome IN ('pass','fail','not_run')),
  failure_mode TEXT,              -- enum: 'overfit','regime','costs','capacity','no_signal'
  n_trials_family INTEGER,        -- for deflated-Sharpe accounting
  prev_row_hash TEXT, row_hash TEXT,  -- hash chain
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TRIGGER trials_ro BEFORE UPDATE ON trials
  BEGIN SELECT RAISE(ABORT,'append-only'); END;
-- same for DELETE; plus: validated_methods, trade_lessons tables
```

`n_trials_family` is what makes this more than a log: Bailey & López de Prado's Deflated Sharpe Ratio requires the **number of trials attempted**, "the most important piece of information missing from virtually all backtests" ([papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551), overview: [en.wikipedia.org/wiki/Deflated_Sharpe_ratio](https://en.wikipedia.org/wiki/Deflated_Sharpe_ratio)). Your negative-results DB *is* the trial registry that makes DSR computable honestly — that alone justifies SQLite over markdown, because markdown can't be `COUNT(*)`'d reliably.

**Trade lessons as numbers:** `trade_lessons(trade_id, symbol, side, entry_ts, r_multiple, mae_bps, mfe_bps, slippage_bps, gate_flags_at_entry, regime_tag, rule_violated)`. Reflexion ([arxiv.org/abs/2303.11366](https://arxiv.org/abs/2303.11366)) showed verbal lessons work, but your MEMORY.md history (the falling-knife "gate firing ≥$50M liquid" lesson) shows the durable part was the *number*, not the story. Store the number in SQL; regenerate the sentence.

**Data flow (event → store → retrieval → decision):**

1. deep_validation finishes a method → Python computes canonical DSL hash, appends `trials` row with p-value/lockbox outcome, extends hash chain. LLM never touches this.
2. A nightly deterministic job (cron, not LLM) renders `LESSONS.md` and `DEAD_IDEAS.md` **from SQL queries** — e.g., top failure modes by count, per-regime R-multiple stats. Markdown is a build artifact, disposable and regenerable; the DB is the source of truth. (This inverts sqlite-memory's "markdown is source of truth" — correct inversion for ground truth.)
3. When the LLM proposer drafts a method, a **pre-registration gate** (Python) canonicalizes the DSL, computes novelty_hash, and runs: exact-hash lookup → param-fingerprint lookup → FTS5/embedding similarity over `method_dsl` with threshold. Hit on a `lockbox_outcome='fail'` row → auto-reject with the stored failure_mode quoted back verbatim into the proposer's context. The LLM cannot argue with a row it cannot edit.
4. At trade-decision time, futures_watch queries `trade_lessons` for matching (regime_tag, setup_family) and injects the *aggregate numbers* (win rate, median MAE, gate-violation correlations) into the prompt — retrieval is SQL, not vector-vibes, because the keys are structural.

**Hype-vs-real:** Temporal knowledge graphs (Zep/Graphiti) are real but overkill here — your facts are already relational and timestamped. Blockchain-grade immutability is theater for a single-operator system; a SHA-256 hash chain in a column plus `SQLITE_OPEN_READONLY` gives you 95% of tamper evidence for 0.1% of the complexity. Embedding-based novelty detection is genuinely unsolved — a paraphrased dead idea can slip a cosine threshold, which is why the canonical-DSL hash (deterministic parse, sorted params, bucketed constants) must be the primary key of "have we tried this," with embeddings only as a fuzzy second net. Expect and accept some false negatives there; the append-only trial log means a re-test is wasted compute, not corrupted truth.

**Migration for the existing stack:** keep MEMORY.md for preferences and workflow norms (its actual strength); move every number currently living in session-log markdown (R-multiples, p-values, "ALGO +$0.0445") into `trade_lessons`/`trials`; expose `brain.db` to both Claude Code and Codex via one shared read-only MCP server so both proposers see the same dead-ideas wall.

Sources: [arxiv.org/pdf/2606.21024](https://arxiv.org/pdf/2606.21024), [arxiv.org/pdf/2606.04220](https://arxiv.org/pdf/2606.04220), [arxiv.org/html/2604.16548v1](https://arxiv.org/html/2604.16548v1), [arxiv.org/html/2601.05504v2](https://arxiv.org/html/2601.05504v2), [arxiv.org/pdf/2604.15774](https://arxiv.org/pdf/2604.15774), [papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551), [en.wikipedia.org/wiki/Deflated_Sharpe_ratio](https://en.wikipedia.org/wiki/Deflated_Sharpe_ratio), [arxiv.org/abs/2303.11366](https://arxiv.org/abs/2303.11366), [arxiv.org/abs/2311.13743](https://arxiv.org/abs/2311.13743) (FinMem layered memory — narrative-tier prior art), [github.com/hannesrudolph/sqlite-explorer-fastmcp-mcp-server](https://github.com/hannesrudolph/sqlite-explorer-fastmcp-mcp-server), [lobehub.com/mcp/fanom2813-mcp-sqlite-readonly](https://lobehub.com/mcp/fanom2813-mcp-sqlite-readonly), [sqlite.org/lang_createtrigger.html](https://www.sqlite.org/lang_createtrigger.html), [github.com/sqliteai/sqlite-memory](https://github.com/sqliteai/sqlite-memory), [towardsdatascience.com/memweave-zero-infra-ai-agent-memory-with-markdown-and-sqlite-no-vector-database-required/](https://towardsdatascience.com/memweave-zero-infra-ai-agent-memory-with-markdown-and-sqlite-no-vector-database-required/), [particula.tech/blog/agent-memory-frameworks-tested-mem0-zep-letta-cognee-2026](https://particula.tech/blog/agent-memory-frameworks-tested-mem0-zep-letta-cognee-2026), [learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing](https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing), [arxiv.org/html/2601.20727v1](https://arxiv.org/html/2601.20727v1)

---

## eval-metrics
# Evaluating the Second Brain: Memory Metrics for a Quant Trading Agent

## 1. What the public benchmarks actually measure — and their limits

**LongMemEval** (https://arxiv.org/abs/2410.10813) is the most useful public template: 500 questions over multi-session histories testing five abilities — information extraction, multi-session reasoning, temporal reasoning, **knowledge updates**, and **abstention**. The last two map directly onto trading: "knowledge update" = a method that was validated then died in forward test; "abstention" = the memory should say "no prior evidence" rather than hallucinate a lesson. LongMemEval-V2 (https://arxiv.org/html/2605.12493v1) extends this toward agents accumulating job experience. **LoCoMo** (https://arxiv.org/abs/2402.17753) tests QA over ~300-turn multi-session dialogs.

**Honest call:** vendor LoCoMo scores are marketing. Mem0 claimed Zep scored 84%, Zep's re-run says Mem0's number reproduces at ~58% (https://blog.getzep.com/lies-damn-lies-statistics-is-mem0-really-sota-in-agent-memory/, https://github.com/getzep/zep-papers/issues/5), and in Mem0's own paper (https://arxiv.org/abs/2504.19413) a **full-context baseline (~73 J) beat their memory system (~68 J)**. Lesson: at your scale (thousands of records, not millions of tokens), a fancy memory layer can *lose* to just loading the whole negative-DB summary into context. Benchmark yourself against that baseline before buying architecture. MemoryAgentBench (https://github.com/HUST-AI-HYZ/MemoryAgentBench, ICLR 2026) is the cleanest open harness pattern: incremental multi-turn ingestion, then probe queries — copy its structure, not its data.

## 2. The metrics that matter for this system

Standard retrieval metrics — context precision/recall, recall@k, MRR (https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/) — apply, but you must build a **gold set from your own history**: for each past decision point, label which memory records *should* have been retrieved. Then add four domain-specific metrics no benchmark gives you:

1. **Dead-idea re-test rate (the killer metric):** of N methods the LLM proposer submits per week, how many collide with the negative-results DB? Target: exact-hash collisions = 0 (hard-blocked), semantic near-dup proposals < 5% and falling. This is measurable deterministically — no LLM judge needed.
2. **Lesson-recall-at-decision:** when a trade/validation decision is made in a regime matching a stored loss-lesson's trigger condition, was that lesson in the retrieved context? (Recall over event-triggered gold labels.)
3. **Abstention correctness:** when queried about a genuinely novel setup, does retrieval return "no match" instead of a spurious neighbor? (LongMemEval's abstention category, repurposed.)
4. **Memory-attributable outcome delta:** run the proposer in two shadow arms — memory-on vs memory-off — and compare wasted validation-compute and forward-test PnL. Controlled ablations of exactly this kind are now standard practice (https://medium.com/@mrsandelin/the-first-controlled-benchmark-of-ai-memory-in-coding-agents-8e0bb776d39e); MemoryArena-style ablations show memory deltas often exceed model-choice deltas.

Bonus: the negative-DB **is** your trial count N for the Deflated Sharpe Ratio (https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551). Every stored dead method makes your significance threshold for the *next* method honest. That alone justifies the database.

## 3. Data flow and schemas

```
EVENT (deep_validation completes / trade closes)
  → deterministic writer (Python, NOT LLM) appends immutable JSONL record
  → indexer: exact hash + embedding index rebuild
  → RETRIEVAL at decision time:
      proposer emits candidate DSL → canonicalize → novelty gate
      trade signal fires → regime-keyed lesson lookup → injected into prompt
  → DECISION: proposal blocked/allowed; sizing adjusted; all reads logged
```

Negative-results record (append-only `negatives.jsonl`):

```json
{"id": "neg_0417", "dsl_canonical": "...", "novelty_hash": "sha256(canonical_ast)",
 "embedding": [...], "universe": ["BTC","ETH",...], "period": ["2021-01","2025-12"],
 "bootstrap_p": 0.31, "oos_sharpe": -0.2, "lockbox_sharpe": null,
 "failure_mode": "decays_after_2023|fee_sensitive|regime_dependent",
 "killed_at_stage": "deep_validation", "git_commit": "abc123", "ts": "..."}
```

Novelty gate (deterministic, three layers):

```python
def novelty_gate(dsl):
    ast = canonicalize(dsl)            # sort commutative ops, normalize params to buckets
    if sha256(ast) in exact_index:      return BLOCK("exact re-test")
    sims = embed_topk(ast, k=5)
    hits = [s for s in sims if s.cos > 0.92]
    if hits:                            return FLAG(hits)  # LLM must WRITE a differentiation
                                                            # statement; a human/rule checks it
    return ALLOW
```

Loss lessons as numbers, keyed by machine-checkable triggers: `{"trigger": {"funding_rate": ">0.05%", "ch24": ">12%"}, "stat": {"n": 9, "win_rate": 0.22, "avg_pnl_R": -0.71}}` — retrieval is then a rule-match plus embedding fallback, and "did the lesson fire" is auditable.

## 4. Read/write boundary — your hard constraint is the literature's conclusion

Your "memory laundering" fear is a documented attack class: MINJA-style memory poisoning corrupts agent behavior through ordinary interactions with >70% effectiveness (https://arxiv.org/pdf/2601.05504), MemoryGraft persists compromise via poisoned experience retrieval (https://arxiv.org/html/2512.16962v1), and origin-bound write authority is the proposed fix (https://arxiv.org/pdf/2606.24322). Concretely: **ground-truth stores (negatives, lockbox winners, trade ledger) are written only by pipeline code from pipeline outputs; the LLM gets read-only MCP tools** (`query_negatives`, `query_lessons`, `check_novelty`). The LLM may *propose* a prose annotation, but it lands in a separate `annotations/` layer that never feeds the novelty gate. Use bi-temporal fields (valid-time vs recorded-time, per Zep/Graphiti, https://arxiv.org/abs/2501.13956, https://github.com/getzep/graphiti) so "method believed alive until 2026-03, invalidated 2026-05" supersedes without deleting — provenance survives.

## 5. Hype vs real, applied

Real: negative-DB with deterministic novelty gating (pure engineering, measurable, ties into DSR); event-triggered numeric lessons; ablation-based eval; append-only provenance. Hype for your scale: graph memory products, LLM-summarized "reflections" as ground truth (Reflexion, https://arxiv.org/abs/2303.11366, works in benchmarks but is exactly the laundering vector you banned), and any vendor LoCoMo number. Keep MEMORY.md as the human-readable *view* generated from JSONL — never the source of truth. Your eval harness is ~200 lines: replay history, score the four metrics weekly, and treat a rising re-test rate as a P0 bug.

Sources: https://arxiv.org/abs/2410.10813, https://arxiv.org/html/2605.12493v1, https://arxiv.org/abs/2402.17753, https://arxiv.org/abs/2504.19413, https://blog.getzep.com/lies-damn-lies-statistics-is-mem0-really-sota-in-agent-memory/, https://github.com/getzep/zep-papers/issues/5, https://github.com/HUST-AI-HYZ/MemoryAgentBench, https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/, https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551, https://arxiv.org/pdf/2601.05504, https://arxiv.org/html/2512.16962v1, https://arxiv.org/pdf/2606.24322, https://arxiv.org/abs/2501.13956, https://arxiv.org/abs/2303.11366, https://aws.amazon.com/blogs/machine-learning/build-agents-to-learn-from-experiences-using-amazon-bedrock-agentcore-episodic-memory/, https://medium.com/@mrsandelin/the-first-controlled-benchmark-of-ai-memory-in-coding-agents-8e0bb776d39e