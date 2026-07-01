"""Edge-research harness — sweep runner (HARNESS-3).

Enumerates a parameter/block grid into strategy specs, backtests each spec over
the universe on IN-SAMPLE data ONLY, and logs every spec + result with an honest
trial count. The sealed holdout is NEVER touched here (that happens once, later,
in the overfit gate for the single best survivor).

Determinism: no wall-clock, no RNG in enumeration. Trial count = number of specs
actually run, logged truthfully for the multiple-testing correction downstream.
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any, Callable, Iterable

import backtest_chart_signal as cs
import backtest_runner as br
import strategy_compiler as sc

ROOT = Path(__file__).resolve().parent
SWEEP_DIR = ROOT / "state" / "sweeps"


def expand_grid(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Cartesian product of a param grid -> list of param dicts."""
    if not grid:
        return [{}]
    keys = list(grid.keys())
    combos = itertools.product(*[grid[k] for k in keys])
    return [dict(zip(keys, vals)) for vals in combos]


def build_specs(spec_factory: Callable[[dict[str, Any]], dict[str, Any]],
                param_grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """spec_factory(params) -> a full strategy spec. Applied over the grid.
    De-duplicates by spec_id so identical specs aren't double-counted."""
    specs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for params in expand_grid(param_grid):
        spec = spec_factory(params)
        sid = sc.spec_id(spec)
        if sid in seen:
            continue
        seen.add(sid)
        spec.setdefault("_sweep_params", params)
        specs.append(spec)
    return specs


def backtest_spec_in_sample(spec: dict[str, Any], datasets: dict[str, dict[str, Any]],
                            split_ts_ms: int, exit_cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one spec over the universe, IN-SAMPLE ONLY (bars closing before split).
    Returns pooled metrics + per-symbol trade counts. Holdout untouched."""
    signal_fn = sc.compile_spec(spec)
    cfg = exit_cfg if exit_cfg is not None else spec.get("exit")
    all_trades: list[dict[str, Any]] = []
    per_symbol: dict[str, int] = {}
    for sym, d in datasets.items():
        trades = cs.backtest_symbol(
            d["bars_5m"], d["bars_1h"], d["quote_volume_24h"],
            end_ts_ms=split_ts_ms, signal_fn=signal_fn, exit_cfg=cfg,
        )
        for t in trades:
            t["symbol"] = sym
        all_trades.extend(trades)
        per_symbol[sym] = len(trades)
    m = br.metrics(all_trades)
    return {
        "spec_id": sc.spec_id(spec),
        "spec": spec,
        "in_sample": m,
        "per_symbol_trades": per_symbol,
        "trades": all_trades,   # kept in-memory for the gate; not serialized to ledger
    }


def run_sweep(spec_factory: Callable[[dict[str, Any]], dict[str, Any]],
              param_grid: dict[str, list[Any]],
              datasets: dict[str, dict[str, Any]],
              split_ts_ms: int,
              *, sweep_name: str = "sweep") -> dict[str, Any]:
    """Enumerate + backtest every spec IN-SAMPLE. Returns all results + honest
    N-trial count. Writes a compact log (no holdout access)."""
    specs = build_specs(spec_factory, param_grid)
    results = []
    for spec in specs:
        res = backtest_spec_in_sample(spec, datasets, split_ts_ms)
        results.append(res)
    n_trials = len(results)
    out = {
        "sweep_name": sweep_name,
        "n_trials": n_trials,          # honest count for multiple-testing correction
        "split_ts_ms": split_ts_ms,
        "results": results,
    }
    _write_sweep_log(out)
    return out


def _write_sweep_log(sweep: dict[str, Any]) -> None:
    SWEEP_DIR.mkdir(parents=True, exist_ok=True)
    path = SWEEP_DIR / f"{sweep['sweep_name']}_insample.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for r in sweep["results"]:
            row = {"spec_id": r["spec_id"], "spec": r["spec"],
                   "in_sample": r["in_sample"], "per_symbol_trades": r["per_symbol_trades"]}
            fh.write(json.dumps(row, default=str) + "\n")
