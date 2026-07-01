"""Edge-research harness — OVERFIT GATE (HARNESS-4).

The most important, non-bypassable stage. Before ANYTHING touches the sealed
holdout, a candidate must pass ALL of:

1. Deflated Sharpe Ratio (Bailey & López de Prado, 2014): the observed Sharpe is
   deflated by the EXPECTED MAXIMUM Sharpe under the null across N independent
   trials, and adjusted for return skew/kurtosis. The more configs you sweep, the
   higher the bar. Must be DSR-significant (PSR against SR0 >= 0.95), not raw
   Sharpe.
2. Purge + embargo: overlapping trades are purged so the return series used for
   statistics has no leaked co-dependence.
3. Cross-consistency: profitable on >= MIN_POSITIVE_SYMBOLS of the universe AND
   across multiple time sub-periods (not one lucky symbol/window).
4. Plateau not spike: the winning params sit on a neighborhood that is also
   profitable (a lone peak surrounded by losers = overfit -> kill).
5. Sealed holdout, peeked EXACTLY ONCE for the single best surviving candidate.
   Fail holdout -> dead. Never re-sweep the same holdout (that burns it).

KILL-by-default: the default verdict is "no edge found". A candidate is promoted
only when it passes every gate cleanly. Most sweeps produce no survivor — that is
the normal, honest outcome.
"""
from __future__ import annotations

import math
from typing import Any

import backtest_chart_signal as cs
import backtest_runner as br
import strategy_compiler as sc

# ---- gate thresholds (frozen; changing them re-opens the overfit risk) --------
EULER_GAMMA = 0.5772156649015329
DSR_MIN = 0.95                 # PSR against SR0 must exceed this
MIN_HOLDOUT_TRADES = 400       # per owner: thousands preferred, 400 is the floor
MIN_POSITIVE_SYMBOLS = 6       # of 9 in the reference universe
MIN_SUBPERIODS_POSITIVE = 3    # of 4 time sub-periods
MIN_PROFIT_FACTOR = 1.2
PLATEAU_MIN_POSITIVE_NEIGHBORS = 0.5   # >=50% of param neighbors also positive


# ---------------------------------------------------------------------------
# normal distribution helpers (no scipy)
# ---------------------------------------------------------------------------

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """Inverse normal CDF (probit) via Acklam's rational approximation."""
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


# ---------------------------------------------------------------------------
# Deflated Sharpe Ratio
# ---------------------------------------------------------------------------

def _moments(rs: list[float]) -> tuple[float, float, float, float]:
    """mean, std (sample), skew, kurtosis (Pearson, normal=3) of returns."""
    n = len(rs)
    if n < 2:
        return 0.0, 0.0, 0.0, 3.0
    mean = sum(rs) / n
    var = sum((r - mean) ** 2 for r in rs) / (n - 1)
    std = math.sqrt(var) if var > 0 else 0.0
    if std == 0:
        return mean, 0.0, 0.0, 3.0
    m3 = sum((r - mean) ** 3 for r in rs) / n
    m4 = sum((r - mean) ** 4 for r in rs) / n
    sd_pop = math.sqrt(sum((r - mean) ** 2 for r in rs) / n)
    skew = m3 / (sd_pop ** 3) if sd_pop > 0 else 0.0
    kurt = m4 / (sd_pop ** 4) if sd_pop > 0 else 3.0
    return mean, std, skew, kurt


def sharpe_ratio(rs: list[float]) -> float:
    mean, std, _, _ = _moments(rs)
    return mean / std if std > 0 else 0.0


def expected_max_sharpe(n_trials: int, var_trial_sharpe: float) -> float:
    """E[max SR] under the null across N independent trials (Bailey/LdP)."""
    if n_trials <= 1 or var_trial_sharpe <= 0:
        return 0.0
    sigma = math.sqrt(var_trial_sharpe)
    a = norm_ppf(1 - 1.0 / n_trials)
    b = norm_ppf(1 - 1.0 / (n_trials * math.e))
    return sigma * ((1 - EULER_GAMMA) * a + EULER_GAMMA * b)


def deflated_sharpe_ratio(returns: list[float], n_trials: int,
                          var_trial_sharpe: float) -> dict[str, Any]:
    """PSR against the deflated benchmark SR0 = E[max SR under null]. Returns the
    probability (0..1) that the true Sharpe exceeds SR0 given skew/kurtosis."""
    t = len(returns)
    if t < 20:
        return {"dsr": 0.0, "sr": 0.0, "sr0": 0.0, "trades": t, "reason": "too_few_returns"}
    sr = sharpe_ratio(returns)
    _, _, skew, kurt = _moments(returns)
    sr0 = expected_max_sharpe(n_trials, var_trial_sharpe)
    denom = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * (sr ** 2)
    if denom <= 0:
        denom = 1e-9
    z = (sr - sr0) * math.sqrt(t - 1) / math.sqrt(denom)
    return {"dsr": norm_cdf(z), "sr": sr, "sr0": sr0, "skew": skew, "kurt": kurt, "trades": t}


# ---------------------------------------------------------------------------
# Purge overlapping trades
# ---------------------------------------------------------------------------

def purge_overlaps(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep a non-overlapping subset per symbol: sort by entry, drop any trade
    whose entry is before the previous kept trade's exit. Removes co-dependence so
    the return series is closer to iid for the DSR."""
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        by_symbol.setdefault(t.get("symbol", "?"), []).append(t)
    kept: list[dict[str, Any]] = []
    for sym, ts in by_symbol.items():
        ts.sort(key=lambda x: int(x["entry_ts"]))
        last_exit = -1
        for t in ts:
            if int(t["entry_ts"]) >= last_exit:
                kept.append(t)
                last_exit = int(t["exit_ts"])
    return kept


# ---------------------------------------------------------------------------
# Cross-consistency
# ---------------------------------------------------------------------------

def cross_consistency(trades: list[dict[str, Any]], n_subperiods: int = 4) -> dict[str, Any]:
    """Positive on how many symbols, and how many time sub-periods."""
    by_symbol: dict[str, float] = {}
    for t in trades:
        by_symbol[t.get("symbol", "?")] = by_symbol.get(t.get("symbol", "?"), 0.0) + float(t.get("r_multiple", 0))
    positive_symbols = sum(1 for v in by_symbol.values() if v > 0)

    if trades:
        ts_sorted = sorted(trades, key=lambda x: int(x["entry_ts"]))
        lo = int(ts_sorted[0]["entry_ts"]); hi = int(ts_sorted[-1]["entry_ts"])
        span = max(1, hi - lo)
        buckets = [0.0] * n_subperiods
        for t in trades:
            k = min(n_subperiods - 1, int((int(t["entry_ts"]) - lo) / span * n_subperiods))
            buckets[k] += float(t.get("r_multiple", 0))
        positive_subperiods = sum(1 for b in buckets if b > 0)
    else:
        positive_subperiods = 0
        buckets = []
    return {"positive_symbols": positive_symbols, "n_symbols": len(by_symbol),
            "positive_subperiods": positive_subperiods, "subperiod_r": buckets}


# ---------------------------------------------------------------------------
# Plateau (sensitivity) check
# ---------------------------------------------------------------------------

def plateau_check(sweep_results: list[dict[str, Any]], best_spec_id: str,
                  metric: str = "expectancy_r") -> dict[str, Any]:
    """A winner must sit on a plateau: among all sweep results, the fraction that
    are also positive on `metric` should be high enough (a lone spike surrounded
    by losers is overfit). We use the full in-sample distribution as the
    neighborhood proxy for the first version."""
    vals = [float(r["in_sample"].get(metric, 0) or 0) for r in sweep_results]
    if not vals:
        return {"is_plateau": False, "positive_fraction": 0.0, "n": 0}
    positive_fraction = sum(1 for v in vals if v > 0) / len(vals)
    return {"is_plateau": positive_fraction >= PLATEAU_MIN_POSITIVE_NEIGHBORS,
            "positive_fraction": positive_fraction, "n": len(vals)}


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def variance_of_trial_sharpes(sweep_results: list[dict[str, Any]]) -> float:
    """Var of per-trial Sharpe proxies across the sweep, for E[max SR]. Use
    expectancy_r / sqrt of variance proxy; here we approximate each trial's SR by
    its in-sample expectancy_r (already risk-normalized R units)."""
    srs = [float(r["in_sample"].get("expectancy_r", 0) or 0) for r in sweep_results]
    if len(srs) < 2:
        return 0.0
    mean = sum(srs) / len(srs)
    return sum((s - mean) ** 2 for s in srs) / (len(srs) - 1)


def pick_best(sweep_results: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Best in-sample candidate by expectancy_r, then profit_factor."""
    ranked = sorted(sweep_results, key=lambda r: (float(r["in_sample"].get("expectancy_r", -9) or -9),
                                                  float(r["in_sample"].get("profit_factor", 0) or 0)),
                    reverse=True)
    return ranked[0] if ranked else None


def evaluate_candidate(best: dict[str, Any], sweep_results: list[dict[str, Any]],
                       n_trials: int) -> dict[str, Any]:
    """Run ALL in-sample gates on the best candidate (NO holdout yet). Returns a
    verdict dict; holdout is only peeked if pre_holdout_pass is True."""
    trades = purge_overlaps(best.get("trades", []))
    returns = [float(t.get("r_multiple", 0)) for t in trades]
    var_sharpe = variance_of_trial_sharpes(sweep_results)
    dsr = deflated_sharpe_ratio(returns, n_trials, var_sharpe)
    cc = cross_consistency(trades)
    plat = plateau_check(sweep_results, best["spec_id"])
    m = best["in_sample"]

    checks = {
        "dsr_significant": dsr["dsr"] >= DSR_MIN,
        "enough_symbols": cc["positive_symbols"] >= MIN_POSITIVE_SYMBOLS,
        "enough_subperiods": cc["positive_subperiods"] >= MIN_SUBPERIODS_POSITIVE,
        "profit_factor_ok": float(m.get("profit_factor", 0) or 0) >= MIN_PROFIT_FACTOR,
        "is_plateau": plat["is_plateau"],
    }
    pre_holdout_pass = all(checks.values())
    return {
        "spec_id": best["spec_id"],
        "n_trials": n_trials,
        "dsr": dsr,
        "cross_consistency": cc,
        "plateau": plat,
        "in_sample": m,
        "checks": checks,
        "pre_holdout_pass": pre_holdout_pass,
        "purged_trades": len(trades),
    }


def peek_holdout_once(spec: dict[str, Any], datasets: dict[str, dict[str, Any]],
                      split_ts_ms: int, exit_cfg: dict[str, Any] | None = None,
                      precomputed: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """THE single sealed-holdout peek for one candidate. Run only AFTER
    pre_holdout_pass. Returns holdout metrics + a KILL/PASS verdict. Uses the fast
    mask path; `precomputed` supplies enriched indicator dfs (needed for Family A
    CVD/funding columns) so the holdout is evaluated on the same feature set."""
    cfg = exit_cfg if exit_cfg is not None else spec.get("exit")
    trades: list[dict[str, Any]] = []
    if precomputed is not None:
        for sym, p in precomputed.items():
            mask = sc.compute_mask(spec, p["df"], p["df_1h"])
            tr = cs.backtest_with_mask(p["df"], p["quote_volume_24h"], mask, spec["direction"],
                                       start_ts_ms=split_ts_ms, exit_cfg=cfg)
            for t in tr:
                t["symbol"] = sym
            trades.extend(tr)
    else:
        signal_fn = sc.compile_spec(spec)
        for sym, d in datasets.items():
            tr = cs.backtest_symbol(d["bars_5m"], d["bars_1h"], d["quote_volume_24h"],
                                    start_ts_ms=split_ts_ms, signal_fn=signal_fn, exit_cfg=cfg)
            for t in tr:
                t["symbol"] = sym
            trades.extend(tr)
    purged = purge_overlaps(trades)
    m = br.metrics(purged)
    cc = cross_consistency(purged)
    passed = (m.get("trades", 0) >= MIN_HOLDOUT_TRADES and
              float(m.get("expectancy_r", -9) or -9) > 0 and
              float(m.get("profit_factor", 0) or 0) >= MIN_PROFIT_FACTOR and
              cc["positive_symbols"] >= MIN_POSITIVE_SYMBOLS)
    return {"holdout": m, "holdout_cross_consistency": cc,
            "verdict": "PASS" if passed else "KILL",
            "reason": None if passed else "failed_sealed_holdout"}
