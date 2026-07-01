---
title: "Claude Takeover — Edge-First Rebuild"
description: "Takeover of the autonomous paper trading agent from Codex. Stop building scaffolding; prove (or kill) a real, cost-adjusted edge on honest data before anything else. Paper-only until a statistically significant edge survives a frozen holdout."
status: pending
priority: P0
owner: claude
created: 2026-07-01
supersedes_focus_of:
  - 260628-2343-neurocore-agent-nervous-system
  - 260630-1921-chart-intelligence-v1
  - 260621-1650-autonomous-paper-learning-masterplan
tags: [trading-agent, edge-discovery, paper-only, takeover, safety, data-honesty]
evidence_base: "19-agent read-only audit + 3-lens adversarial red-team (wf_ec62bb84-0cf), 2026-07-01"
---

# Claude Takeover — Edge-First Rebuild

## 0. One-paragraph truth

Codex built an enormous, well-engineered **cognitive scaffold** (NeuroCore: ~50 LLM/learning agents, chart engine, dream/belief/self-model, backtest + walk-forward + promotion machinery, Vietnamese dashboard, 280+ files) wrapped around **one or two hardcoded scalp signals fed synthetic/stale data**, and it has **no demonstrated edge**. Paper is down ~20% ($80/$100). The decision path runs on **fabricated features** (3 synthetic candles built from a single 24h ticker row) and **resolves exits against single-point mark candles** (open=high=low=close), so SL/TP wicks are invisible and ~54% of trades die on a blind 30-minute timeout near breakeven-minus-fees. Every subsystem that could *produce* edge (chart scorer, backtest harness, memory recall, real scoring board) is **dead code or disconnected from the money path**. The system even diagnoses its own lack of edge (`walk_forward_latest.json`: expectancy_after_fees ≈ -0.10, profit_factor ≈ 0.85) and then parks the conclusion. **The job is not to add features. It is to get honest data, prove or kill an edge offline, and refuse live capital until that passes.**

This plan is the basis for moving development from Codex to Claude. Live trading stays **disabled** throughout (`can_place_live_orders=false`).

## 1. What the audit established (verified facts)

| # | Fact | Evidence |
|---|---|---|
| F1 | Decision features are **fabricated** | `paper_candidate_feeder.py:65-104` builds 3 synthetic candles from one 24h ticker; `is_synthetic_chart_proxy=True`, `chart_decision_eligible=False` |
| F2 | Microstructure feeds are **static stale fixtures** | `state/derivatives_latest.json` OI=110, BTC@$100, frozen; SLA 30/600/300s violated |
| F3 | Exits resolve on **single-point mark candles** | `paper_execution_lifecycle_loop.py:594-610` `mark_candle` open=high=low=close, `quality='mark_only_snapshot'` |
| F4 | **~54% of all closes are timeouts** (30-min blind) | full ledger `state/agent_memory/paper_trades.jsonl` (946 closes): timeout 509, sl 219, tp 218; `MAX_HOLD_SECONDS=1800` |
| F5 | Realized loss is **real strategy loss**, not bookkeeping | reconciler ties out: 44W avg +0.66 / 69L avg -0.71 = -19.95; fees modeled (taker 0.0005 both legs) |
| F6 | **Fees ≈ 33% of loss** | `paper_account.json` fees_paid 6.60 / realized -19.95; entries hardcoded `order_type='market'` |
| F7 | Costs/exits bias paper **UPWARD** → live would lose *faster* | flat 2bps slippage, no spread/depth, timeout exits log **zero** slippage, single-mark fills |
| F8 | Edge-producing subsystems are **dead/disconnected** | `chart_used=true` on **0** trades; `memory_retrieval.db` never built; `backtest_harness` 0 non-test callers; `real_scoring_board` never runs (`real_scoring_missing`) |
| F9 | Validation **cannot reach significance** | `walk_forward_latest.json`: holdout test trades=6 vs min 20 → `insufficient_future_trades` |
| F10 | Operational substrate **cannot run unattended** | supervisor + dashboard dead, port 8090 down; no Windows autostart; `llm_reasoning_agent` quarantined by heartbeat-window config bug |
| F11 | Promotion gates measure **process, not alpha** | `promotion_board.py:26-32` requires counts/days/exam-80, **no** expectancy CI / drawdown / significance |
| F12 | The real live-money surface is the **~100 manual scripts** | `execute_*.py`, `monitor_*.py`, `cleanup_algo*.py` route through `bf.open_long()`; firewall guards the paper brain (which can't trade live) but these use weaker filename/regex guard |

## 2. Red-team corrections folded in (do NOT repeat the prior framing)

The diagnosis was adversarially reviewed (quant-rigor, execution-realism, scope). Corrections that this plan obeys:

1. **Drop "negative EV by construction = -0.20R."** It is invalid: <20% of trades resolve at planned ±R, the 39% rate counts timeout-in-profit as wins, and SL/timeout are indistinguishable in the ledger. The honest statement is: **expectancy is currently UNESTIMABLE** — 3+ inconsistent trade populations, **172 account resets** (`state/agent_state.db`), gross-PnL sign flips by window, unreliable win/loss labels. We do not size any confidence off the current numbers.
2. **Kill the "100% LONG monoculture" framing.** The full ledger (946 closes) is ~50/50 funding_squeeze/exhaustion_fade and 68/32 LONG/SHORT. The 113-trade "monoculture" was just the latest post-reset window.
3. **`chart_setup_scorer` is NOT callerless** — it is imported and invoked (`paper_candidate_feeder.py:128/153/174`) but masked to zero influence via `chart_decision_eligible=False`. Do not blind-delete it (breaks the trade-record schema); neutralize by fixing data eligibility, not by ripping it out.
4. **The PnL-resolving executor is `paper_execution_lifecycle_loop.py` + `paper_candidate_feeder.py`, not `autonomous_paper_trading_brain.py`** (the brain mostly computes leverage). "Consolidate to one executor" targets the lifecycle loop.
5. **The most probable correct outcome of edge-discovery is "kill the strategy," not "improve it."** This plan has an explicit **KILL CRITERION** (§5). Honest data will likely make paper look *worse* first — that is success, not failure.
6. **Do not prescribe new scaffolding alongside "nothing matters until edge exists."** Live microstructure adapters, autostart service, and re-integrating the learning fleet are **deferred until after an edge survives holdout** (Phase 5+).

## 3. Non-negotiables

- **Paper/shadow only.** `can_place_live_orders=false` stays everywhere. No live order this entire plan.
- **No new feature/agent is built until an edge is proven offline.** Edge-discovery is the only authorized work in Phases 0-3.
- **Honest data or no decision.** No synthetic-proxy candle, no stale fixture may feed a decision feature. If real data is missing, the candidate is rejected — not faked.
- **Frozen holdout the ranker has never seen.** Pre-register holdout window + size before looking at results. No peeking, no re-fitting on holdout.
- **Realistic costs.** Taker-both-legs fees, explicit half-spread, depth/volatility-scaled slippage with a microcap floor far above 2bps, true intrabar OHLC exits, tiered MMR liquidation. Costs must make paper look *worse*, not better.
- **Multiple-testing discipline.** Searching many setups/params requires deflated-Sharpe / FDR / Bonferroni at the *search* level, not just per-candidate CI.
- **Kill criterion is binding** (§5). If no setup clears it, we stop trading and stop building — we do not loop into more features.

## 4. Phases (edge-first ordering)

> Each phase: implement → test → loop-audit until no BLOCKER/MAJOR → only then next phase. Tests must be fast (see Phase 0). Commit per phase.

### Phase 0 — Make iteration possible (≤1 day)
**Goal: a fast, trustworthy test run and a clean baseline.** Nothing else can be trusted until this works.
- Fix the full-suite **pytest timeout >129s / no output**: find hanging tests (network calls, `time.sleep`, import side-effects, missing fixtures), mark/skip/iso­late, target a green suite in <60s.
- Snapshot current state (paper account, ledger, walk-forward) into `plans/.../reports/baseline.md` so we can measure change.
- Verify `can_place_live_orders=false` is enforced at the **order wrapper** (`tradingagents.binance.futures.create_order/open_long`), fail-closed, not by filename regex. (This is a safety fix, allowed pre-edge.)
- **Exit gate:** `pytest` green & fast; baseline report written; live firewall fails closed on a smoke test.

### Phase 1 — Honest historical data (the unlock) (1-2 days)
**Goal: real point-in-time OHLCV so an edge can even be measured.** This single fix unblocks honest features, intrabar exits, and backtest simultaneously.
- Wire real multi-timeframe **closed** OHLCV klines (use existing `chart_candle_service.py`) into (a) feature computation and (b) the position replay/exit buffer — replacing synthetic 3-candle proxy (F1) and mark-only snapshots (F3).
- Enforce point-in-time: no forming candle, no future leakage; every feature row carries `decision_cutoff` + `cutoff_proof` (the Chart Intelligence contracts already define this — reuse, don't rebuild).
- **Exit gate:** a replayed historical window reproduces intrabar SL/TP touches (not 54% timeouts); zero synthetic-proxy rows reach a decision feature; lookahead test passes.

### Phase 2 — Honest cost & exit model (1 day)
**Goal: the simulator stops flattering us.** Expect edge to look worse — that is correct.
- Intrabar SL/TP/liq against true OHLC (Phase 1 data); model explicit bid/ask half-spread; slippage = f(depth, volatility) with a **microcap floor** ≫ 2bps; taker-both-legs unless a *realistic* maker-fill model exists; tiered Binance MMR for liquidation.
- **Do not** recommend maker/limit entries until maker-fill realism (queue position, adverse selection) is modeled — otherwise the cure adds fresh optimism (red-team #2).
- **Exit gate:** re-running the existing 946-close ledger through the new model produces a *more negative* or equal PnL; cost components itemized per trade.

### Phase 3 — Prove or kill an edge offline (the crux) (2-4 days)
**Goal: ONE primitive signal with statistically significant, cost-adjusted positive expectancy on a frozen holdout.** Until this exists, nothing else matters.
- Turn on the dead validation tooling: run `backtest_harness` / walk-forward over candidate setups on **historical PIT candles** with Phase-2 costs.
- **Pre-register**: holdout window, minimum N (power analysis for detecting ~+0.1R vs R-noise → typically hundreds–thousands of trades/candidate), and the search count (for multiple-testing correction).
- Candidates to test first (cheapest, already coded): `funding_squeeze`, `exhaustion_fade` — and note `exhaustion_fade` is the **banned counter-momentum fade** (`feedback_no_counter_momentum`) and the worst-PnL setup; expect to retire it.
- **Gate to pass:** out-of-sample, cost-adjusted expectancy with **lower 95% CI bound > 0**, profit_factor > ~1.2, AND survives deflated-Sharpe/FDR for the number of candidates searched, AND a max-drawdown ceiling.
- **Exit gate:** either a setup passes the gate (→ Phase 4), or none does (→ §5 KILL).

### Phase 4 — Wire the proven edge into the executor (only if Phase 3 passes) (1-2 days)
- Route ONLY the validated setup through `paper_execution_lifecycle_loop.py`; decouple leverage from stop-tightness; hard-cap leverage low (3-5x) and per-trade risk 0.5-1%; match hold time to the thesis (kill blind 30-min timeout); cut frequency so fees < 10% of expected win.
- Implement & run `real_scoring_board` as the **non-bypassable primary promotion gate** (expectancy CI lower bound > 0 + drawdown ceiling + significance). Demote LLM self-exam to advisory.
- **Exit gate:** forward paper run reproduces the holdout edge within CI on fresh unseen data.

### Phase 5 — Operational hardening & selective re-integration (only after Phase 4) (1-2 days)
- Real Windows autostart (Task Scheduler) so the supervisor is itself supervised; fix the `llm_reasoning_agent` heartbeat-window bug; un-terminal the quarantine.
- Build **live** microstructure adapters (depth/funding/OI/liquidations) to replace fixtures — now justified because an edge exists to feed.
- Re-integrate learning fleet **only** where it measurably tunes a live parameter; otherwise leave shelved.
- Governance for the **~100 manual scripts** (the real live-money surface): route them through the same fail-closed order wrapper.

### Phase 6 — Tiny live, gated (only after 4-5 hold) (open-ended)
- Only after the promotion gate holds on out-of-sample forward paper: risk *tiny* real capital with kill-switch + drawdown ceiling. Scale only with continued evidence.

## 5. KILL CRITERION (binding)

If, after Phase 3 with honest data + realistic costs, **no setup** clears: out-of-sample cost-adjusted expectancy lower-95%-CI > 0, PF > 1.2, surviving multiple-testing correction, within drawdown ceiling — across the pre-registered candidate set — then:
- **Stop trading. Stop building features.** Do not loop into another NeuroCore-style scaffold.
- Report honestly that this universe/strategy family has no demonstrable edge for a small account, and present the realistic options (different market/timeframe, market-making, or not trading).

## 6. Theater to SHELVE during edge-discovery (do not re-integrate until §5 passes)

Per audit + red-team, these consume effort/MB and touch **zero** trade decisions today. Shelve (don't delete blindly — some are schema-wired):
- Learning fleet: `dream_cycle` (DREAMS.md 908K, dream_journal 2.2M), `belief_ledger`, `self_model`, `skill_forge`, `daily_exam`, `memory_consolidation`, `self_improvement`, `hypothesis_engine` (frozen 11 days).
- `memory_retrieval` active-recall (DB never built in prod).
- The ~50 LLM agent files / "8-analyst debate" narrative (trade path is hardcoded thresholds).
- Re-integrating Chart Intelligence beyond providing the **OHLCV feed** Phase 1 needs.
- Rewrite the **stale README** (still describes a Base-chain meme bot that doesn't exist) — low effort, high honesty.

## 7. Definition of done for this plan

- Phase 0-2 complete: fast green tests, honest historical data feeding decisions + exits, simulator that no longer flatters.
- Phase 3 resolved: either a setup **passes** the pre-registered edge gate (with the artifacts to prove it) **or** the KILL criterion is invoked and reported.
- No live capital risked unless Phase 4-5 gates hold on out-of-sample forward paper.
- Every claim of "done" backed by a runnable test or a measured number, not a narrative.

## 8. Repo layout note (decided 2026-07-01)

`tradingagents_crypto_src/` is a **separate nested git repo** (118 core files:
binance client/futures, LLM clients, data adapters) that the main repo ignores
(`.gitignore:36`). Everything in the main repo imports from it (`from tradingagents...`).
Decision: **leave it out of the public repo** (owner: "bỏ qua"). Consequences:
- The live-order guard (`live_guard.py` + guarded `client.py`/`futures.py`) lives
  there, so it is enforced at **runtime on the local machine** (verified) but is
  **not** in the public GitHub repo.
- The public repo therefore is not independently runnable; Claude-web sees the
  orchestration layer but not the binance/LLM/data core. Acceptable per owner.
- `tests/test_live_guard.py` is committed publicly but imports the local-only
  module; it passes locally, would error on a bare public clone. Non-blocking.
- `plans/**/reports/` stays gitignored (baseline + audit artifacts are local).

## 9. Phase 0 result (2026-07-01)

DONE. Gates: pytest fixed (821 pass ~17s, was >129s hang); the one real test
failure fixed (time-dependent, not masked); **fail-closed live guard at the
client chokepoint** — blocks the typed wrappers AND the ~100 manual scripts
(both verified: direct `spot_client().futures_create_order` raises
`LiveOrdersBlocked` without `ALLOW_LIVE_ORDERS`); baseline measured. Adversarial
audit passed after fixing the manual-script bypass it found. Live trading remains
disabled.

## 10. Phase 1 result (2026-07-01)

DONE (3 pieces, adversarially audited, live disabled throughout):
- **A. Ingestor** (`chart_candle_ingestor.py`): fetches+stores real Binance 5m
  klines for the symbols about to be scored; bounded, fail-closed.
- **B. Feature path** (`paper_candidate_feeder.py`): decision features now come
  from real closed 5m OHLCV via `load_closed_candles` at the snapshot cutoff.
  Missing data -> skip (reject-not-fake). Synthetic proxy has no non-test caller.
- **C. Exit path** (`paper_execution_lifecycle_loop.py`): `should_close` resolves
  SL/TP against real intrabar OHLC (fixes the ~54% blind-timeout problem);
  fail-open to the mark candle; 30-min timeout retained as safety net.

Audit (Phase 1.7) found and this session FIXED:
- **M1 (MAJOR):** `build_cutoff_proof` gated on `ingested_at` (operational
  write-time ~now) which starved the decision path against an older cutoff;
  green tests hid it. Fixed in both copies — lookahead is now defined only by
  data-existence timestamps (available_at/known_at/finalized_at). Regression
  test added with `ingested_after_cutoff=True`.
- **m5 (MINOR):** exit bars now require `open_time >= opened_at` so a bar
  spanning entry can't fire on a pre-entry wick.
- No lookahead leak confirmed. Full suite 827 passed.

DEFERRED (tracked, not blocking):
- **M2:** inline ingest does blocking network on the feeder hot path (bounded,
  fail-closed, but no timeout; a test does real I/O unless
  `INGEST_DECISION_CANDLES=0`). Move to a supervised ingest loop in **Phase 5**.
- **m4/m6/m7:** stamp exit records with real-vs-mark source; `chart_candle_cache`
  provider="local" isn't recency-flagged; close_ts uses mark ts. Address when
  hardening execution realism (**Phase 2**).

## 11. Next: Phase 2 — honest cost & exit model

Intrabar SL/TP/liq against real OHLC (done in Piece C for touch detection; now
add) explicit bid/ask half-spread, depth/volatility-scaled slippage with a
microcap floor >> 2bps, taker-both-legs unless a realistic maker-fill model
exists, tiered Binance MMR for liquidation. Expect edge to look WORSE — correct.
Also fold in m4/m6/m7 exit-honesty stamping here.
