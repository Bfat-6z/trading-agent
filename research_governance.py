"""Anti-overfit backbone — the spine that lets an auto-generating meta-loop be
trusted (HARNESS meta-phase0 backbone).

Three guards a nightly spec generator cannot be trusted without:
1. GLOBAL cumulative trial count — the DSR multiple-testing penalty must reflect
   EVERY spec ever tested (persisted in research_ledger.jsonl), not reset per run.
   The more we search across all time, the higher the bar.
2. HOLDOUT BUDGET — the sealed holdout may be peeked ONCE per spec, ever. Track
   which spec_ids have peeked; refuse a second peek. Burning the holdout by
   re-testing near-duplicates is how overfit sneaks in.
3. RUNTIME NO-LOOKAHEAD GUARD — every composed (possibly self-generated) spec is
   re-checked for repaint: recompute its mask on the series truncated to 0..i and
   compare bit i to the full-series mask. A leaker is rejected BEFORE it can count
   as a valid trial or reach the holdout.

Paper-only; nothing here can place an order.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import backtest_chart_signal as cs
import research_ledger as rl
import strategy_blocks as sb
import strategy_compiler as sc

ROOT = Path(__file__).resolve().parent
HOLDOUT_BUDGET_PATH = ROOT / "state" / "agent_memory" / "holdout_budget.jsonl"

# blocks that require an enriched (CVD/funding) df; a holdout peek on these MUST
# use enriched precomputed dfs, never the plain backtest_symbol fallback.
ORDER_FLOW_BLOCKS = {"cvd_aggression", "cvd_reversal", "funding_extreme_contrarian", "buy_frac_extreme"}


# --- 1. GLOBAL cumulative trial count -----------------------------------------

def global_trial_count(ledger_path: Path = rl.LEDGER_PATH) -> int:
    """Sum n_trials across every ledger row = total distinct specs ever evaluated.
    Feeds the DSR so the multiple-testing penalty is cumulative, not per-run."""
    total = 0
    for row in rl.load_rows(ledger_path):
        try:
            total += int(row.get("n_trials", 0) or 0)
        except Exception:
            continue
    return total


# --- 2. Holdout budget / peek-once --------------------------------------------

def _holdout_digest(datasets_or_split: Any) -> str:
    """A stable id for 'which holdout' — here keyed by the split timestamp so the
    same sealed window is recognized across runs."""
    return hashlib.sha256(str(datasets_or_split).encode("utf-8")).hexdigest()[:12]


def peeked_specs(path: Path = HOLDOUT_BUDGET_PATH) -> set[str]:
    if not path.exists():
        return set()
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.add(json.loads(line)["spec_id"])
            except Exception:
                continue
    return out


def can_peek_holdout(spec_id: str, path: Path = HOLDOUT_BUDGET_PATH) -> bool:
    return spec_id not in peeked_specs(path)


def record_holdout_peek(spec_id: str, holdout_key: str, stamped_at: str,
                        path: Path = HOLDOUT_BUDGET_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"spec_id": spec_id, "holdout_key": holdout_key,
                             "stamped_at": stamped_at}) + "\n")


# --- 3. Runtime no-lookahead / repaint guard on composed specs ----------------

def assert_no_lookahead(spec: dict[str, Any], df, df_1h, *, cuts: tuple[int, ...] | None = None) -> dict[str, Any]:
    """Recompute the spec's mask on the series truncated to 0..cut and compare the
    boolean at `cut` to the full-series mask. Any change = the spec peeks the
    future (repaint) = reject. Returns {clean, mismatches, checks}."""
    full = sc.compute_mask(spec, df, df_1h)
    n = len(df)
    if cuts is None:
        # spread checks across the back half where signals are live
        base = max(sc.REQUIRED_WARMUP + 5, n // 2)
        cuts = tuple(c for c in (base, int(n * 0.7), int(n * 0.85), n - 2) if 0 < c < n)
    mismatches = 0
    checks = 0
    htf_ts = df_1h["ts_ms"].to_numpy() if (df_1h is not None and "ts_ms" in getattr(df_1h, "columns", [])) else None
    for cut in cuts:
        if cut <= 0 or cut >= n:
            continue
        df_t = df.iloc[: cut + 1].copy()
        # truncate HTF to only bars closed by the cut bar's close time (causal)
        if htf_ts is not None:
            cut_ts = int(df.iloc[cut]["ts_ms"])
            df1_t = df_1h[df_1h["ts_ms"] <= cut_ts]
        else:
            df1_t = df_1h
        m_t = sc.compute_mask(spec, df_t, df1_t)
        checks += 1
        if bool(m_t.iloc[cut]) != bool(full.iloc[cut]):
            mismatches += 1
    return {"clean": mismatches == 0, "mismatches": mismatches, "checks": checks}


def guard_spec_across_symbols(spec: dict[str, Any], precomputed: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Run the no-lookahead guard on a spec over every symbol's df. Clean only if
    all symbols are clean."""
    total_mis = 0
    total_checks = 0
    for sym, p in precomputed.items():
        r = assert_no_lookahead(spec, p["df"], p.get("df_1h"))
        total_mis += r["mismatches"]
        total_checks += r["checks"]
    return {"clean": total_mis == 0, "mismatches": total_mis, "checks": total_checks}


def spec_has_order_flow(spec: dict[str, Any]) -> bool:
    entry = spec.get("entry") or {}
    for grp in ("all", "any"):
        for b in (entry.get(grp) or []):
            if b.get("block") in ORDER_FLOW_BLOCKS:
                return True
    return False
