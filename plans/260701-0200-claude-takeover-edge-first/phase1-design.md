# Phase 1 Design — Honest OHLCV into decisions + exits

Status: DRAFT for review before coding. Read-only investigation complete (3 agents, 1.1–1.3).

## What the investigation established

### The real OHLCV source (1.1)
- `chart_candle_service.load_closed_candles(symbol, timeframe, cutoff, limit=200)` → real closed candles, **disk-only**, strong no-lookahead (closed_only, `available_at/known_at/finalized_at`, `cutoff_proof`), fail-closed (missing cache → `quarantined`, `capability_mask.action="skip"`).
- `fetch_binance_futures_candles(symbol, timeframe, limit)` → live Binance REST klines, then `store_candle_batch()` persists to `state/chart/candles/<SYM>/<tf>.jsonl`.
- **Cache is EMPTY today and nothing ingests it.** (verified: 0 jsonl, no store_candle_batch caller)

### The fabrication seam (1.2)
- `paper_candidate_feeder.py:65-104 feature_candles_from_market_row()` back-computes 3 fake candles from one 24h ticker row.
- Injected at `paper_candidate_feeder.py:148` inside `feature_row_for_market_row()`.
- **Critical leak:** the candles are tagged `is_synthetic_chart_proxy=True/chart_decision_eligible=False`, but `normalize_feature_candles` (market_feature_store.py:79-96) **drops those keys**, so the capability mask comes out `normal/size_cap` — fake data passes every gate as if real.
- Real gate (already exists) at `autonomous_paper_trading_brain.py:324-342`: rejects candidate if `capability_mask.action in {skip, shadow_only}` OR `cutoff_proof.ok=False`. **So if data is missing → mask skip → auto-reject. This is exactly the reject-not-fake behavior we want.**

### The exit fabrication seam (1.3)
- `paper_execution_lifecycle_loop.py:594-610 mark_candle()` builds a degenerate point candle (open=high=low=close=mark), one per ~30s tick.
- `paper_execution_simulator.py:135-195 simulate_exit()` IS OHLC-aware (tests low<=sl, high>=tp wick touches) but is **starved** — fed single points, so SL/TP only fire if the lone mark crosses the level → 54% die on the 30-min timeout (`MAX_HOLD_SECONDS=1800`).
- Injection seam: `monitor_open_positions:933-943` where `mark_candle` is built/appended/passed to `should_close`→`simulate_exit`.

## The design (3 pieces, minimal, reversible)

> Guiding rule: **honest data or no decision.** Missing real candles → skip the candidate / keep timeout fallback. Never fabricate. Every change behind a clear seam, revertible.

### Piece A — Candle ingestor (NEW, prerequisite)
A small module that, for the symbols in the current market snapshot, fetches real closed multi-timeframe klines and stores them so `load_closed_candles` has data.
- New file `chart_candle_ingestor.py`: for each snapshot symbol × timeframe set (start with **1 timeframe, e.g. 5m**, `limit=200`), call `fetch_binance_futures_candles` → `store_candle_batch`. Idempotent (dedup by open_time already in service).
- Run it **once inline** before/inside `paper_candidate_feeder.run_once` for the symbols about to be scored (bounded, e.g. top ~15), OR as its own supervised loop later. Phase 1 = inline, simplest, no new daemon.
- Fail-closed: if fetch fails for a symbol, no cache written → that symbol's candidate will `skip` downstream. Correct.

### Piece B — Feature path: real candles at feeder:148
- In `feature_row_for_market_row()`, replace `feature_candles_from_market_row(row, snapshot_ts)` with `load_closed_candles(symbol, tf, cutoff=snapshot_ts, limit=200)["bars"]`.
- Pass real `timeframe` (e.g. "5m") not "ticker_24h_proxy"; drop synthetic `fit_metadata`.
- If `load_closed_candles` returns quarantined/empty (< required bars) → do NOT fabricate; return a feature_row whose `capability_mask.action="skip"` so the brain rejects it. (compute_market_features already raises on <3 candles; catch → skip stub, same as existing except path.)
- Keep `feature_candles_from_market_row` in the file but unused by the decision path (or clearly mark diagnostic-only) to avoid breaking `test_chart_contracts.py:133-141` — update that test to assert the proxy is NO LONGER used for decisions.

### Piece C — Exit path: real intrabar OHLC at lifecycle:933-943
- Before `should_close`, fetch the real closed candles that elapsed since position open via `load_closed_candles(symbol, tf, cutoff=now)` and append the **new** intrabar OHLC bars to `replay_candles` (dedup by ts already exists at append_replay_candle:644-647).
- `simulate_exit` then sees real highs/lows → SL/TP fire intrabar instead of 54% timeouts.
- Keep `mark_candle` as a **fallback only** when real candles are unavailable (stamp quality so honest vs fallback is distinguishable — the field already exists: `mark_sequence` vs `mark_only_snapshot`).
- Do NOT remove the timeout safety net; just let real OHLC resolve SL/TP first.

## Risks & mitigations
| Risk | Mitigation |
|---|---|
| Cache empty → everything skips → paper loop stops trading | Piece A ingests first; verify candidates still flow in a smoke test before declaring done |
| Live REST fetch inside paper loop = network dependency / latency | bounded symbol count; failures fail-closed (skip), never block; consider caching TTL later |
| Lookahead leak via wrong cutoff | always pass `cutoff=snapshot_ts` (features) / position-relative cutoff (exits); rely on service's cutoff_proof; add explicit no-lookahead test |
| Breaking existing contract tests | update `test_chart_contracts.py` + feeder tests intentionally; new tests assert real-data behavior |
| Timeframe mismatch (proxy was "ticker_24h_proxy") | pick one real tf (5m) for Phase 1; multi-tf alignment is a later phase |
| ALLOW_LIVE_ORDERS unaffected | none of this touches order placement; live stays blocked |

## Rollback
Each piece is one seam. Revert = restore the single call at feeder:148 and lifecycle:933-943, delete ingestor. Git commit per piece.

## Test plan
- No synthetic proxy reaches a decision feature (assert capability mask path uses real tf, `is_synthetic_chart_proxy` absent from decision candles).
- No-lookahead: build features/exit at cutoff T with a candle at T+1 present in cache → the T+1 bar must be excluded (cutoff_proof rejects).
- Exit realism: seed a replay window with a real OHLC bar whose high>=tp → simulate_exit closes at TP, not timeout.
- Reject-not-fake: symbol with empty cache → candidate skipped, not fabricated.
- Full suite stays green; smoke test: candidates still flow end-to-end with ingestor on.

## Open question for owner
Phase 1 scope: **1 timeframe (5m) inline ingest** to prove the wiring, or multi-timeframe now? Recommendation: **1 tf first** (smallest correct change), expand in a later phase. Also: run ingest **inline in run_once** (simplest) vs a **separate supervised loop** (more robust, more moving parts). Recommendation: inline for Phase 1.
