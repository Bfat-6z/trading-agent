---
tags: [research, architecture]
---

# Codebase Recon (5-agent, pre-build)

# Second Brain — Build Readiness Map (5-agent codebase recon, 2026-07-06)

## 🔑 HEADLINE REFRAME
The second brain is **~70% already built** as the disconnected "NeuroCore" (System B, Codex-authored) — a deterministic, taint-gated SQLite memory system. It just **never connects to the live trader** and has JSONL sprawl. So the job is **CONSOLIDATE + WIRE + ADD the missing trials/DSR registry + KILL the one laundering loop** — NOT a greenfield rebuild. The `plans/second_brain_design.md` (my 15-agent blueprint) was written without knowing System B existed; System B actually matches its philosophy (custom SQLite + FTS5 + evidence gate).

There are TWO disjoint memory systems (no shared code/data):
- **System A** = the live trader's hot path (`llm_trader.py` + `llm_trader_memory.py`, `state/llm_trader/*.jsonl`). The ONLY memory that reaches the decision.
- **System B** = NeuroCore (`memory_retrieval.py` FTS5, `memory_consolidation_agent.py` gated promotion, `belief_ledger.py`, `dont_do_memory.py`, `data_trust.py` taint firewall, `event_store.py` agent_state.db 417MB). Feeds `autonomous_paper_trading_brain.py`/`inner_critic.py` — **never `llm_trader.py`**.

## ⚠️ #1 IMMEDIATE HONEST FIX — kill the laundering loop
`llm_trader.py:_reflect()` (L386) → writes free-text LLM self-directives to `state/llm_trader/self_reflection.json` (L436, LLM-mediated, NO gate) → `_reflection_block()` (L455) injects them back into the decision prompt (L867/939) as "follow your own conclusions." This is a textbook memory-laundering loop (model writes its own authority, no evidence/decay/taint check). RETIRE from the write path first. `mistake_lessons()` already produces the deterministic P&L-derived version.

## KEEP / REUSE / RETIRE
| Component | LLM-write? | Verdict |
|---|---|---|
| `closed.jsonl` (real P&L ledger) | No | **KEEP** → the `trades` source of truth |
| `llm_trader_memory.py` (deterministic distiller) | pure | **REUSE** — crown jewel; repoint input to SQLite |
| `self_reflection.json` + `_reflect()` | **YES, ungated** | **RETIRE** (top priority) |
| `memory.jsonl` (dup of closed) | No | **RETIRE** (dead reader) |
| `survivors.json` / `armed_methods.json` | No | **KEEP** |
| `data_trust.py` (taint/evidence gate) | No | **REUSE** — the laundering firewall already exists |
| `memory_retrieval.py` (FTS5, time-safe recall) | No (ETL) | **REUSE** as THE read layer; **WIRE into llm_trader** |
| `memory_consolidation_agent.py` (gated promotion) | No (gated) | **REUSE gate**, collapse JSONL → tables |
| `belief_ledger.py` / `dont_do_memory.py` | text proposed, confidence gated | **KEEP** mechanics → `beliefs`/`rules` tables |
| `memory_compactor.py` / `reflection_agent.py` dreams / `self_model.py` | No (templates) | **RETIRE/merge** (near-zero signal, off decision path) |
| **trials / DSR registry** (negative-results, novelty hash) | — | **BUILD — genuinely missing** |

## INTEGRATION POINTS (precise, from recon)
**Writer hooks (deterministic, single sites):**
- Trial result → `deep_validation.py:201-231` (each `row`: oos/lockbox/pvalue/verdict/robust).
- Shadow close → `forward_test.py:157-159` (`_append(CLOSED,...)`; **no MAE/MFE yet — add to `resolve_open`**).
- Proposal → `method_lab_runner.py:140`; round → `method_lab.py:344-355` (survivors/killed/ledger).
- Armed method → **no code writer** (manual curation) — add an emit at the arm step.

**Novelty gate (the seed already exists):** `ingest_candidates.py:48-66` `sig()` — widen to include `sl/tp` + check seeds∪pool∪killed; hook at `method_lab_runner.py:138-140` (propose accept) and dedup at `run_lab` top (`method_lab.py:322`). Reuse `atomic_state.canonical_json` + `sha256[:20]` idiom. Categorical feats (`ema_stack`, `dow`, `hour_utc`, `streak*`, `ema4h_*`) exact-never-bucket. 27 feats / 5 ops. Live pool: 0 exact dupes, 4 threshold-twin groups = bucket calibration target.

**Lesson gate (evaluator already exists):** reuse `method_lab.method_fires`/`_cond_ok` over an `active`-lessons table (a lesson = structured `when` + `block_side`). Hook: `_mechanical_decisions` before `size_fires` (llm_trader.py:~587) for live; mirror `_validate_decisions:776-797` (`gate_block_chase`/`gate_block_low_vol` template) for discretionary. Live `$50M` floor already = one such gate (`_hot_universe:1372`). Namespace: no `ch24`; use `ret20`/`dd96_pct`.

**Retrieval wire:** live PROVEN_ONLY path has NO LLM prompt → `memory_retrieval.active_recall_for_decision` (L492) becomes a mechanical veto/downsize per fire (map `decision_delta ∈ {block,tighten}`). Add the **≥20% loss quota** (missing). Discretionary path: extend `memory_context()`/`build_memory_context` (llm_trader_memory.py:321). Latency: local SQLite read ~sub-ms, safe.

**MCP server (greenfield, but boundary pre-spec'd):** no MCP code/config exists; prior spec at `plans/260628-.../phase-22-read-only-mcp-boundary.md` names read-sources + forbidden tools. Register in `agent_process_supervisor.py:specs()` (model on `dashboard`, heartbeat=None) + `agent_runtime_contract.REGISTERED_ARTIFACTS`. Wrap `memory_retrieval.search_memory`/`active_recall_for_decision`, pass every response through `data_trust.prepare_llm_egress`.

## INFRA READINESS
- Python **3.14.4** venv. Present: **pydantic 2.13**, **sqlite_vec 0.1.9**, **aiosqlite**, stdlib **sqlite3 3.50 + FTS5 works**. Reuse `atomic_state.py` (fail-open read/write, atomic tmp-then-replace).
- **Missing only:** `mcp`/`fastmcp` package (need `pip install` OR hand-roll stdio JSON-RPC) — and it must be vetted before install.

## REVISED BUILD ORDER (given ~70% exists)
1. **KILL `_reflect()` laundering loop** (delete write path + `_reflection_block` injection). Immediate, honest, ~0.3 session.
2. **trials / DSR registry + novelty gate** — the genuinely-missing piece: `memory/brain.db` `trials` table + canonical `method_hash` + gate at propose. Starts the DSR trial count. ~1.5 sessions.
3. **Wire `memory_retrieval.active_recall_for_decision` into `_mechanical_decisions`** as veto/downsize + add loss quota. ~1 session.
4. **`closed.jsonl` → `trades` table + repoint `llm_trader_memory` distillers**; collapse System B JSONL sprawl into brain.db tables. ~1.5 sessions.
5. **Read-only MCP server** (vet mcp pkg first) wrapping the read layer, shared Claude+Codex, register in supervisor. ~1.5 sessions.
6. **Retire** compactor/dream/self_model text; nightly renderer for MEMORY-like views. ~0.5 session.

## DO NOT (from blueprint, reaffirmed)
Mem0/Zep/Letta; graph DB; vector-DB product; MLflow/W&B; decay/TTL on the trials table (deleting a dead trial un-deflates every future Sharpe); LLM memory curator; keeping the `_reflect` free-text loop.
