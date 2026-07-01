"""Edge-research harness — building blocks (HARNESS-1).

Each block is a pure, vectorized, NO-LOOKAHEAD predicate or feature computed on
CLOSED candles. A block reads an indicator DataFrame (from
backtest_chart_signal.compute_indicators) and returns a boolean pandas Series
aligned to the bars: True where the condition holds AT THAT CLOSED BAR, using
only data up to and including that bar.

Blocks are parameterized and composed by the strategy compiler (HARNESS-2). They
never look at future bars: they use the already-audited causal indicators
(EWM EMA/ATR/ADX, rolling volume) plus backward-only rolling windows for
structure. Direction is explicit (long vs short) so setups are symmetric.

Invariant every block guarantees: block(df, ...).iloc[i] depends only on df rows
0..i. Tests in tests/test_strategy_blocks.py prove this by shuffling/truncating
future rows and asserting earlier outputs are unchanged.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

import backtest_chart_signal as cs

# ---------------------------------------------------------------------------
# TREND
# ---------------------------------------------------------------------------

def trend_ema_stack(df: pd.DataFrame, direction: str) -> pd.Series:
    """EMA fast above slow (long) / below (short). Uses cs EMA columns."""
    up = df["ema_fast"] > df["ema_slow"]
    return up if direction == "LONG" else ~up


def trend_price_vs_ema(df: pd.DataFrame, direction: str, ema_col: str = "ema_fast") -> pd.Series:
    """Close above (long) / below (short) the chosen EMA."""
    above = df["close"] > df[ema_col]
    return above if direction == "LONG" else ~above


# ---------------------------------------------------------------------------
# REGIME
# ---------------------------------------------------------------------------

def regime_adx_min(df: pd.DataFrame, adx_min: float = 25.0) -> pd.Series:
    """Trending regime: ADX >= threshold (direction-agnostic)."""
    return df["adx"] >= float(adx_min)


def regime_atr_percentile(df: pd.DataFrame, low_pct: float = 0.0, high_pct: float = 1.0,
                          window: int = 200) -> pd.Series:
    """ATR (as % of price) within a rolling percentile band [low,high]. Rolling
    rank uses only the trailing `window` bars (no future)."""
    atr_pct = (df["atr"] / df["close"]).clip(lower=0)
    # trailing percentile rank of the current value within the rolling window
    rank = atr_pct.rolling(window, min_periods=max(20, window // 4)).apply(
        lambda a: (a[:-1] <= a[-1]).mean() if len(a) > 1 else 0.5, raw=True)
    return (rank >= float(low_pct)) & (rank <= float(high_pct))


# ---------------------------------------------------------------------------
# STRUCTURE  (backward-only swing detection)
# ---------------------------------------------------------------------------

def _swing_high(df: pd.DataFrame, left: int, right: int) -> pd.Series:
    """A confirmed swing high is known only `right` bars AFTER it forms. We shift
    the confirmation forward so the flag is set on the bar where it becomes
    KNOWN (no lookahead)."""
    h = df["high"]
    is_piv = pd.Series(False, index=df.index)
    for i in range(left, len(df) - right):
        window = h.iloc[i - left:i + right + 1]
        if h.iloc[i] == window.max() and (window == h.iloc[i]).sum() == 1:
            # known only at bar i+right (after right confirming bars close)
            is_piv.iloc[i + right] = True
    return is_piv


def _swing_low(df: pd.DataFrame, left: int, right: int) -> pd.Series:
    l = df["low"]
    is_piv = pd.Series(False, index=df.index)
    for i in range(left, len(df) - right):
        window = l.iloc[i - left:i + right + 1]
        if l.iloc[i] == window.min() and (window == l.iloc[i]).sum() == 1:
            is_piv.iloc[i + right] = True
    return is_piv


def structure_break(df: pd.DataFrame, direction: str, left: int = 2, right: int = 2) -> pd.Series:
    """Break of structure: for SHORT, close breaks below the most recent confirmed
    swing low; for LONG, close breaks above the most recent confirmed swing high.
    All confirmations are backward-only."""
    if direction == "LONG":
        piv = _swing_high(df, left, right)
        level = df["high"].where(piv).ffill()
        return (df["close"] > level) & level.notna()
    piv = _swing_low(df, left, right)
    level = df["low"].where(piv).ffill()
    return (df["close"] < level) & level.notna()


# ---------------------------------------------------------------------------
# VOLUME
# ---------------------------------------------------------------------------

def volume_min_ratio(df: pd.DataFrame, min_ratio: float = 1.5) -> pd.Series:
    """Current volume >= min_ratio × its trailing moving average."""
    return df["vol_ratio"] >= float(min_ratio)


def volume_spike(df: pd.DataFrame, mult: float = 2.0) -> pd.Series:
    return df["vol_ratio"] >= float(mult)


# ---------------------------------------------------------------------------
# LOCATION
# ---------------------------------------------------------------------------

def location_near_ema(df: pd.DataFrame, ema_col: str = "ema_fast", max_atr: float = 1.0) -> pd.Series:
    """Price within max_atr ATRs of the EMA (a pullback zone, not extended)."""
    dist = (df["close"] - df[ema_col]).abs() / df["atr"].replace(0, float("nan"))
    return dist <= float(max_atr)


def location_not_overextended(df: pd.DataFrame, ema_col: str = "ema_fast", max_atr: float = 2.0) -> pd.Series:
    dist = (df["close"] - df[ema_col]).abs() / df["atr"].replace(0, float("nan"))
    return dist <= float(max_atr)


def location_reject_ema_from_below(df: pd.DataFrame, ema_col: str = "ema_fast") -> pd.Series:
    """SHORT setup core: prior bar poked UP into/above the EMA, current bar closes
    back BELOW it and is bearish — a rejection of the EMA cluster from below."""
    prev_high = df["high"].shift(1)
    prev_ema = df[ema_col].shift(1)
    poked = prev_high >= prev_ema
    reclaim_down = (df["close"] < df[ema_col]) & (df["close"] < df["open"])
    return poked & reclaim_down


def location_reclaim_ema_from_above(df: pd.DataFrame, ema_col: str = "ema_fast") -> pd.Series:
    """LONG mirror: prior bar dipped to/below the EMA, current bar closes back
    ABOVE it and is bullish."""
    prev_low = df["low"].shift(1)
    prev_ema = df[ema_col].shift(1)
    dipped = prev_low <= prev_ema
    reclaim_up = (df["close"] > df[ema_col]) & (df["close"] > df["open"])
    return dipped & reclaim_up


# ---------------------------------------------------------------------------
# Block registry — name -> (callable, whether it needs `direction`)
# ---------------------------------------------------------------------------

BLOCKS: dict[str, dict[str, Any]] = {
    "trend_ema_stack": {"fn": trend_ema_stack, "directional": True},
    "trend_price_vs_ema": {"fn": trend_price_vs_ema, "directional": True},
    "regime_adx_min": {"fn": regime_adx_min, "directional": False},
    "regime_atr_percentile": {"fn": regime_atr_percentile, "directional": False},
    "structure_break": {"fn": structure_break, "directional": True},
    "volume_min_ratio": {"fn": volume_min_ratio, "directional": False},
    "volume_spike": {"fn": volume_spike, "directional": False},
    "location_near_ema": {"fn": location_near_ema, "directional": False},
    "location_not_overextended": {"fn": location_not_overextended, "directional": False},
    "location_reject_ema_from_below": {"fn": location_reject_ema_from_below, "directional": False},
    "location_reclaim_ema_from_above": {"fn": location_reclaim_ema_from_above, "directional": False},
}


def evaluate_block(name: str, df: pd.DataFrame, direction: str, params: dict[str, Any] | None = None) -> pd.Series:
    """Evaluate a named block. Directional blocks receive `direction`; others get
    only their params. Returns a boolean Series aligned to df."""
    spec = BLOCKS.get(name)
    if spec is None:
        raise KeyError(f"unknown block: {name}")
    params = dict(params or {})
    fn = spec["fn"]
    if spec["directional"]:
        return fn(df, direction, **params).fillna(False).astype(bool)
    return fn(df, **params).fillna(False).astype(bool)
