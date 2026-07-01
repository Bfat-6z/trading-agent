"""Edge-research harness — orchestrator (HARNESS-5 + HARNESS-6).

Runs ONE setup family end-to-end for a given entry timeframe:
  universe (liquidity at window start) -> fetch data -> sweep IN-SAMPLE ->
  pick best -> overfit gate (all in-sample checks) -> peek sealed holdout ONCE
  (only if it passed) -> write ledger row + report.

Timeframe is a sweep dimension: call run_family once per TF (15m/1h/4h) and the
report says which TF had the highest DSR. KILL-by-default; most runs find no edge
and that is reported honestly.

No wall-clock inside the harness: end_ms + stamped_at are passed in by the caller.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import backtest_data_fetcher as bf
import overfit_gate as og
import research_governance as rg
import research_ledger as rl
import sweep_runner as sw

ROOT = Path(__file__).resolve().parent
REPORT_DIR = ROOT / "plans" / "260701-0200-claude-takeover-edge-first" / "reports"

# HTF pairing for each entry timeframe (bias timeframe)
HTF_FOR = {"5m": "1h", "15m": "1h", "1h": "4h", "4h": "1d"}


def fetch_datasets(client: Any, symbols: list[str], entry_tf: str, htf_tf: str,
                   months: float, end_ms: int, quote_vols: dict[str, float],
                   sleep_between: float = 0.02) -> dict[str, dict[str, Any]]:
    ds: dict[str, dict[str, Any]] = {}
    for sym in symbols:
        b_entry = bf.fetch_history(sym, entry_tf, months=months, end_ms=end_ms, client=client, sleep_between=sleep_between)
        b_htf = bf.fetch_history(sym, htf_tf, months=months + 1.0, end_ms=end_ms, client=client, sleep_between=sleep_between)
        ds[sym] = {"bars_5m": b_entry, "bars_1h": b_htf, "quote_volume_24h": quote_vols.get(sym, 0.0)}
    return ds


def run_family(family: str, spec_factory: Callable[[dict[str, Any]], dict[str, Any]],
               param_grid: dict[str, list[Any]], datasets: dict[str, dict[str, Any]],
               *, entry_tf: str, split_ts_ms: int, stamped_at: str,
               direction_of: Callable[[dict[str, Any]], str] | None = None,
               precomputed: dict[str, dict[str, Any]] | None = None,
               n_trials_offset: int | None = None) -> dict[str, Any]:
    """Full pipeline for one family on one timeframe. Writes ledger + report.
    `precomputed` supplies enriched indicator dfs (Family A CVD/funding).
    DSR multiple-testing correction uses the GLOBAL cumulative trial count from
    the ledger (auto), so the penalty never resets. `n_trials_offset` (rare) lets
    a caller override with an explicit prior count instead of the auto global."""
    sweep = sw.run_sweep(spec_factory, param_grid, datasets, split_ts_ms,
                         sweep_name=f"{family}_{entry_tf}", precomputed=precomputed)
    prior = int(n_trials_offset) if n_trials_offset is not None else rg.global_trial_count()
    n_trials = sweep["n_trials"] + prior
    best = og.pick_best(sweep["results"])
    if best is None:
        return _finalize(family, entry_tf, n_trials, None, None, None, stamped_at,
                         reason="no_specs")

    # RUNTIME NO-LOOKAHEAD GUARD: a (possibly self-generated) winning spec that
    # repaints would win in-sample by cheating. Re-check the best spec across all
    # symbols before it can reach the holdout; reject a leaker outright.
    guard_pre = precomputed if precomputed is not None else sw.precompute_indicator_dfs(datasets)
    guard = rg.guard_spec_across_symbols(best["spec"], guard_pre)
    if not guard["clean"]:
        return _finalize(family, entry_tf, n_trials, best, None, None, stamped_at,
                         final_verdict="KILL", reason=f"lookahead_guard_failed:{guard['mismatches']}mismatch")

    verdict = og.evaluate_candidate(best, sweep["results"], n_trials)
    holdout = None
    holdout_blocked = None
    if verdict["pre_holdout_pass"]:
        spec_id = best["spec_id"]
        holdout_key = rg._holdout_digest(split_ts_ms)
        if not rg.can_peek_holdout(spec_id):
            # HOLDOUT BUDGET: this spec already spent its one-and-only peek.
            holdout_blocked = "holdout_already_peeked"
        else:
            # fail-closed: order-flow specs must peek on enriched dfs, never the
            # plain backtest_symbol fallback (which would drop CVD/funding).
            if rg.spec_has_order_flow(best["spec"]) and precomputed is None:
                holdout_blocked = "order_flow_holdout_needs_enriched_precomputed"
            else:
                holdout = og.peek_holdout_once(best["spec"], datasets, split_ts_ms,
                                               exit_cfg=best["spec"].get("exit"), precomputed=precomputed)
                rg.record_holdout_peek(spec_id, holdout_key, stamped_at)
    final_verdict = "KILL"
    reason = holdout_blocked or "failed_in_sample_overfit_gate"
    if verdict["pre_holdout_pass"] and holdout:
        final_verdict = holdout["verdict"]
        reason = holdout.get("reason") or ("passed_all_gates" if final_verdict == "PASS" else "failed_sealed_holdout")

    return _finalize(family, entry_tf, n_trials, best, verdict, holdout, stamped_at,
                     final_verdict=final_verdict, reason=reason, sweep=sweep)


def _finalize(family, entry_tf, n_trials, best, verdict, holdout, stamped_at,
              *, final_verdict="KILL", reason="no_edge", sweep=None) -> dict[str, Any]:
    direction = (best["spec"].get("direction") if best else "?")
    row = {
        "stamped_at": stamped_at,
        "family": family,
        "timeframe": entry_tf,
        "direction": direction,
        "n_trials": n_trials,
        "spec_id": (best["spec_id"] if best else None),
        "spec": (best["spec"] if best else None),
        "in_sample": (best["in_sample"] if best else None),
        "dsr": (verdict["dsr"] if verdict else None),
        "cross_consistency": (verdict["cross_consistency"] if verdict else None),
        "plateau": (verdict["plateau"] if verdict else None),
        "checks": (verdict["checks"] if verdict else None),
        "holdout": (holdout["holdout"] if holdout else None),
        "verdict": final_verdict,
        "reason": reason,
    }
    rl.append_row(row)
    rl.regenerate_ranked()
    _write_report(family, entry_tf, n_trials, best, verdict, holdout, sweep, final_verdict, reason, stamped_at)
    return row


def _write_report(family, entry_tf, n_trials, best, verdict, holdout, sweep,
                  final_verdict, reason, stamped_at) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"sweep_{family}_{entry_tf}.md"
    lines = [f"# Sweep report — {family} @ {entry_tf}", "",
             f"Stamped: {stamped_at}", f"Trials (N): **{n_trials}**",
             f"Verdict: **{final_verdict}** — {reason}", ""]
    if sweep:
        exps = sorted([float(r["in_sample"].get("expectancy_r", 0) or 0) for r in sweep["results"]])
        if exps:
            pos = sum(1 for e in exps if e > 0)
            lines += ["## In-sample expectancy distribution (all trials)",
                      f"- min {exps[0]:.3f} | median {exps[len(exps)//2]:.3f} | max {exps[-1]:.3f}",
                      f"- positive: {pos}/{len(exps)} ({pos/len(exps)*100:.0f}%)", ""]
    if best:
        m = best["in_sample"]
        lines += ["## Best in-sample candidate",
                  f"- spec_id: `{best['spec_id']}`",
                  f"- trades: {m.get('trades')} | expectancy_r: {m.get('expectancy_r')} | "
                  f"profit_factor: {m.get('profit_factor')}",
                  f"- spec: `{json.dumps(best['spec'], default=str)[:300]}`", ""]
    if verdict:
        d = verdict["dsr"]; cc = verdict["cross_consistency"]; pl = verdict["plateau"]
        lines += ["## Overfit gate (in-sample)",
                  f"- DSR: {d.get('dsr'):.3f} (SR {d.get('sr'):.3f} vs SR0 {d.get('sr0'):.3f}) — "
                  f"{'PASS' if verdict['checks']['dsr_significant'] else 'FAIL'}",
                  f"- positive symbols: {cc['positive_symbols']}/{cc['n_symbols']} — "
                  f"{'PASS' if verdict['checks']['enough_symbols'] else 'FAIL'}",
                  f"- positive subperiods: {cc['positive_subperiods']} — "
                  f"{'PASS' if verdict['checks']['enough_subperiods'] else 'FAIL'}",
                  f"- plateau positive fraction: {pl['positive_fraction']:.2f} — "
                  f"{'PASS' if verdict['checks']['is_plateau'] else 'FAIL'}",
                  f"- pre-holdout pass: **{verdict['pre_holdout_pass']}**", ""]
    if holdout:
        h = holdout["holdout"]
        lines += ["## Sealed holdout (peeked once)",
                  f"- trades: {h.get('trades')} | expectancy_r: {h.get('expectancy_r')} | "
                  f"profit_factor: {h.get('profit_factor')}",
                  f"- verdict: **{holdout['verdict']}**", ""]
    else:
        lines += ["## Sealed holdout", "- NOT peeked (candidate failed in-sample gate). Holdout stays sealed.", ""]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
