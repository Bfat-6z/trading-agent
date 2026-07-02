"""Meta-learning edge-research loop — Layers 2-4 (rides the clean foundation +
anti-overfit backbone).

Layer 1 (sources -> specs): Tier-1 seed blocks live in strategy_blocks.
Layer 2 (multi-factor hypothesis gen): compose trigger x regime-gate x filter into
  specs, EACH with a written hypothesis. Bounded, reasoned — not a blind product.
Layer 3 (learn from ledger): after a run, compute per-component expectancy stats
  from the sweep logs and write learnings — which triggers/gates/filters/TFs
  correlate with higher expectancy, which are always negative.
Layer 4 (loop + KILL): one iteration = generate -> no-lookahead guard EVERY spec
  -> sweep -> DSR gate (GLOBAL cumulative trial count) -> holdout peek-once ->
  ledger. Auto-STOP after N consecutive dry iterations (no spec passes holdout).

Invariants (never violated): paper-only; live_guard untouched; every generated
spec passes the no-lookahead guard BEFORE backtest; DSR penalty is cumulative
across ALL history; holdout peeked once per spec; diagnostics never gate.
1 spec passing holdout != confirmed edge -> must forward-paper before real money.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import backtest_chart_signal as cs
import orderflow_data as of
import research_governance as rg
import research_harness as rh
import research_ledger as rl
import strategy_compiler as sc
import sweep_runner as sw
import universe_selector as us

ROOT = Path(__file__).resolve().parent
LEARNINGS_PATH = ROOT / "plans" / "260701-0200-claude-takeover-edge-first" / "reports" / "meta_learnings.md"
DRY_STREAK_PATH = ROOT / "state" / "agent_memory" / "meta_dry_streak.json"
MAX_DRY_ITERATIONS = 3   # auto-STOP: this family is exhausted

# --- Layer 2 building material: triggers x regime gates x filters --------------
# Each entry is composable; hypotheses are explicit. Order-flow blocks require an
# enriched df (built below), so filters can include funding/CVD.

TRIGGERS = [
    {"block": "ts_momentum", "params": [{"lookback": 20}, {"lookback": 40}],
     "src": "T3 Liu-Tsyvinski", "hyp": "trend persists: trade with trailing-return sign"},
    {"block": "sweep_reversal", "params": [{"swing_lookback": 25, "reverse_within": 3}],
     "src": "SMC", "hyp": "liquidity sweep then revert"},
    {"block": "bb_reversion", "params": [{"period": 20, "k": 2.0}],
     "src": "mean-reversion", "hyp": "stretched past band reverts"},
    {"block": "breakout_retest", "params": [{"lookback": 20, "tol_atr": 0.3}],
     "src": "breakout", "hyp": "break a level then hold the retest"},
]

REGIME_GATES = [
    {"block": None, "params": None, "hyp": "no regime gate"},
    {"block": "regime_adx_min", "params": {"adx_min": 25}, "hyp": "only in a trending regime (ADX>=25)"},
    {"block": "trend_ema_stack", "params": None, "hyp": "aligned with the EMA trend"},
]

FILTERS = [
    {"block": None, "params": None, "hyp": "no flow filter"},
    {"block": "funding_zscore_fade", "params": {"window": 48, "z": 2.0},
     "hyp": "only when funding z-score is crowded against the crowd (carry tailwind)"},
    {"block": "cvd_reversal", "params": {"min_norm": 0.1}, "hyp": "confirmed by a CVD aggression flip"},
]

# Direction-specific trigger: EMA-cluster rejection (SHORT) / reclaim (LONG) — the
# least-bad component from iteration 1 (+0.05R). Added to the pool for round 2.
EMA_LOCATION_TRIGGER = {
    "dir_block": {"SHORT": "location_reject_ema_from_below", "LONG": "location_reclaim_ema_from_above"},
    "params": [{}], "src": "EMA rejection/reclaim",
    "hyp": "price rejects/reclaims the EMA cluster (least-bad block in iter 1)"}

# A non-order-flow confirmation filter (volume), distinct from the flow filters
# that iteration 1 proved harmful.
VOLUME_FILTER = {"block": "volume_min_ratio", "params": {"min_ratio": 1.5},
                 "hyp": "confirmed by above-average volume"}

DIRECTIONS = ["SHORT", "LONG"]


def _trig_block(trig: dict[str, Any], direction: str) -> str | None:
    if trig.get("dir_block"):
        return trig["dir_block"].get(direction)
    return trig.get("block")


def generate_specs(triggers: list[dict[str, Any]] | None = None,
                   gates: list[dict[str, Any]] | None = None,
                   filters: list[dict[str, Any]] | None = None,
                   exit_grid: tuple[tuple[float, float], ...] | None = None,
                   max_hold_bars: int = 48) -> list[dict[str, Any]]:
    """Layer 2: compose bounded, reasoned multi-factor specs, each with a
    hypothesis + source. Pools default to the module constants but can be supplied
    (e.g. an EVOLVED config learned from the ledger). exit_grid = tuple of
    (sl_atr, rr); default is the tight (1.5,·) grid, but new-mechanism families can
    pass WIDE stops (the ledger's fee lesson)."""
    triggers = triggers if triggers is not None else TRIGGERS
    gates = gates if gates is not None else REGIME_GATES
    filters = filters if filters is not None else FILTERS
    exit_grid = exit_grid if exit_grid is not None else ((1.5, 2.0), (1.5, 3.0))
    specs: list[dict[str, Any]] = []
    for direction in DIRECTIONS:
        for trig in triggers:
            tblock = _trig_block(trig, direction)
            if not tblock:
                continue
            for tparams in trig["params"]:
                for gate in gates:
                    for filt in filters:
                        # skip illogical combo: a funding-fade filter on a
                        # trend-continuation trigger contradicts the thesis
                        if tblock == "ts_momentum" and filt.get("block") == "funding_zscore_fade":
                            continue
                        blocks = [{"block": tblock, "params": tparams}]
                        if gate["block"]:
                            blocks.append({"block": gate["block"], **({"params": gate["params"]} if gate["params"] else {})})
                        if filt["block"]:
                            blocks.append({"block": filt["block"], "params": filt["params"]})
                        hyp = f"{trig['hyp']} | {gate['hyp']} | {filt['hyp']}"
                        for sl_atr, rr in exit_grid:
                            spec = {
                                "name": f"meta_{tblock}_{direction.lower()}",
                                "direction": direction,
                                "entry": {"all": blocks},
                                "exit": {"sl_atr": sl_atr, "tp_atr": sl_atr * rr,
                                         "min_rr": 1.5, "regime_exit": True, "max_hold_bars": max_hold_bars},
                                "source": trig["src"],
                                "hypothesis": hyp,
                            }
                            specs.append(spec)
    # dedup by spec id
    seen: set[str] = set()
    out = []
    for s in specs:
        sid = sc.spec_id(s)
        if sid not in seen:
            seen.add(sid)
            out.append(s)
    return out


def evolve_pools_from_stats(stats: dict[str, Any], *, drop_below: float = -0.15,
                            base_triggers: list[dict[str, Any]] | None = None,
                            base_gates: list[dict[str, Any]] | None = None,
                            base_filters: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Layer 3 -> Layer 2 feedback: given per-component expectancy stats, DROP any
    pool entry whose block's mean expectancy is <= drop_below (proven-bad by
    number), and keep the rest. This is data-driven pruning, not hand-tuning: the
    ledger decides what survives. Returns {triggers, gates, filters, dropped}."""
    def block_ok(block: str | None) -> bool:
        if not block:
            return True   # 'none' options always allowed
        s = stats.get(block)
        if not s:
            return True   # untested block -> allowed (explore)
        return float(s.get("mean_expectancy_r", 0)) > drop_below

    triggers = base_triggers if base_triggers is not None else TRIGGERS
    gates = base_gates if base_gates is not None else REGIME_GATES
    filters = base_filters if base_filters is not None else FILTERS
    dropped = []
    kept_triggers = []
    for t in triggers:
        blocks = list(t["dir_block"].values()) if t.get("dir_block") else [t.get("block")]
        if all(block_ok(b) for b in blocks):
            kept_triggers.append(t)
        else:
            dropped.append(("trigger", blocks))
    kept_filters = []
    for f in filters:
        if block_ok(f.get("block")):
            kept_filters.append(f)
        else:
            dropped.append(("filter", f.get("block")))
    kept_gates = [g for g in gates if block_ok(g.get("block"))]
    return {"triggers": kept_triggers, "gates": kept_gates, "filters": kept_filters, "dropped": dropped}


def guard_specs(specs: list[dict[str, Any]], ref_df, ref_df1) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run the no-lookahead guard on EVERY generated spec BEFORE backtest. Returns
    (clean_specs, rejected). A rejected spec never enters the sweep and never
    counts as a valid trial."""
    clean, rejected = [], []
    for s in specs:
        r = rg.assert_no_lookahead(s, ref_df, ref_df1)
        (clean if r["clean"] else rejected).append(s)
    return clean, rejected


def build_enriched_precomputed(client: Any, symbols: list[str], entry_tf: str, htf_tf: str,
                               months: float, end_ms: int, quote_vols: dict[str, float]) -> dict[str, dict[str, Any]]:
    """Enriched dfs (chart + CVD + funding) so both chart and order-flow blocks
    work on the same, ts-aligned frames (fail-closed enrich)."""
    pre: dict[str, dict[str, Any]] = {}
    for sym in symbols:
        fb = of.fetch_klines_with_flow(sym, entry_tf, months=months, end_ms=end_ms, client=client, sleep_between=0.02)
        fund = of.fetch_funding_series(sym, months=months, end_ms=end_ms, client=client)
        ind = cs.compute_indicators(fb)
        enr = of.enrich_indicator_df(ind, fb, fund)
        hb = of.fetch_klines_with_flow(sym, htf_tf, months=months + 1.0, end_ms=end_ms, client=client, sleep_between=0.02)
        pre[sym] = {"df": enr, "df_1h": cs.compute_indicators(hb), "quote_volume_24h": quote_vols.get(sym, 0.0)}
    return pre


def learn_from_ledger(ledger_path: Path = rl.LEDGER_PATH) -> dict[str, Any]:
    """Layer 3: component-level stats across the whole ledger. Which timeframes /
    verdicts recur; cumulative trial count; dry streak. (Per-spec component
    correlation is computed from sweep logs in run_iteration's report.)"""
    rows = rl.load_rows(ledger_path)
    by_tf: dict[str, list[float]] = {}
    for r in rows:
        tf = r.get("timeframe", "?")
        exp = r.get("in_sample", {})
        if isinstance(exp, dict) and exp.get("expectancy_r") is not None:
            by_tf.setdefault(tf, []).append(float(exp["expectancy_r"]))
    tf_stats = {tf: {"n": len(v), "mean_best_expectancy": round(sum(v) / len(v), 4)}
                for tf, v in by_tf.items() if v}
    return {"rows": len(rows), "global_trial_count": rg.global_trial_count(ledger_path),
            "by_timeframe_best_expectancy": tf_stats}


def component_stats_from_sweeps(sweep_dir: Path = sw.SWEEP_DIR) -> dict[str, Any]:
    """Layer 3 core: read the per-spec sweep logs and correlate each block's
    presence with in-sample expectancy — which components help vs always hurt."""
    if not sweep_dir.exists():
        return {}
    block_exp: dict[str, list[float]] = {}
    for f in sweep_dir.glob("*_insample.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            exp = float((row.get("in_sample") or {}).get("expectancy_r", 0) or 0)
            trades = int((row.get("in_sample") or {}).get("trades", 0) or 0)
            if trades < 100:   # ignore tiny-sample noise in the learning signal
                continue
            entry = (row.get("spec") or {}).get("entry") or {}
            blocks = [b.get("block") for grp in ("all", "any") for b in (entry.get(grp) or [])]
            for b in set(blocks):
                if b:
                    block_exp.setdefault(b, []).append(exp)
    stats = {b: {"n": len(v), "mean_expectancy_r": round(sum(v) / len(v), 4),
                 "always_negative": all(x < 0 for x in v)}
             for b, v in block_exp.items() if v}
    return dict(sorted(stats.items(), key=lambda kv: kv[1]["mean_expectancy_r"], reverse=True))


def _read_dry_streak() -> int:
    if DRY_STREAK_PATH.exists():
        try:
            return int(json.loads(DRY_STREAK_PATH.read_text())["streak"])
        except Exception:
            return 0
    return 0


def _write_dry_streak(streak: int) -> None:
    DRY_STREAK_PATH.parent.mkdir(parents=True, exist_ok=True)
    DRY_STREAK_PATH.write_text(json.dumps({"streak": streak}), encoding="utf-8")


def write_learnings(report: dict[str, Any]) -> None:
    LEARNINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    cs_stats = report.get("component_stats", {})
    lines = ["# Meta-loop learnings", "",
             f"Iteration stamped: {report.get('stamped_at')}",
             f"Global cumulative trial count: **{report.get('global_trial_count')}**",
             f"Specs generated: {report.get('generated')} | guarded-out (lookahead): "
             f"{report.get('guarded_out')} | tested: {report.get('tested')}",
             f"Dry streak: {report.get('dry_streak')}/{MAX_DRY_ITERATIONS}"
             f"{'  -> STOP (family exhausted)' if report.get('stop') else ''}", "",
             "## Per-component in-sample expectancy (samples >=100 trades)",
             "| block | n | mean_expectancy_r | always_negative |",
             "|---|---|---|---|"]
    for b, s in cs_stats.items():
        lines.append(f"| {b} | {s['n']} | {s['mean_expectancy_r']} | {s['always_negative']} |")
    lines += ["", "## Per-cell verdicts this iteration", "| cell | verdict | reason | best_exp | trades |",
              "|---|---|---|---|---|"]
    for c in report.get("cells", []):
        m = c.get("in_sample") or {}
        lines.append(f"| {c['cell']} | {c['verdict']} | {c['reason']} | "
                     f"{m.get('expectancy_r')} | {m.get('trades')} |")
    lines += ["", f"## Verdict: **{report.get('overall_verdict')}**", report.get("note", "")]
    LEARNINGS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


CANDIDATE_FILE = ROOT / "state" / "agent_memory" / "forward_candidate.json"


def _emit_forward_candidate(cells: list[dict[str, Any]], stamped_at: str) -> None:
    """Write the best in-sample candidate (highest expectancy_r among cells that
    have a spec) to forward_candidate.json. forward_strategy_paper reads this, so a
    freshly-discovered lead flows to forward-paper automatically. This is a
    FORWARD-TEST candidate (best-of-search), NOT a confirmed edge — it was still
    KILLed in-sample by DSR; forward-paper is exactly how we test it out-of-sample."""
    best = None
    for c in cells:
        sp = c.get("spec")
        if not sp:
            continue
        exp = float((c.get("in_sample") or {}).get("expectancy_r", -9) or -9)
        if best is None or exp > best[0]:
            best = (exp, sp, c)
    if not best:
        return
    exp, sp, c = best
    CANDIDATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CANDIDATE_FILE.write_text(json.dumps({
        "stamped_at": stamped_at, "spec": sp,
        "insample_expectancy_r": exp, "verdict": c.get("verdict"),
        "note": "forward-test candidate (best-of-search) — NOT a confirmed edge; KILLed in-sample by DSR",
    }, indent=1, default=str), encoding="utf-8")


def run_iteration(client: Any, *, end_ms: int, stamped_at: str, months: float = 9.0,
                  timeframes: tuple[str, ...] = ("1h", "4h"), max_symbols: int = 9,
                  triggers: list[dict[str, Any]] | None = None,
                  gates: list[dict[str, Any]] | None = None,
                  filters: list[dict[str, Any]] | None = None,
                  exit_grid: tuple[tuple[float, float], ...] | None = None,
                  new_family: bool = False) -> dict[str, Any]:
    """Layer 4: ONE honest iteration. Returns a report. Optional pools let a caller
    pass an EVOLVED (ledger-pruned) config for later iterations. exit_grid sets the
    SL/RR grid (wide stops for new-mechanism families). new_family=True resets the
    dry streak (a genuinely new source/angle, per the post-STOP mandate)."""
    if new_family:
        _write_dry_streak(0)
    uni = us.select_universe(client, end_ms=end_ms, months=months, timeframe="1h",
                             min_daily_quote_volume=50_000_000.0, max_symbols=max_symbols)
    symbols = uni["selected"]
    quote_vols = {s: uni["detail"].get(s, 0.0) for s in symbols}

    specs = generate_specs(triggers=triggers, gates=gates, filters=filters, exit_grid=exit_grid)
    split = end_ms - int(3 * 30 * 24 * 3600 * 1000)
    cells = []
    any_pass = False
    total_generated = len(specs)
    total_guarded_out = 0
    total_tested = 0

    for entry_tf in timeframes:
        htf_tf = rh.HTF_FOR[entry_tf]
        pre = build_enriched_precomputed(client, symbols, entry_tf, htf_tf, months, end_ms, quote_vols)
        ref = next(iter(pre.values()))
        clean, rejected = guard_specs(specs, ref["df"], ref["df_1h"])
        total_guarded_out += len(rejected)
        total_tested += len(clean)
        if not clean:
            cells.append({"cell": f"meta_{entry_tf}", "verdict": "KILL", "reason": "all_specs_guarded_out",
                          "in_sample": {}})
            continue
        # run all clean specs as one sweep for this TF via a list-indexing factory
        factory = lambda p, _c=clean: _c[p["i"]]
        grid = {"i": list(range(len(clean)))}
        row = rh.run_family(f"meta_{entry_tf}", factory, grid, {}, entry_tf=entry_tf,
                            split_ts_ms=split, stamped_at=stamped_at, precomputed=pre)
        cells.append({"cell": f"meta_{entry_tf}", "verdict": row["verdict"], "reason": row["reason"],
                      "in_sample": row.get("in_sample"), "holdout": row.get("holdout"),
                      "spec": row.get("spec")})
        if row["verdict"] == "PASS":
            any_pass = True

    # LINK meta-loop -> forward-paper: write the best-sampled candidate spec to
    # forward_candidate.json so forward_strategy_paper picks it up automatically.
    _emit_forward_candidate(cells, stamped_at)

    # Layer 3: learnings
    comp = component_stats_from_sweeps()
    led = learn_from_ledger()

    # Layer 4: dry-streak / auto-STOP
    dry = 0 if any_pass else _read_dry_streak() + 1
    _write_dry_streak(dry)
    stop = dry >= MAX_DRY_ITERATIONS

    overall = "PASS_CANDIDATE" if any_pass else "KILL"
    note = ("A spec passed the sealed holdout — this is a CANDIDATE, NOT confirmed "
            "edge. Must forward-paper before any real money." if any_pass else
            ("No spec cleared the gate. " + ("Family exhausted (dry streak hit) — propose a new "
             "source/angle instead of grinding more combos." if stop else "Normal KILL; loop may continue.")))
    report = {"stamped_at": stamped_at, "generated": total_generated, "guarded_out": total_guarded_out,
              "tested": total_tested, "global_trial_count": led["global_trial_count"],
              "cells": cells, "component_stats": comp, "ledger": led,
              "dry_streak": dry, "stop": stop, "overall_verdict": overall, "note": note}
    write_learnings(report)
    return report
