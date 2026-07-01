"""Edge-research harness — strategy spec + compiler (HARNESS-2).

A setup is a serializable dict (spec) that the compiler turns into a signal
function the existing backtest engine can run. This lets the sweep runner
enumerate specs, backtest each, and log them to the experience ledger.

Spec schema (all fields JSON-serializable):
{
  "name": "reject_ema_short",
  "direction": "SHORT",                # LONG or SHORT
  "entry": {
     "all": [                          # AND group (list of blocks)
        {"block": "trend_ema_stack"},
        {"block": "regime_adx_min", "params": {"adx_min": 25}},
        {"block": "location_reject_ema_from_below"},
        {"block": "volume_min_ratio", "params": {"min_ratio": 1.5}}
     ],
     "any": []                         # optional OR group (list of blocks)
  },
  "exit": {"sl_atr": 1.5, "tp_atr": 3.0, "min_rr": 1.5,
           "regime_exit": true, "adx_exit": 20, "max_hold_bars": 48}
}

The compiled signal function has the signature the backtest engine expects:
  signal_fn(df, i, df_1h) -> {side, index, feature_ts, atr, ref_close} | None
It evaluates every entry block once (vectorized) and caches per-df, then at bar i
checks the combined predicate and, if true, emits an entry for bar i+1.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

import pandas as pd

import backtest_chart_signal as cs
import strategy_blocks as sb

REQUIRED_WARMUP = max(cs.EMA_SLOW, cs.ADX_PERIOD, cs.VOL_MA) + 1


def spec_id(spec: dict[str, Any]) -> str:
    payload = json.dumps(spec, sort_keys=True, default=str)
    return "setup_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def validate_spec(spec: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if spec.get("direction") not in ("LONG", "SHORT"):
        errors.append("direction must be LONG or SHORT")
    entry = spec.get("entry") or {}
    all_blocks = entry.get("all") or []
    any_blocks = entry.get("any") or []
    if not all_blocks and not any_blocks:
        errors.append("entry needs at least one block in 'all' or 'any'")
    for grp in (all_blocks, any_blocks):
        for b in grp:
            if b.get("block") not in sb.BLOCKS:
                errors.append(f"unknown block: {b.get('block')}")
    return errors


def _combined_mask(df: pd.DataFrame, direction: str, entry: dict[str, Any]) -> pd.Series:
    """AND all blocks in 'all', OR all in 'any', then AND the two groups."""
    mask = pd.Series(True, index=df.index)
    all_blocks = entry.get("all") or []
    for b in all_blocks:
        mask &= sb.evaluate_block(b["block"], df, direction, b.get("params"))
    any_blocks = entry.get("any") or []
    if any_blocks:
        any_mask = pd.Series(False, index=df.index)
        for b in any_blocks:
            any_mask |= sb.evaluate_block(b["block"], df, direction, b.get("params"))
        mask &= any_mask
    return mask.fillna(False).astype(bool)


def compile_spec(spec: dict[str, Any]) -> Callable[[pd.DataFrame, int, pd.DataFrame], dict[str, Any] | None]:
    """Return a signal_fn(df, i, df_1h) -> sig|None for the backtest engine.

    The combined entry mask is computed once per df and cached on the df object
    (keyed by spec id) so per-bar calls are O(1). No lookahead: the mask at bar i
    uses only blocks that are themselves no-lookahead (proven in HARNESS-1)."""
    errors = validate_spec(spec)
    if errors:
        raise ValueError(f"invalid spec: {errors}")
    direction = spec["direction"]
    entry = spec["entry"]
    sid = spec_id(spec)
    cache_attr = f"_mask_{sid}"

    def signal_fn(df: pd.DataFrame, i: int, df_1h: pd.DataFrame) -> dict[str, Any] | None:
        if i < REQUIRED_WARMUP or i + 1 >= len(df):
            return None
        mask = getattr(df, cache_attr, None)
        if mask is None:
            mask = _combined_mask(df, direction, entry)
            try:
                object.__setattr__(df, cache_attr, mask)
            except Exception:
                pass
        cur = df.iloc[i]
        atr_v = cur.get("atr")
        if atr_v is None or pd.isna(atr_v) or float(atr_v) <= 0:
            return None
        if not bool(mask.iloc[i]):
            return None
        return {"side": direction, "index": i + 1, "feature_ts": cur["close_time"],
                "atr": float(atr_v), "ref_close": float(cur["close"])}

    signal_fn.spec_id = sid  # type: ignore[attr-defined]
    return signal_fn
