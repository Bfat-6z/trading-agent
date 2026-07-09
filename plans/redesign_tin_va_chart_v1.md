# Redesign v1 — TIN + CHART (3 khung, two-pass) — OWNER-APPROVED DIRECTION

**Owner decision (2026-07-10, verbatim intent):** "1 tao chọn 1b; 2 tao chọn lớn hơn — vừa theo tin vừa theo
chart luôn, và chart thì nhìn cả 3 khung rồi quyết định; nếu chọn đánh khung nào thì nhìn lại khung đó rồi
quyết định."
- **1B**: mission keeps running as-is 1-2 days → P0 closes accumulate → calibration confirms the thesis-wrong split.
- **Redesign**: information (news/whale/funding/OI) + chart TOGETHER; charts = all three TFs (15m/1h/4h) on the
  first look; after the model picks a TF, it looks AGAIN at that TF (fresh, focused) before the final call.

Status: **DESIGN ONLY — nothing built.** Per owner: "thiết kế kỹ chứ k phải phát triển liền", "plan càng chi
tiết càng tốt". Build starts only after owner OKs this spec (and after P0 confirms, per 1B).

---

## 1. Why this shape (evidence recap, one paragraph)

n=81 proved chart-gazing on a single 15m frame has no edge (WR 14.8% vs 64.6% breakeven; 96.7% of the loss is
wrong ENTRY DIRECTION; zero losses ever reached +1R → thesis-wrong, not noise-stops). So the entry SIGNAL must
change. The two things with supporting evidence: (a) semi-strong *information* reactions (16-agent research
verdict; funding/OI/news/whale-flow), (b) the one +EV lab setup = capitulation + OI-declining. The owner's
two-pass multi-TF idea attacks the same root cause differently: it replaces "one noisy 15m glance" with
cross-TF agreement + a focused second look. All of these become **tagged, measured paths** — data decides which
lives (owner: "tận dụng model thông minh một cách thông minh" + prove-or-kill).

## 2. Core design — trigger paths → two-pass decision → tagged execution

### 2a. Stage 0 — TRIGGER (code, not prompt; cheap; no vision)
A coin enters the candidate list ONLY via one of these paths (each trade permanently tagged `trigger_path`):

| path | trigger (deterministic, in code) | source (exists today) |
|---|---|---|
| `news` | fresh catalyst: `news_latest.json` catalyst_score ≥ T_news AND event matches coin (or macro event → majors) | state/agent_memory/news_latest.json (LIVE, ts fresh; NOTE: per-symbol tagging thin — most events are macro-level) |
| `whale` | `whale_flow_latest.json` by_symbol[sym]: crowd_bias LONG/SHORT with notional ≥ T_whale | state/agent_memory/whale_flow_latest.json (LIVE, 127 syms, crowd_bias + flow notionals). **Contract `allowed_effect: shadow_only` → starts as CONTEXT/tag only, never sole trigger until measured** |
| `funding_extreme` | \|8h funding\| ≥ T_fund (crowded positioning to fade) | funding already in context |
| `flush_no_oi` | capitulation-flush proxy (ret5 ≤ −3% + vol_ratio ≥ 2); OI-declining leg DEFERRED until deriv re-enable (§4) — renamed from umbrella `funding_oi` per Opus review L1: two hypotheses, two buckets | funding/vol already in context; OI needs re-enable |
| `chart_align` | NO information trigger, but **all 3 TFs agree on direction** (15m+1h+4h EMA-stack/structure alignment, computed in code from already-fetched bars) | build_context already fetches 1h/4h |

Hard rule (code-enforced, NOT prompt-enforced — the model has proven it ignores prompt rules): **no trigger →
no candidate → the model never even sees the coin as a trade option that cycle.** This is selection, not a
judgment gate — consistent with the owner's "đừng bảo thủ QUÁ" middle ground.

### 2b. Stage 1 — BROAD LOOK (one batched vision call, as today)
For candidates only: render 15m/1h/4h charts + info context block (catalyst headline(s), whale bias, funding,
OI slope, regime). Model outputs per coin: `direction, tf_choice (15m|1h|4h), conviction, thesis (1 line)` — or
SKIP. This is the *owner's step 1: "nhìn cả 3 khung rồi quyết định [khung nào]"*.

### 2c. Stage 2 — SECOND LOOK (NEW; the owner's re-look)
For each stage-1 pick (bounded: max 3/cycle): render a **fresh, single-TF, larger chart** of the chosen TF
(more bars, entry/SL/TP zones drawn) + the SAME info context + stage-1's own thesis. Model must answer:
**CONFIRM (with entry/sl/tp on that TF's structure) or REJECT (say why).** Only CONFIRM trades execute.
- Rationale: two-pass = triage → focused verification; the second look kills marginal ideas the broad scan
  over-liked. Cost: ≤3 extra vision calls/cycle, gpt-5.5 = free.
- `tf_basis` (exists) + `trigger_path` + `stage2_confirmed` all land in the trade record.

### 2d. Execution + hold (per chosen TF)
- LAW unchanged: x5/x10 only, 5-10% equity, paper-LOCKED. Gap-veto + daily breaker + caps stay.
- Structure SL/TP set on the CHOSEN TF (not 15m defaults). Hold expectation scales with TF: 15m→hours,
  1h→1-2 days, 4h→days. No discretionary timeout (owner law; already the case).
- resolve() unchanged (P0 metrics already flow).

## 3. Measurement — data decides which path lives (the whole point)
- Every close already carries P0 metrics; ADD `trigger_path`, `tf_choice`, `stage2_confirmed` to the open rec →
  they flow to closed.jsonl automatically.
- `calibration_report` gains `by_trigger_path` + `by_tf_choice` groupings (trivial: `_group_stats` reuse).
- **Kill criteria per path** (auto-flagged in progress.jsonl, owner decides the cut): at n≥20 per path, if
  mean_actual_R < 0 → flag `PATH_BLEEDING`; two consecutive flagged windows → recommend disable (env toggle per
  path: `LLM_TRADER_PATH_<NAME>=0`). Same bar for `chart_align` as for info paths — no favorites.
- Success metric for the redesign overall: WR & mean_actual_R vs the n=81 baseline (14.8%, −0.54$/trade) at
  n≥30 new closes; plus per-path expectancy.

## 4. Plumbing (what actually has to be built)

| # | item | size | notes |
|---|---|---|---|
| 1 | `information_context.py` — read news_latest.json + whale_flow_latest.json (fresh-window check, fail-soft to empty), normalize per-symbol: `{news_events, catalyst, macro_risk, whale_bias, whale_notional}` | S | pure read; respect whale `shadow_only` (context only) |
| 2 | Trigger engine in llm_trader (stage 0): the 4 path checks, deterministic, logged per cycle (`trigger_log.jsonl`: sym, paths_hit, values) — auditable "why was this coin considered" | M | thresholds as env with defaults; fail-open to `chart_align`-only if info files stale |
| 3 | OI re-enable, bounded: `with_deriv=True` ONLY for stage-1 candidates (≤10 syms), not the whole universe scan | S | the 2026-07-08 latency root-cause was fleet-wide deriv fetch; bounded per-candidate is fine |
| 4 | Stage-2 second-look call: new focused render (existing chart lib, more bars, single TF) + `_llm_vision` per candidate, ≤3/cycle | M | reuse llm_trader_charts; new prompt |
| 5 | Rec fields `trigger_path`/`tf_choice`/`stage2_confirmed` + calibration groupings | S | additive |
| 6 | Tests: trigger paths (incl. stale-info fail-soft), stage-2 gating (no CONFIRM → no order), P0 fields intact, LAW pinned | M | extend existing suites |

Explicitly NOT in v1: auto-disabling paths (owner decides from flags), whale as sole trigger (shadow_only
contract), reflection→playbook loop (research: theater), any live-order path (LOCKED, forever).

## 5. Rollout
- **R0 (now, per 1B):** current bot untouched; P0 accumulates; this spec awaits owner OK.
- **R1:** build plumbing #1/#2/#5 — dark: triggers log + tag, don't gate → verify trigger quality on live
  data for ~a day (how many candidates/day per path? sane?). Zero behavior change.
- **R2:** flip `LLM_TRADER_REDESIGN=1`: triggers gate candidates; two-pass on; measure.
- **R3 (n≥30):** read per-path verdicts → owner prunes/keeps; iterate thresholds.
- Every step: Opus-xhigh adversarial review before flip (Codex out of tokens). Rollback = env flag, instant.

**R1 STATUS (2026-07-10): BUILT + REVIEWED (Opus xhigh: SHIP, no critical/high) + tests 20/20 + 39 existing
green.** `llm_trader_triggers.py` (read_news/evaluate/log_cycle, fail-soft) wired into run_once; tags flow
decision→position→PENDING→closed.jsonl; `by_trigger_path` in calibration_report. Deviation from plan: R1 runs
UNCONDITIONALLY (it is pure measurement, like P0 — no flag needed until R2 gating). Review fixes applied: naive
news-ts→UTC (M1), funding_oi split into funding_extreme/flush_no_oi (L1), _num rejects inf (L3). Known R2 tuning
items from live smoke: whale path over-fires (score=1.0 from a single Telegram event, 11/127 syms) — thresholds
will be tuned on trigger_log data before the R2 flip; trigger_log rotation if the window runs long (M2).

## 6. Honest risks
1. **News per-symbol tagging is thin** (top_events symbols[] mostly empty) → `news` path may fire mainly on
   majors via macro events at first. Acceptable: measured, and better than fake precision.
2. **Trigger scarcity** → far fewer trades (maybe 1-5/day vs 17). Slower measurement; that's the trade-off for
   not bleeding. Owner accepted implicitly by choosing redesign; flagged anyway.
3. **Model may still call direction wrong even on triggered coins** — possible; that's what per-path
   measurement + kill criteria are for. Probability of finding real edge remains honestly ~10-15%.
4. **Two-pass latency**: +≤3 vision calls ≈ +2-6 min/cycle worst case. Cycle is 90s loop — stage-2 must be
   async-tolerant (positions open next cycle; signals are bar-close-based, 1-2 cycle delay is immaterial at
   1h/4h basis, minor at 15m).
5. **Threshold cherry-picking** risk: defaults chosen ONCE from percentile scans, logged in this doc when set,
   never tuned on the same data that judges them (Šidák lesson from funnel bughunt).

## 7. Open questions for owner (answer whenever)
1. Trade frequency floor: nếu redesign chỉ ra 1-2 lệnh/ngày, chấp nhận không? (đo cần ~2-4 tuần thay vì 2 ngày)
2. `chart_align` path: giữ (đúng ý "vừa theo chart") nhưng nếu sau n≥20 nó bleed như 15m cũ → cắt luôn OK?
3. 15m vẫn là 1 lựa chọn tf_choice của model, hay bỏ hẳn 15m (chỉ 1h/4h)? (evidence nói 15m nhiễu nhất;
   default v1 GIỮ 15m nhưng đo riêng — cắt bằng số liệu, không bằng cảm giác)

---
Next action khi owner OK spec: build R1 (dark plumbing), Opus-xhigh review, rồi mới R2.
