"""llm_trader scorecard — single source of truth for performance metrics.

Plan: plans/260702-0900-llm-trader-core-upgrade (checklist items #5-#9).

WHY this module exists: llm_trader is forward-only (no backtester), so this
scorecard IS its out-of-sample evaluation. Every number must be honest and
reproducible — the verdict ladder never says "edge proven", only PROMISING
at best, because N will stay small for a long time and a lucky streak looks
identical to skill until the statistics say otherwise.

Design rules (integrator depends on these):
- Pure functions, no I/O, no network, no clock reads.
- Deterministic: all randomness flows through random.Random(seed), default
  seed=7 — the same closed-trade list ALWAYS produces the same CI / p-value,
  so scorecard.json diffs are meaningful and tests can assert exact equality.
- Input is the plain closed-trade record list written by llm_trader.resolve
  (keys used here: net, r, reason). Malformed rows are DROPPED (fail-closed),
  never coerced into fake trades: a row only counts toward n when BOTH its
  net and r parse to finite floats. A scorecard must never take the trading
  loop down, but it must also never let junk rows (schema drift, truncated
  JSONL writes, NaN round-trips) satisfy the min-trades gate or poison the
  statistics — NaN comparisons are silently False, which biases every
  downstream test in the OPTIMISTIC direction.
"""
from __future__ import annotations

import math
import random
from typing import Any

DEFAULT_SEED = 7
DEFAULT_ITERS = 5000
DEFAULT_ALPHA = 0.05
DEFAULT_MIN_TRADES = 30


def _f(value: Any, default: float = 0.0) -> float:
    """Coerce to FINITE float; malformed/non-finite values coerce to default.

    WHY the isfinite gate: float('nan') and float('inf') pass a bare float()
    call, and NaN then poisons every statistic OPTIMISTICALLY — nan < 0 is
    False (disarms the NEGATIVE verdict), total/n >= nan is False (drives the
    permutation p-value toward its minimum). Rejecting non-finite here keeps
    the fail-soft contract honest.
    """
    try:
        if value is None:
            return default
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _finite(value: Any) -> float | None:
    """Strict parse: finite float, or None. No default masking — used to
    decide whether a row is a real trade at all (fail-closed row filter)."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _valid_trades(closed: list[dict]) -> list[dict]:
    """FAIL-CLOSED row filter: keep only rows where net AND r are finite floats.

    WHY drop instead of coerce: n gates the verdict ladder (min_trades). If a
    corrupted row coerced to net=0/r=0 and still counted, garbage rows could
    satisfy the 30-trade minimum and mint PROMISING with fewer than 30 real
    samples — the exact failure the gate exists to prevent. Dropped rows
    shrink n, so the gate always demands 30 REAL trades.
    """
    out = []
    for t in closed:
        if not isinstance(t, dict):
            continue
        if _finite(t.get("net")) is None or _finite(t.get("r")) is None:
            continue
        out.append(t)
    return out


def _all_finite(rs: list[float]) -> bool:
    """True only when every element parses to a finite float (fail-closed)."""
    try:
        return all(math.isfinite(float(x)) for x in rs)
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------
def basic_metrics(closed: list[dict]) -> dict:
    """Compute descriptive stats from closed-trade records (net & r keys).

    Conventions (pessimistic where a choice exists):
    - win = net > 0; a zero-net trade counts as a loss for streak purposes
      (it paid fees for nothing — calling it a win would flatter the bot).
    - profit_factor = gross wins / abs(gross losses); inf-safe: with zero
      losses it is float('inf') only when there are actual wins, else 0.0.
    - sharpe_trade = mean(r) / population std(r); 0.0 when n < 2 or std == 0
      (a single trade or a constant series carries no dispersion signal).
    - max_dd_usd = peak-to-trough of the cumulative NET curve (starts at 0),
      returned as a positive magnitude in USD.
    - malformed rows (net or r missing / non-numeric / NaN / inf) are DROPPED:
      they do not count toward n, so they can never satisfy the min-trades
      gate. n_dropped reports how many were excluded (audit visibility).
    """
    valid = _valid_trades(closed)
    n = len(valid)
    n_dropped = len(closed) - n
    nets = [float(t["net"]) for t in valid]
    rs = [float(t["r"]) for t in valid]

    wins = sum(1 for x in nets if x > 0)
    win_rate = wins / n if n else 0.0
    mean_r = sum(rs) / n if n else 0.0
    expectancy_usd = sum(nets) / n if n else 0.0

    gross_win = sum(x for x in nets if x > 0)
    gross_loss = sum(x for x in nets if x < 0)  # <= 0
    if gross_loss < 0:
        profit_factor = gross_win / abs(gross_loss)
    else:
        profit_factor = float("inf") if gross_win > 0 else 0.0

    if n < 2:
        sharpe_trade = 0.0
    else:
        var = sum((x - mean_r) ** 2 for x in rs) / n  # population std
        std = math.sqrt(var)
        sharpe_trade = mean_r / std if std > 0 else 0.0

    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for x in nets:
        cum += x
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    max_win_streak = max_loss_streak = 0
    cur_win = cur_loss = 0
    for x in nets:
        if x > 0:
            cur_win += 1
            cur_loss = 0
        else:
            cur_loss += 1
            cur_win = 0
        max_win_streak = max(max_win_streak, cur_win)
        max_loss_streak = max(max_loss_streak, cur_loss)

    liq_count = sum(1 for t in valid if t.get("reason") == "liquidation")

    return {
        "n": n,
        "n_dropped": n_dropped,
        "wins": wins,
        "win_rate": round(win_rate, 4),
        "mean_r": round(mean_r, 4),
        "expectancy_usd": round(expectancy_usd, 4),
        "profit_factor": profit_factor if math.isinf(profit_factor) else round(profit_factor, 4),
        "sharpe_trade": round(sharpe_trade, 4),
        "max_dd_usd": round(max_dd, 4),
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "liq_count": liq_count,
    }


# ---------------------------------------------------------------------------
# statistics (deterministic seed=7)
# ---------------------------------------------------------------------------
def bootstrap_ci(rs: list[float], iters: int = DEFAULT_ITERS,
                 alpha: float = DEFAULT_ALPHA, seed: int = DEFAULT_SEED) -> tuple[float, float]:
    """Percentile-bootstrap CI for mean(rs).

    WHY bootstrap: trade R distributions are fat-tailed and small-N; a normal
    approximation would produce false confidence exactly where it hurts most.
    Deterministic via random.Random(seed) so the scorecard is reproducible.
    Empty input returns (0.0, 0.0) — no data, no interval. Any non-finite
    input (NaN/inf) also returns (0.0, 0.0): NaN makes the sorted percentile
    positions implementation-order-dependent and the resulting "interval"
    meaningless, so we fail closed instead of emitting garbage bounds.
    """
    n = len(rs)
    if n == 0 or iters <= 0 or not _all_finite(rs):
        return (0.0, 0.0)
    rng = random.Random(seed)
    means = []
    for _ in range(iters):
        total = 0.0
        for _ in range(n):
            total += rs[rng.randrange(n)]
        means.append(total / n)
    means.sort()
    lo_idx = max(0, min(iters - 1, int((alpha / 2.0) * iters)))
    hi_idx = max(0, min(iters - 1, int((1.0 - alpha / 2.0) * iters) - 1))
    return (means[lo_idx], means[hi_idx])


def permutation_pvalue(rs: list[float], iters: int = DEFAULT_ITERS,
                       seed: int = DEFAULT_SEED) -> float:
    """One-sided sign-flip permutation p-value: P(mean_flipped >= mean_obs).

    Null hypothesis: R outcomes are symmetric noise around 0 (no edge), so
    each trade's sign is a coin flip. We flip signs at random and count how
    often luck alone matches the observed mean. Add-one smoothing
    ((count+1)/(iters+1)) keeps p > 0 — we can never claim impossibility from
    a finite simulation. Empty input returns 1.0 (no evidence of anything).
    Any non-finite input (NaN/inf) also returns 1.0 (fail closed): a NaN mean
    makes `total/n >= mean_obs` False on every iteration, which would report
    the MINIMUM possible p-value — maximal fake significance — for garbage.
    """
    n = len(rs)
    if n == 0 or iters <= 0 or not _all_finite(rs):
        return 1.0
    mean_obs = sum(rs) / n
    rng = random.Random(seed)
    count = 0
    for _ in range(iters):
        total = 0.0
        for x in rs:
            total += x if rng.random() < 0.5 else -x
        if total / n >= mean_obs:
            count += 1
    return (count + 1) / (iters + 1)


# ---------------------------------------------------------------------------
# verdict ladder (honest — never "proven")
# ---------------------------------------------------------------------------
def verdict(metrics: dict, ci: tuple, pvalue: float,
            min_trades: int = DEFAULT_MIN_TRADES) -> dict:
    """Map stats to an honest verdict {code, detail}.

    Ladder (exact order, first match wins):
      1. n < min_trades              -> INSUFFICIENT_DATA
      2. mean_r < 0                  -> NEGATIVE
      3. ci_low > 0 AND p < 0.05
         AND n >= min_trades         -> PROMISING (forward-continue only)
      4. otherwise                   -> INCONCLUSIVE

    WHY never "proven": at these sample sizes a hot streak and a real edge
    are statistically indistinguishable; PROMISING only licenses continued
    forward paper trading, never live capital.
    """
    n = int(metrics.get("n", 0) or 0)
    mean_r = _f(metrics.get("mean_r"))
    ci_low = _f(ci[0]) if len(ci) >= 1 else 0.0

    if n < min_trades:
        return {
            "code": "INSUFFICIENT_DATA",
            "detail": f"only {n} closed trades (< {min_trades}); no statistical claim possible yet",
        }
    if mean_r < 0:
        return {
            "code": "NEGATIVE",
            "detail": f"mean R {mean_r:.4f} < 0 over {n} trades; system is losing, not edge-hunting",
        }
    if ci_low > 0 and pvalue < 0.05 and n >= min_trades:
        return {
            "code": "PROMISING",
            "detail": (
                f"mean R {mean_r:.4f}, 95% CI low {ci_low:.4f} > 0, p={pvalue:.4f} < 0.05 "
                f"over {n} trades — forward-continue, NOT proven edge"
            ),
        }
    return {
        "code": "INCONCLUSIVE",
        "detail": (
            f"mean R {mean_r:.4f} over {n} trades but CI low {ci_low:.4f} and p={pvalue:.4f} "
            f"do not separate skill from luck"
        ),
    }


# ---------------------------------------------------------------------------
# top-level scorecard
# ---------------------------------------------------------------------------
def scorecard(closed: list[dict], benchmark: dict | None = None) -> dict:
    """Full deterministic scorecard from closed-trade records.

    benchmark, when provided by the integrator, is the pre-computed
    {btc_ret_pct, agent_ret_pct, excess_pct} buy-hold comparison; this module
    stays pure and only passes it through. No timestamps here on purpose —
    same input must always yield the byte-identical output.

    rs is built from the SAME fail-closed row filter as basic_metrics, so
    metrics["n"] == len(rs) always: the CI/p-value are computed over exactly
    the trades that were allowed to count toward the min-trades gate.
    """
    metrics = basic_metrics(closed)
    rs = [float(t["r"]) for t in _valid_trades(closed)]
    ci = bootstrap_ci(rs)
    pvalue = permutation_pvalue(rs)
    v = verdict(metrics, ci, pvalue)
    return {
        "metrics": metrics,
        "ci_mean_r": [round(ci[0], 4), round(ci[1], 4)],
        "pvalue": round(pvalue, 6),
        "verdict": v,
        "benchmark": benchmark,
    }
