# Self-Improving Trading Agent — Detailed Development Roadmap

**Supersedes** `cost_efficient_redesign.md` (moot: gpt-5.5 via 9router is FREE — do NOT optimize for
its call cost; call it as much as learning needs). The only paid resource is the owner's $200 Claude
(assistant) sub — so this plan exists to make execution efficient and to make the agent AUTONOMOUS,
so Claude babysits less.

**North star (owner, 2026-07-09):** leverage a smart free model (gpt-5.5) to TEACH itself to trade well
over a long horizon — grow it like the `hermes` project grew, via effective learning, not conservative
gates and not blind trust. -EV now (18% WR) is the STARTING POINT. Success = the model measurably gets
BETTER at trading over weeks.

---

## 0. Ground truth (what exists today)

- **Decision engine:** `llm_trader.py` — `build_context()` (60 coins, indicators, multi-TF 15m/1h/4h
  charts) → `decide()` (gpt-5.5 vision) → `open_positions()` → `resolve()`. Runs `--interval-seconds 90`.
- **Full-trust already shipped:** only owner-LAW (x5/x10, 5-10% size, paper-LOCKED) + ruin (gap-veto,
  daily -15% breaker, caps) remain. No quality gates.
- **Existing (but WEAK) learning:** `llm_trader_memory.py::mistake_lessons()` + `build_memory_context()`
  feed AGGREGATE stats/lessons into the prompt each cycle. Honest verdict: REAL but INEFFECTIVE — the
  model reads "you win 18%" as text and ignores it; WR unchanged.
- **Data already captured per trade:** `state/llm_trader/closed.jsonl` rows carry `rationale`, `entry`,
  `exit`, `r`, `reason`, `entry_feats`, `regime`, `tf_basis`, `bars_held`, `margin`. `thinking_latest.json`
  holds the model's reasoning. `state/memory/brain.db` = trials/lessons registry (second brain).
- **Robustness gap:** the mission process keeps dying / needs manual restarts; launched by hand
  (`--interval-seconds`, NOT `--loop`). This is what forces Claude to babysit = burns the $200 sub.

---

## 1. Why the current learning fails (root cause)

The model is fed learning as **aggregate, third-person, code-authored text** ("OVER-TRADING: 18% win").
That is trivially ignorable — it isn't tied to the model's OWN decisions, isn't specific, and the model
never has to RECKON with it. Three fixes, each a phase:

1. Make the feedback **specific + first-person + tied to the model's own past reasoning** (rationale ⟷
   outcome), so it confronts its OWN mistakes, not a statistic.
2. Make the model **actively reflect** (write its own lesson), not passively read one.
3. **Close the loop:** the model's self-authored lessons drive the next decision.

---

## 2. THE CORE — an effective learning loop (detailed)

### 2a. Structured trade journal (foundation)
Extend the closed-trade record into a first-class **learning journal** (`state/llm_trader/journal.jsonl`,
append-only). Each entry, written by `resolve()` at close:
```
{
  ts_open, ts_close, symbol, side, tf_basis, regime,
  PREDICTION: { rationale (verbatim), predicted_R, sl_pct, tp_pct, entry_px, conviction, confluences },
  SETUP_SNAPSHOT: { rsi14, px_vs_ema20_pct, vol_ratio, atr_pct, gap_risk, wick_intensity, htf_trend, smc_zone },
  OUTCOME: { exit_reason (sl/tp/liq/trail), actual_R, net, bars_held, mfe_R, mae_R },
  CALIBRATION: { predicted_R - actual_R, sl_was_in_noise (bool: mae hit sl before any mfe), tp_realistic (bool) }
}
```
`mfe_R`/`mae_R` (max favorable/adverse excursion) require capturing intrabar highs/lows in `resolve()` —
this is the single most valuable new signal (tells us if the stop was in noise vs the thesis was wrong).

### 2b. Calibration tracker (the "is it learning" ground truth)
`llm_trader_learning.py::calibration_report(journal)` → measures, over the last N trades:
- **Directional hit rate** by regime / tf_basis / setup-type.
- **R-prediction error:** does the model's "R>4" actually average R>4? (systematic over-optimism?)
- **Noise-stop rate:** % of losers where MAE hit the SL before MFE reached +0.5R (stop-in-noise).
- **Rationale-cluster performance:** group by rationale keywords ("EMA pullback", "capitulation", "breakout")
  → which of the model's OWN setup NAMES actually win.
This report is BOTH fed to the model (2c) AND is our metric for "is the teaching working" (§5).

### 2c. Self-reflection pass (gpt-5.5, free, every ~20-30 min or every N closes)
New agent `reflection_agent.py` (free gpt-5.5). Prompt:
> "Here are your last 25 trades: for each, YOUR rationale, then what happened (R, reason, MFE/MAE).
> Here is your calibration report. In 5 bullets: (1) the ONE setup pattern that is losing you the most
> money and why, (2) where your R/SL predictions are systematically wrong, (3) which of your setup types
> actually works, (4) one concrete rule you will change starting now, (5) one thing you should do MORE.
> Write it as instructions to your future self, first-person, specific with numbers."
Output → `state/llm_trader/self_playbook.json` (versioned, append the diff each time).

### 2d. Close the loop — playbook drives decisions
`decide()`'s system prompt gets a **=== YOUR PLAYBOOK (self-authored, follow it) ===** block = the latest
`self_playbook.json`. First-person, specific, its own words → far harder to ignore than code-authored
aggregate lessons. (There is already a `_playbook()` hook wired into the prompt — populate it from here.)

### 2e. Regime/context-adaptive memory
The playbook + calibration are sliced by regime (trending/choppy) and tf_basis. In a choppy regime the
model sees "in choppy your last 20 were 7% WR — here's what you changed last time." The model ADAPTS to
the live regime instead of a global average.

---

## 3. Capability growth (richer inputs — phased, additive)

Each is a discrete phase; the model gets a new sense, we measure if calibration improves.
- **P-A Order-book microstructure** (owner's earlier ask): bid/ask depth imbalance + spread at decision
  time → a "is a stop-hunt likely" and "which way is the book leaning" signal. New fetch in build_context.
- **P-B OI / funding / long-short ratio** as decision inputs (some exist for lanes; wire to the mission).
- **P-C Better charts:** mark the model's own prior entries/exits on the chart so it SEES its history
  visually; annotate the SMC zones it's trading.
- **P-D Portfolio awareness:** feed open positions + correlation so it doesn't stack 5 correlated longs.

## 4. Autonomy & robustness (this is what actually saves the $200 Claude sub)

Every manual restart / debug I do burns the sub. Make the agent self-sufficient:
- **P-R1 Bulletproof launcher + watchdog:** a supervised, self-restarting mission that survives crashes
  and machine reboots WITHOUT me (fix the recurring death; correct `--interval-seconds` launch baked in;
  heartbeat-driven auto-relaunch). See `reference_restart_recovery`.
- **P-R2 Autonomous learning cadence:** reflection_agent + calibration run on a timer, no human trigger.
- **P-R3 Self-monitoring + alert:** the agent writes a daily health+progress digest (WR trend, calibration
  trend, is-it-improving) so I read ONE summary instead of investigating — minimal Claude tokens.
- **P-R4 Guardrail auto-trip:** if equity or WR falls off a cliff, auto-pause + log WHY (not silent).

## 5. Measurement — is the teaching working? (define success up front)

Track weekly, in `state/llm_trader/progress.jsonl`:
- WR, mean-R, calibration error, noise-stop rate — **trend over time is the KPI.**
- Success = these improve across reflection cycles (the model is learning). Flat/worse after M cycles of
  effective reflection = honest signal that gpt-5.5 can't find edge on this TF (then: change TF/market,
  not add gates).
- A/B honesty: keep the L10-style RANDOM control alive as the alpha floor.

## 6. Phasing (build order — each: design → owner OK → build → verify → measure)

| Phase | What | Why first | Effort |
|---|---|---|---|
| **P0** | Trade journal + MFE/MAE capture in resolve() (§2a) | Foundation — everything learns from this | S-M |
| **P1** | Calibration tracker + progress.jsonl (§2b, §5) | The metric; proves whether anything works | S |
| **P2** | reflection_agent → self_playbook → wire into decide() prompt (§2c-e) | THE learning loop — the whole point | M |
| **P3** | Bulletproof launcher + watchdog + daily digest (§4) | Stops Claude babysitting = saves the $200 sub | M |
| **P4** | Order-book microstructure input (§3 P-A) | Owner's ask; new edge source | M |
| **P5+** | remaining capability growth (§3), portfolio awareness | Iterative | ongoing |

**Recommended start: P0 → P1 → P2** (journal → metric → learning loop). P3 in parallel (autonomy).

## 7. Non-negotiables (carry through every phase)
- Paper-only, live LOCKED. Leverage EXACTLY x5/x10. Size 5-10%. gap-veto + daily breaker + caps stay.
- brain.db trials append-only (never delete/decay). Codex adversarial review on trading-logic changes.
- Free gpt-5.5 used liberally; the $200 Claude sub conserved by autonomy + tight plans, not by crippling
  the agent.

---

## 8. VERIFIED STATUS & FINDINGS — 2026-07-09 (updated as things ship)

**Built & LIVE (verified):**
- **P0** (MFE/MAE + R-calibration in `resolve()`) — committed `fd48349`, Opus-xhigh reviewed clean.
- **P1** (`llm_trader_learning.calibration_report` + daily `progress.jsonl`) — committed `edafc82`, reviewed clean.
- Mission process (pid at check: 33696) started **2026-07-09 20:15 UTC** — AFTER P0 commit (19:07 UTC), so it
  **is running the P0+P1 code**. Cmdline `llm_trader.py --interval-seconds 90` (loop mode, NOT the `--loop` trap).
  Mode DISCRETIONARY, `cx/gpt-5.5`, live LOCKED, equity ~$39.80, 109 closes, WR 17.4%, verdict NEGATIVE.

**P0 data coverage: 0 / 109 closes carry `actual_R`** — every existing close finished BEFORE P0 deployed
(last was MRVL 14:48 UTC < 20:15). This is expected, NOT a write bug (rec dict at :1448-49 writes the fields;
verified). 1 open discretionary position (CRCL SHORT) → will be the first P0-instrumented close.

**CORRECTION (a first-pass analysis was wrong — recorded for honesty).** An initial backfill segmented the
**40-row brain.db subset** and concluded "discretionary is a small −2.30 loser, the catastrophe is all
mechanical." **That was an artifact of the subset** — `brain.db.trade_autopsy` only holds the ~40 most recent
mission rows, while the real ledger `closed.jsonl` holds all **109**. Re-segmenting the FULL ledger flips the
conclusion: **the discretionary LLM path is the BIGGER loser.**

| path (FULL ledger n=109) | n | WR | net | by tier |
|---|---|---|---|---|
| **DISCRETIONARY** (the LLM path) | **81** | **15%** | **−43.84** | major −0.93 (12%), mid −22.63 (15%), **micro −20.28 (14%)** |
| **MECHANICAL** (proven methods) | 28 | 25% | −16.36 | major −0.66, mid −15.70 |

Account: $100 → **$39.80** (realized −$60.20 = −43.84 disc + −16.36 mech). Discretionary by regime: choppy 8%,
trending 17% (−32.0), mixed 50% (n=6). By side: LONG 22% (−38.7), SHORT 6%. **It is −EV on every tier, every
regime, every side** — a decisive no-edge signal at n=81 (not the "too small" I wrongly claimed at n=11).

- **Trade rate ≈ 17 discretionary closes/day** (81 over 4.7d). So P0-instrumented data accrues FAST: n≥15 in
  ~1 day, n≥30 in ~2 days. P2's noise-vs-thesis mechanism verdict is **1-2 days out, not weeks.** (The earlier
  "SHADOW acceleration" idea is moot — real data comes fast enough.)
- **Micro leak mechanism (−20.28, 46% of the discretionary bleed) CONFIRMED:** the $50M floor uses 24h **ticker**
  volume; low-float coins that **pump** spike their ticker >$50M transiently → pass the scan gate → the model
  **LONGs the pump top at x10** → they fade/dump → SL + `tier='micro'` tag. Poster child `POWERUSDT` n=3 −8.79
  (all x10 LONG, trending). 9/11 of these symbols are back under $50M now. Raising the floor only partially helps
  (POWER is $124M now); the true driver is pump-chasing = a *judgment* gate the owner vetoed, so NOT band-aided.
- The −14.35 HMSTR (`wr_flush_notknife`, mechanical, 07-06) is the trade that motivated the 07-06 gap-tail fix
  (PER_POS_CAP 0.25→0.10 + atr>3.3% veto); the guard is now on BOTH paths (:543-558, :1084/:1632). No open ruin bug.

**Adversarial review (2 rounds — Opus red-team, then I re-checked its claims) — hardened conclusions:**
- **The no-edge verdict is statistically overwhelming, not marginal.** Breakeven WR needed = **64.6%** (payoff
  ratio 0.55: avg win $0.385 / avg loss $0.702); actual WR = 14.8%. A ~50-point gap at n=81 → p→0. **No +EV
  subset exists** (excluding micro AND the worst-5 longs still leaves n=65 @ −$0.28/trade; best cherry-picked
  cell −$0.09). Median trade −$0.185 → the bleed is BROAD, not just tails.
- **Costs are immaterial: fees+funding = $1.46 = 3.3% of the discretionary loss.** Gross directional PnL −$42.38
  vs net −$43.84. **96.7% of the bleed is bad direction/entries** — not fees, funding, slippage, or double-charging.
  This rules out any cost/exit-mechanics fix as the answer.
- **Two framings CORRECTED (both partly wrong):** (a) "loses on every tier/regime/side" leans on tiny cells
  (major n=8, mixed n=6 — mixed was +median); the claim only holds firmly on the LARGE cells (mid 59, choppy 40,
  trending 35, LONG 45, SHORT 36 — all solidly −EV). (b) The "micro = low-float pump-and-dump" story is too clean:
  `tier` uses a 96-bar kline-volume reconstruction, not the ticker gate, so established coins (APT, GALA, ORDI)
  get mislabeled "micro" — the bucket is mixed. (c) The reviewer's own "risk-inverted 5.7× oversizing" claim I
  RE-CHECKED and **refuted**: micro trades are the EARLIEST (chrono positions 2-20 of 81, median 07-05 04:39),
  so their bigger margin is just **5-10% of the then-$100 equity** — sizing is proportional-to-equity and correct;
  the micro damage is front-loaded, not a sizing bug. SHORTs are NOT "near breakeven" — WR 6%, they just bleed
  *small* via stops; no edge either.
- Attribution aside: of the −$16.36 mechanical loss, a single HMSTR liquidation = −$14.33 (23% of the whole
  account drawdown in one trade); mechanical ex-HMSTR = −$2.03 / 27 trades.

**P2 mechanism — FIRST-PASS from available data (the `reason` field is a proxy for MFE, no P0 wait needed):**
exit-reason split of the 81 discretionary trades = sl 78% / timeout 11% (all pre-change, before discretionary
timeout was killed this session) / tp 10% / liq 1%. The BE-trail arms at +1R and, once armed, turns a reversal
into a `reason='trail'` exit — so **`trail` exits are the noise-stop fingerprint** (reached +1R, then gave it
back). On the clean sub-population where BE is functional (mid+major, 57 losses): **54 `sl`, ZERO `trail`.** Not
one trade reached +1R before failing. That is the **THESIS-WRONG signature** — entries are wrong from the outset
(price goes against them immediately), not good-entries-shaken-out-by-noise. It matches the "96.7% bad direction"
number exactly. (Noise-stops appear only in micro, where BE is correctly disabled — i.e. on illiquid coins.)
**P0 will quantify the precise split, but the direction is already clear: the problem is ENTRY DIRECTION, not
exits or stops.** → the redesign must change the SIGNAL SOURCE (information), not merely slow the horizon while
keeping the same chart-vision entries. This tilts §9 toward **B/C/D (or A+D)** and away from pure-A.

**Bottom line:** the top-level verdict is already decisive from n=81 — **15m discretionary vision-scalping has no
edge and is draining the account** (needs 64.6% WR, has 14.8%; 96.7% bad direction; thesis-wrong entries, not
noise-stops). P0 will only sharpen the split. Per the
owner's own prove-or-KILL protocol the threshold is breached, BUT the owner *this session* deliberately enabled
full-trust discretionary to teach + measure it — so the kill/pivot call is theirs, not a unilateral revert.
**Chosen action: keep it running 1-2 days to capture the definitive P0 mechanism verdict, then present the
redesign decision (revert-to-PROVEN vs horizon/information pivot) with complete data.** No band-aid gate added
(core problem is no-edge, not micro alone; and it would re-add a vetoed judgment gate).

## 9. REDESIGN OPTIONS — for the owner to choose once P0 lands (drafted 2026-07-09, not decided)

Grounded in: the n=81 no-edge verdict + the 16-agent research (edge lives in slower-horizon *information*
reactions, not adversarial 15m chart-direction) + what `decide()` actually feeds the model today.

**What the discretionary model sees NOW (verified):** funding_rate + CVD + regime + 15m/1h/4h charts (vision).
It does NOT see OI / long-short ratio (`with_deriv=False` on the hot path for latency, :327-331) nor
news/whale-flow (those run as separate observer agents, never wired into `decide()`). Yet WITH funding+CVD it
is still 15% WR — so the bottleneck is not merely missing inputs, it's that **15m direction is too noisy to
predict.** More inputs at 15m ≠ edge.

**The P0 verdict (1-2 days) forks the redesign:**
- **NOISE-STOP dominates** (entries fine, price offered ≥1R then stopped) → keep the model's *entries*, fix the
  *horizon/exit*: → **Option A**.
- **THESIS-WRONG dominates** (price went straight against — the model can't read direction) → the *signal
  source* must change: → **Options B / C / D**.

| # | Direction | Evidence for | Infra today | Trade-off |
|---|---|---|---|---|
| **A** | **Slower horizon (1h primary, hold days)** — owner asked "đánh 1h-4h chưa". Same vision model, on 1h/4h, structure stops. | 15m is where noise/adversarial-ness peaks; SNR rises with TF | `build_context` already fetches 1h/4h; model already sees them | fewer trades → slower learning; still chart-direction (may still lack edge) |
| **B** | **Funding/OI mean-reversion** — fade crowded positioning (funding extremes, OI blow-off). | funding is semi-strong info; classic crypto edge | funding fetched; **OI needs re-enabling on hot path** | narrower setup, fewer signals; needs OI plumbing |
| **C** | **News/event reaction** — act on `news_observer` + `whale_flow_observer`. | fastest semi-strong info; observers already run | observers EXIST but **not wired into `decide()`** | latency-sensitive; hard to backtest; parse quality |
| **D** | **Scale the ONE empirical edge: capitulation + OI-declining** (`clf_oi_dn`, microstructure_edge, n=31, −0.16%→+0.045% weak). | the *only* setup with positive expectancy in the whole method-lab | already wired as a lane | weak, small-n; needs OI; may not survive more data |

**Draft recommendation (owner decides):** a combined **A+D** — move the primary horizon to 1h (owner's stated
preference, kills most of the 15m noise) AND gate entries on the one positive-evidence setup (capitulation +
OI-declining), demoting the vision model from *primary decider* to a *confirm/veto* on the 1h chart. This leans
on the only positive evidence we have + the owner's horizon preference + slashes the adversarial noise, while
keeping the model in the loop (not a full revert to PROVEN_ONLY).
**The thesis-wrong first-pass (§8) REINFORCES this:** because the losses are wrong-direction entries (not
shaken-out good entries), pure-A (same vision entries, slower TF) would likely still bleed — the *entry signal
itself* must change. So the model's chart-vision judgment should be DEMOTED to a filter over an information
trigger (D), not kept as the primary direction-caller. Pure-A is the weakest option; A+D or B/D are the live ones.

**Open questions for the owner (genuine forks — will ask, not guess):**
1. Direction: A / B / C / D / A+D combo / other?
2. Keep the vision model as the *decider*, or demote it to a *confirm/filter* over an information trigger?
3. OK to re-enable OI on the hot path (small latency cost)? — required for B and D.
4. Target trade frequency: slower = fewer trades = slower learning but far less bleed. Acceptable?

**→ OWNER DECIDED (2026-07-10):** (1) **1B** — keep the bot running 1-2 days for P0 confirmation (no immediate
stop). (2) Redesign = **BIGGER than A+D: information + chart together** — news/whale/funding/OI triggers AND
multi-TF chart; the model looks at ALL THREE TFs (15m/1h/4h), picks the TF, then takes a SECOND focused look at
the chosen TF before the final call (owner's two-pass idea). Full detailed spec:
**`plans/redesign_tin_va_chart_v1.md`** (trigger paths → two-pass decide → tagged execution → per-path
measurement with kill criteria; R0-R3 rollout; awaiting owner OK on the spec itself before R1 build).

---
## 10. P2 VERDICT — OFFICIAL (2026-07-10, n=15 P0-instrumented closes)

`calibration_report` at the pre-registered n=15 threshold (last 2 closes owner-ordered flatten, booked through
the standard resolve() path):
- **thesis_wrong_rate = 77.8%** (7/9 losses; median loss mfe_R = 0.0 — typical loss NEVER went in favor)
- **noise_stop_rate = 0.0%** — every trade that reached ≥1R was protected or won (2 TP +2/+2.2R, 1 trail +1.1R,
  4 trail ~BE). Exit physics are NOT the problem.
- **over_optimism_R = 3.75** — the model systematically predicts ~3.75R more than it achieves.
- Window WR 40%, mean_actual_R −0.456 (BE-trail slashed the bleed vs the n=81 era, still −EV).
- verdict_hint (machine-generated): *"THESIS-WRONG dominates → losses are bad entries, not bad stops →
  fix = entry SELECTION."*

**This formally validates the R2 redesign direction (trigger-gated entry selection + two-pass confirm) with
pre-registered measurement BEFORE the flip.** Remaining to R2=ON: 24h trigger window (~7h collected) → tune
thresholds from logged discriminators → final Opus review → owner OK → touch redesign.flag + respawn.
Per-path table exists but every path is n≤6 — no per-path verdicts until n≥20 each (post-flip).

Everything above §8 is the original design; §8-§10 are verified ground truth.
