"""Phase 3 — prove-or-kill backtest driver with honest statistics.

Runs the chart setup across the frozen symbol universe over cached history,
splits into in-sample (freeze params, count trials) and a SEALED holdout
(one peek), and applies the binding KILL criterion with block-bootstrap CI
(returns are NOT iid — 9 correlated symbols, overlapping regimes — so a plain
CI is too narrow). Deterministic: no wall-clock, no network (reads cached bars).
"""
from __future__ import annotations

import math
from typing import Any

import backtest_chart_signal as cs

# KILL thresholds (binding — see phase3-design.md)
MIN_HOLDOUT_TRADES = 400
MIN_TRADES_PER_SYMBOL = 25
MIN_PROFIT_FACTOR = 1.2
MAX_DRAWDOWN_R = 25.0          # in R units of cumulative equity
MIN_POSITIVE_SYMBOLS = 3
MIN_TSTAT = 3.0               # deflated / conservative
BOOTSTRAP_RESAMPLES = 10000
BLOCK_SIZE = 20              # block bootstrap to respect autocorrelation


def _seeded_rand(seed: int):
    """Tiny deterministic LCG (Math.random/np.random forbidden for replay)."""
    state = {"s": seed & 0xFFFFFFFF}
    def rnd() -> float:
        state["s"] = (1103515245 * state["s"] + 12345) & 0x7FFFFFFF
        return state["s"] / 0x7FFFFFFF
    return rnd


def block_bootstrap_ci(rs: list[float], *, resamples: int = BOOTSTRAP_RESAMPLES, block: int = BLOCK_SIZE, seed: int = 42) -> dict[str, float]:
    """Block-bootstrap 95% CI of mean R. Blocks preserve autocorrelation so the
    CI is not falsely narrow."""
    n = len(rs)
    if n == 0:
        return {"mean": 0.0, "lo95": 0.0, "hi95": 0.0, "n": 0}
    rnd = _seeded_rand(seed)
    means = []
    n_blocks = max(1, n // block)
    for _ in range(resamples):
        acc = 0.0; cnt = 0
        for _b in range(n_blocks):
            start = int(rnd() * max(1, n - block))
            for k in range(block):
                idx = start + k
                if idx < n:
                    acc += rs[idx]; cnt += 1
        means.append(acc / cnt if cnt else 0.0)
    means.sort()
    lo = means[int(0.025 * len(means))]
    hi = means[int(0.975 * len(means))]
    return {"mean": sum(rs) / n, "lo95": lo, "hi95": hi, "n": n}


def metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {"trades": 0}
    rs = [t["r_multiple"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    # equity curve in R, max drawdown
    eq = 0.0; peak = 0.0; mdd = 0.0
    for r in rs:
        eq += r; peak = max(peak, eq); mdd = max(mdd, peak - eq)
    ci = block_bootstrap_ci(rs)
    sd = (sum((r - ci["mean"]) ** 2 for r in rs) / len(rs)) ** 0.5 if len(rs) > 1 else 0.0
    tstat = (ci["mean"] / (sd / math.sqrt(len(rs)))) if sd > 0 else 0.0
    sharpe = (ci["mean"] / sd) if sd > 0 else 0.0   # per-trade Sharpe (mean/std of R)
    return {
        "trades": len(rs),
        "win_rate": len(wins) / len(rs),
        "expectancy_r": ci["mean"],
        "expectancy_lo95": ci["lo95"],
        "expectancy_hi95": ci["hi95"],
        "profit_factor": pf,
        "total_r": sum(rs),
        "max_drawdown_r": mdd,
        "tstat": tstat,
        "std_r": sd,
        "sharpe": sharpe,
    }


def run_prove_or_kill(datasets: dict[str, dict[str, Any]], *, split_ts_ms: int) -> dict[str, Any]:
    """datasets: {symbol: {"bars_5m":[...], "bars_1h":[...], "quote_volume_24h":float}}.
    split_ts_ms: bars with close < split -> in-sample; >= split -> holdout.
    Returns in-sample + holdout metrics and the KILL verdict."""
    in_sample_all: list[dict[str, Any]] = []
    holdout_all: list[dict[str, Any]] = []
    per_symbol_holdout: dict[str, list[dict[str, Any]]] = {}
    for sym, d in datasets.items():
        b5, b1, qv = d["bars_5m"], d["bars_1h"], d["quote_volume_24h"]
        ins = cs.backtest_symbol(b5, b1, qv, end_ts_ms=split_ts_ms)
        hold = cs.backtest_symbol(b5, b1, qv, start_ts_ms=split_ts_ms)
        for t in ins: t["symbol"] = sym
        for t in hold: t["symbol"] = sym
        in_sample_all.extend(ins)
        holdout_all.extend(hold)
        per_symbol_holdout[sym] = hold

    ins_m = metrics(in_sample_all)
    hold_m = metrics(holdout_all)
    positive_symbols = sum(1 for sym, ts in per_symbol_holdout.items()
                           if ts and sum(t["r_multiple"] for t in ts) > 0)
    per_symbol_counts = {sym: len(ts) for sym, ts in per_symbol_holdout.items()}

    reasons: list[str] = []
    if hold_m.get("trades", 0) < MIN_HOLDOUT_TRADES:
        reasons.append(f"insufficient_holdout_trades:{hold_m.get('trades',0)}<{MIN_HOLDOUT_TRADES}:INCONCLUSIVE")
    if any(c < MIN_TRADES_PER_SYMBOL for c in per_symbol_counts.values()):
        reasons.append(f"symbol_below_min_trades:{per_symbol_counts}")
    if hold_m.get("expectancy_lo95", -1) <= 0:
        reasons.append(f"expectancy_lo95<=0:{hold_m.get('expectancy_lo95')}")
    if hold_m.get("profit_factor", 0) < MIN_PROFIT_FACTOR:
        reasons.append(f"profit_factor<{MIN_PROFIT_FACTOR}:{hold_m.get('profit_factor')}")
    if hold_m.get("max_drawdown_r", 999) > MAX_DRAWDOWN_R:
        reasons.append(f"max_drawdown>{MAX_DRAWDOWN_R}R:{hold_m.get('max_drawdown_r')}")
    if positive_symbols < MIN_POSITIVE_SYMBOLS:
        reasons.append(f"positive_symbols<{MIN_POSITIVE_SYMBOLS}:{positive_symbols}")
    if hold_m.get("tstat", 0) < MIN_TSTAT:
        reasons.append(f"tstat<{MIN_TSTAT}:{hold_m.get('tstat')}")

    verdict = "PASS" if not reasons else "KILL"
    return {
        "verdict": verdict,
        "kill_reasons": reasons,
        "in_sample": ins_m,
        "holdout": hold_m,
        "holdout_positive_symbols": positive_symbols,
        "holdout_per_symbol_trades": per_symbol_counts,
        "split_ts_ms": split_ts_ms,
    }
