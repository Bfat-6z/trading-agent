"""P1 calibration report — the honest READ-ONLY feedback/KPI channel.

Reads the mission's closed trades (which carry the P0 metrics: predicted_R, actual_R, mfe_R, mae_R,
noise_stop, thesis_wrong) and answers the decision-critical question the whole roadmap hinges on:

    is the 75% stop-out rate NOISE-STOP (price offered >=1R then wiggled to the stop -> fix = wider
    structure stop) or THESIS-WRONG (went straight against the entry -> fix = entry SELECTION)?

Pure, deterministic, no side effects. Malformed rows are skipped (never crash the caller), mirroring
llm_trader_memory.py's discipline. This module NEVER decides or trades — it only measures. The
interpretation caveats (Opus xhigh review): BE-trailing suppresses noise_stop for discretionary trades
that reach +1R, and mfe_R excludes the exit bar (single-bar round-trips under-count as thesis_wrong),
so read noise_stop_rate as a floor, population-conditionally.
"""
from __future__ import annotations

from typing import Any

_SETUP_KEYWORDS = ("capitulation", "flush", "panic", "pullback", "reclaim", "breakout", "break",
                   "range", "fade", "bounce", "trend", "bos", "choch", "support", "resistance")


def _setup_type(rationale: str | None) -> str:
    """Bucket a free-text rationale into a controlled setup vocab (works retroactively on old rows)."""
    t = (rationale or "").lower()
    for kw in _SETUP_KEYWORDS:
        if kw in t:
            return {"flush": "capitulation", "panic": "capitulation", "break": "breakout",
                    "fade": "range", "bounce": "reclaim"}.get(kw, kw)
    return "other"


def _num(x: Any) -> float | None:
    try:
        v = float(x)
        return v if v == v else None   # reject NaN
    except (TypeError, ValueError):
        return None


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 3) if xs else None


def _group_stats(rows: list[dict[str, Any]], key) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for c in rows:
        groups.setdefault(str(key(c)), []).append(c)
    out = {}
    for g, rs in groups.items():
        nets = [_num(c.get("net")) for c in rs]
        nets = [x for x in nets if x is not None]
        wins = sum(1 for x in nets if x > 0)
        ar = [_num(c.get("actual_R")) for c in rs]
        ar = [x for x in ar if x is not None]
        out[g] = {"n": len(rs), "wr": round(wins / len(rs), 3) if rs else None,
                  "mean_actual_R": _mean(ar), "net": round(sum(nets), 3) if nets else 0.0}
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["n"]))


def calibration_report(closed: list[dict[str, Any]], window: int = 40,
                       discretionary_only: bool = True) -> dict[str, Any]:
    """Deterministic calibration/diagnostic report over the last `window` P0-instrumented trades.

    Only rows that carry the P0 metrics (actual_R present) are considered — older pre-P0 trades are
    silently ignored. `discretionary_only` filters out mechanical/proven fires (mech_method set).
    """
    rows = []
    for c in closed:
        if not isinstance(c, dict):
            continue
        if discretionary_only and c.get("mech_method"):
            continue
        if _num(c.get("actual_R")) is None:   # not P0-instrumented -> skip
            continue
        rows.append(c)
    rows = rows[-window:]
    n = len(rows)
    if n == 0:
        return {"n": 0, "note": "no P0-instrumented discretionary trades yet — accumulate closes first"}

    nets = [(_num(c.get("net")) or 0.0) for c in rows]
    wins = sum(1 for x in nets if x > 0)
    losers = [c for c in rows if (_num(c.get("net")) or 0.0) < 0]
    n_loss = len(losers)

    noise = sum(1 for c in losers if c.get("noise_stop") is True)
    thesis = sum(1 for c in losers if c.get("thesis_wrong") is True)

    over = [(_num(c.get("predicted_R")) - _num(c.get("actual_R")))
            for c in rows if _num(c.get("predicted_R")) is not None and _num(c.get("actual_R")) is not None]
    ar_all = [x for x in (_num(c.get("actual_R")) for c in rows) if x is not None]
    mfe_loss = [x for x in (_num(c.get("mfe_R")) for c in losers) if x is not None]

    return {
        "n": n, "n_loss": n_loss,
        "win_rate": round(wins / n, 3),
        "mean_actual_R": _mean(ar_all),
        "sum_net": round(sum(nets), 3),
        # THE two headline numbers that decide the direction:
        "noise_stop_rate": round(noise / n_loss, 3) if n_loss else None,   # floor (BE-trail suppresses)
        "thesis_wrong_rate": round(thesis / n_loss, 3) if n_loss else None,
        "median_loss_mfe_R": (sorted(mfe_loss)[len(mfe_loss) // 2] if mfe_loss else None),
        # calibration of the model's R prediction (systematic over-optimism):
        "over_optimism_R": _mean(over),            # positive => predicts more R than it achieves
        "by_setup": _group_stats(rows, lambda c: _setup_type(c.get("rationale"))),
        "by_regime": _group_stats(rows, lambda c: c.get("regime") or "?"),
        # honest interpretation flag for whoever reads this:
        "verdict_hint": _verdict_hint(noise, thesis, n_loss),
    }


def _verdict_hint(noise: int, thesis: int, n_loss: int) -> str:
    if n_loss == 0:
        return "no losses in window"
    if thesis > noise:
        return "THESIS-WRONG dominates -> losses are bad entries, not bad stops -> fix = entry SELECTION"
    if noise > thesis:
        return "NOISE-STOP dominates -> price offered profit then stopped -> fix = wider structure stop"
    return "mixed -> need more samples to separate"
