"""Growth-optimal position sizing for the PROVEN-ONLY mechanical trader.

Derived, not hardcoded (owner: 'the LOGIC must be correct'). Replaces the old
binary-Kelly _kelly_size_pct, which a multi-agent risk review found DANGEROUS —
correlation-blind, binary-misspecified, no estimation-error/drawdown control.

The correct procedure (per the review), sizing the FIRING CLUSTER as a batch
because correlation is a property of the cluster, not a single method:
  1. Empirical Kelly on the method's real per-trade net% distribution (TP/SL/
     timeout+fees is a continuous smear, NOT binary): E* = argmax_E mean(ln(1+E*r)).
  2. Estimation-error shrinkage: size on the lower-confidence-bound mean
     m_lcb = m_hat - z*SE (z=1, or 1.5 for thin/marginal methods). m_lcb<=0 -> skip.
  3. Correlation divisor: k simultaneous correlated fires are ~N_eff = k/(1+(k-1)*rho)
     independent bets; divide each exposure by (1+(k-1)*rho).
  4. Drawdown governor: multiply by c_dd=0.30 (fractional-Kelly law P(ever halve)
     ~= alpha^((2-c)/c) ~ 2%), keeping ~51% of full-Kelly growth.
  5. Convert margin = c_dd*E/(corr_div*lev), per-position cap, NO floor (skip small),
     then a HARD aggregate exposure cap so one synchronized dump bar is survivable.

Everything reads the persisted per-trade net% arrays; nothing is hand-picked
except the justified safety coefficients. PAPER/OFFLINE: pure math, no orders.
"""
from __future__ import annotations

import os
from math import isfinite, sqrt
from typing import Any

import numpy as np

# Safety coefficients. Aggressiveness (owner 'danh be qua' — accepts more risk) is
# env-tunable; the aggregate cap remains the ruin backstop no matter how high C_DD goes.
#   C_DD 0.30 -> P(ever halve)~2%  |  0.50 (half-Kelly) ~12%  |  0.70 ~25%.
C_DD = float(os.environ.get("MECH_C_DD", "0.50"))            # drawdown governor / Kelly fraction
Z_LCB = 1.0             # lower-confidence-bound sigmas (bumped to 1.5 for thin methods)
Z_LCB_THIN = 1.5
THIN_N = 500            # oos_n below this (or p>0.01) -> extra shrinkage
RHO_DEFAULT = 0.7      # assume high correlation when co-fire data is thin (safe side)
PER_POS_CAP = float(os.environ.get("MECH_PER_POS_CAP", "0.25"))   # max margin fraction per position
GROSS_EXP_CAP = float(os.environ.get("MECH_GROSS_EXP_CAP", "3.0"))  # max sum(notional/equity); a ~12% synced gap costs <=~36% equity
MIN_MARGIN = 0.01      # below this -> skip (NO hard minimum-size floor)
MIN_TRADES = 30        # untrusted sample -> don't fire
# Codex adversarial review (2026-07-05): survivor_distributions come from the SAME OOS
# slice that SELECTED the method, so its mean is winner-biased — sizing empirical Kelly
# on it over-bets a method that merely won the selection lottery (worse now that C_DD
# and caps are raised). Fix: haircut exposure HARD until the method is confirmed on
# truly out-of-sample LIVE forward-test data (which carries no selection/grid/regime
# bias). d["forward_confirmed"]=True (set by the caller from the shadow ledger) lifts it.
SELECTION_HAIRCUT = float(os.environ.get("MECH_SELECTION_HAIRCUT", "0.5"))  # unconfirmed edge -> half size


def kelly_exposure(r: np.ndarray) -> float:
    """Full-Kelly EXPOSURE (notional/equity) = argmax_E mean(ln(1+E*r)) via bisection
    on g'(E)=mean(r/(1+E*r)) (strictly decreasing). r = per-trade net% on NOTIONAL
    (leverage-invariant). Returns 0 if no positive-mean edge."""
    r = np.asarray(r, dtype=float)
    if r.size == 0 or float(np.mean(r)) <= 0:
        return 0.0
    worst = min(float(r.min()), -1e-9)          # most negative trade
    lo, hi = 0.0, 0.999 / (-worst)              # keep 1 + E*r_k > 0 for all k
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if float(np.mean(r / (1.0 + mid * r))) > 0:
            lo = mid
        else:
            hi = mid
    e = 0.5 * (lo + hi)
    return e if isfinite(e) and e > 0 else 0.0


def _pair_rho(dists: dict[str, np.ndarray], a: str, b: str) -> float | None:
    """Best-effort correlation proxy between two methods from their trade-return
    distributions (real per-bar co-fire matrix not persisted -> use RHO_DEFAULT).
    All armed methods are LONG mean-reversion firing into dumps, so the default
    high rho is the safe assumption."""
    return None


def size_fires(firing: list[tuple[str, str]], dists: dict[str, dict[str, Any]],
               lev: int = 10) -> list[dict[str, Any]]:
    """Batch-size the fires in ONE bar. `firing` = [(coin, method_id), ...].
    `dists`[method_id] = {"net": [...per-trade net% on notional...], "n": oos_n,
    "pvalue": p}. Returns [{coin, method, margin_pct, exposure}] after all five
    steps; the correlation divisor + aggregate cap make the CLUSTER the constraint."""
    if not firing:
        return []
    k = len(firing)
    # cluster correlation (equicorrelation model). Real co-fire matrix not stored ->
    # RHO_DEFAULT (safe: these longs move together in a dump).
    if k > 1:
        prs = []
        for i, a in enumerate(firing):
            for b in firing[i + 1:]:
                rp = _pair_rho(dists, a[1], b[1])
                prs.append(RHO_DEFAULT if rp is None else rp)
        rho_bar = float(np.clip(np.mean(prs), 0.0, 0.99)) if prs else RHO_DEFAULT
    else:
        rho_bar = 0.0
    corr_div = 1.0 + (k - 1) * rho_bar

    out = []
    for coin, mid in firing:
        d = dists.get(mid) or {}
        r = np.asarray(d.get("net") or [], dtype=float)
        if r.size < MIN_TRADES:
            continue
        n = int(d.get("n") or r.size)
        p = d.get("pvalue")
        m_hat = float(np.mean(r))
        s = float(np.std(r, ddof=1))
        if s <= 0:
            continue
        z = Z_LCB_THIN if (n < THIN_N or (p is not None and p > 0.01)) else Z_LCB
        se = s / sqrt(max(n, 1))
        m_lcb = m_hat - z * se
        if m_lcb <= 0:                                   # no edge survives shrinkage
            continue
        r_shift = r - (m_hat - m_lcb)                    # pessimistic mean, real variance/tail
        e_full = kelly_exposure(r_shift)
        if e_full <= 0:
            e_full = m_lcb / (s * s + m_lcb * m_lcb)     # mean/var fallback
        # selection-bias haircut: full size only once the edge is confirmed on live
        # forward-test data (no selection/grid/regime bias); else half (Codex review).
        hair = 1.0 if d.get("forward_confirmed") else SELECTION_HAIRCUT
        e = C_DD * e_full * hair / corr_div              # drawdown governor + correlation + selection haircut
        margin = min(e / lev, PER_POS_CAP)
        if margin < MIN_MARGIN:
            continue
        out.append({"coin": coin, "method": mid, "exposure": margin * lev,
                    "margin_pct": round(100 * margin, 2)})

    # hard aggregate cap: survive one synchronized adverse bar across the cluster
    tot_e = sum(o["exposure"] for o in out)
    if tot_e > GROSS_EXP_CAP and tot_e > 0:
        scale = GROSS_EXP_CAP / tot_e
        for o in out:
            o["exposure"] *= scale
            o["margin_pct"] = round(o["margin_pct"] * scale, 2)
    return [o for o in out if o["margin_pct"] >= 100 * MIN_MARGIN]
