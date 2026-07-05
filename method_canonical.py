"""Canonical form + novelty hash for trading-method DSL (second brain P2).

The novelty gate's identity function. Two methods are THE SAME IDEA iff their
canonical payloads match: id/name/desc are pure labels (zero effect on a
backtest) and are excluded — they are exactly what an LLM re-proposal changes.

Layers:
  method_hash()   EXACT identity — side + sorted `when` conditions (val rounded
                  to 6, matching repo float conventions) + sl/tp rounded to 2
                  (validate_method already stores round(x, 2)). This reproduces
                  and widens the proven ingest_candidates.sig() behavior; it
                  never merges deliberate A/B variants.
  bucketed_hash() ADVISORY near-duplicate layer — thresholds bucketed per
                  feature family so `rsi14<30` ≈ `rsi14<31`. Categorical feats
                  (ema_stack, dow, hour_utc, streak*, ema4h_*) are NEVER
                  bucketed (ema_stack==1 bucketed would be a semantic error).
                  Used to FLAG, not to block: too-coarse buckets would kill
                  intended threshold experiments.

Pure module: no I/O, no repo imports beyond atomic_state.canonical_json.
"""
from __future__ import annotations

import hashlib
from typing import Any

from atomic_state import canonical_json

# categorical features: exact identity, never bucketed
CATEGORICAL = {"ema_stack", "ema4h_state", "ema4h_cross", "dow", "hour_utc",
               "streak", "streak_up", "streak_down"}

# advisory bucket width per feature family (val is snapped to nearest multiple)
_BUCKET = {
    "rsi14": 5.0,
    "vol_ratio": 0.1,
    "close_pos": 0.1,
    "bar_z": 0.5,
    "funding_rate_bps": 0.5,
    "funding_z": 0.5,
    # percent-space features
    "ret5": 0.5, "ret20": 0.5,
    "px_vs_ema20": 0.5, "px_vs_ema50": 0.5, "px_vs_ema200": 0.5,
    "dd96_pct": 0.5, "rally96_pct": 0.5, "dd_from_high96_pct": 0.5,
    "brk20_pct": 0.5, "brkdn20_pct": 0.5, "range20_pct": 0.5, "atr_pct": 0.5,
}


def _conds(method: dict[str, Any], bucket: bool) -> list[list[Any]]:
    out = []
    for c in method.get("when") or []:
        feat = str(c.get("feat"))
        op = str(c.get("op"))
        val = float(c.get("val"))
        if bucket and feat not in CATEGORICAL:
            step = _BUCKET.get(feat)
            if step:
                val = round(round(val / step) * step, 6)
        out.append([feat, op, round(val, 6)])
    out.sort(key=lambda t: (t[0], t[1], t[2]))     # `when` is an AND-set: order-free
    return out


def canonical_method(method: dict[str, Any], bucket: bool = False) -> dict[str, Any]:
    """Semantic payload only — everything that changes backtest outcome, nothing
    that doesn't. v2 (Codex review): sl/tp AND timeout included — deep_validation
    optimizes the hold cap, so two trials differing only by timeout are distinct;
    omitting it merged them and made the as-traded hash non-replayable. In the
    bucketed (advisory) layer, sl/tp are bucketed too — they are optimized params
    like any threshold."""
    sl = float(method.get("sl_pct", 1.5))
    tp = float(method.get("tp_pct", 2.5))
    to = int(method.get("timeout") or 16)          # DSL default hold cap = 16 bars
    if bucket:
        sl = round(round(sl / 0.5) * 0.5, 2)
        tp = round(round(tp / 0.5) * 0.5, 2)
        to = int(round(to / 16.0) * 16) or 16
    return {
        "side": str(method.get("side", "LONG")),
        "when": _conds(method, bucket),
        "sl": round(sl, 2),
        "tp": round(tp, 2),
        "to": to,
        "v": 2,
    }


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()[:20]


def method_hash(method: dict[str, Any]) -> str:
    """EXACT novelty hash — the hard gate key."""
    return _digest(canonical_method(method, bucket=False))


def bucketed_hash(method: dict[str, Any]) -> str:
    """Advisory near-dup hash — FLAG only, never a hard block."""
    return _digest(canonical_method(method, bucket=True))
