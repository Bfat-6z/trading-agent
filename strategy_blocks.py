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
# LIQUIDITY SWEEP REVERSAL (SMC/ICT concepts forced into numeric, no-lookahead
# rules). Every threshold is explicit. Anything only definable "in hindsight" is
# excluded. Hypothesis: price sweeps a liquidity level then reverses.
# ---------------------------------------------------------------------------

def sweep_reversal(df: pd.DataFrame, direction: str, swing_lookback: int = 20,
                   reverse_within: int = 3) -> pd.Series:
    """Numeric sweep: within the last `reverse_within` bars, the HIGH exceeded the
    prior swing high (recent max over `swing_lookback`) then the CURRENT bar
    closes back BELOW that level (bearish sweep -> SHORT); mirror for LONG.

    No-lookahead: the swing level at bar i is the rolling max/min of bars strictly
    BEFORE the sweep window; the current close is bar i's own close."""
    # prior swing level: rolling extreme excluding the reverse window
    shift_n = reverse_within
    if direction == "SHORT":
        prior_level = df["high"].shift(shift_n).rolling(swing_lookback, min_periods=5).max()
        swept = df["high"].rolling(reverse_within, min_periods=1).max() > prior_level
        reclaimed = df["close"] < prior_level
        return (swept & reclaimed).fillna(False)
    prior_level = df["low"].shift(shift_n).rolling(swing_lookback, min_periods=5).min()
    swept = df["low"].rolling(reverse_within, min_periods=1).min() < prior_level
    reclaimed = df["close"] > prior_level
    return (swept & reclaimed).fillna(False)


def htf_bias_po3(df: pd.DataFrame, direction: str, df_htf: pd.DataFrame | None = None) -> pd.Series:
    """PO3 higher-timeframe bias: only allow SHORT when the HTF is in a down bias
    (HTF EMA fast < slow at the last CLOSED HTF bar), LONG when up bias. Joined
    point-in-time against df's ts_ms (no in-progress HTF bar).

    If df_htf is None, falls back to df's own ema stack (same-TF bias)."""
    if df_htf is None or df_htf.empty:
        up = df["ema_fast"] > df["ema_slow"]
        return up if direction == "LONG" else ~up
    htf_ts = df_htf["ts_ms"].to_numpy()
    htf_up = (df_htf["ema_fast"] > df_htf["ema_slow"]).to_numpy()
    import numpy as np
    idx = np.searchsorted(htf_ts, df["ts_ms"].to_numpy(), side="right") - 1
    out = pd.Series(False, index=df.index)
    valid = idx >= 0
    vals = np.where(valid, htf_up[np.clip(idx, 0, len(htf_up) - 1)], False)
    up_series = pd.Series(vals, index=df.index)
    return up_series if direction == "LONG" else (~up_series & pd.Series(valid, index=df.index))


def structure_shift(df: pd.DataFrame, direction: str, min_atr: float = 0.5,
                    left: int = 2, right: int = 2) -> pd.Series:
    """BOS/CHOCH forced to number: current close breaks the most recent confirmed
    swing (opposite side) by at least `min_atr` * ATR. Backward-only swings."""
    if direction == "SHORT":
        piv = _swing_low(df, left, right)
        level = df["low"].where(piv).ffill()
        return ((level - df["close"]) >= float(min_atr) * df["atr"]) & level.notna()
    piv = _swing_high(df, left, right)
    level = df["high"].where(piv).ffill()
    return ((df["close"] - level) >= float(min_atr) * df["atr"]) & level.notna()


def displacement(df: pd.DataFrame, min_atr: float = 1.0) -> pd.Series:
    """Confirmation candle with a real range: (high-low) >= min_atr * ATR."""
    rng = df["high"] - df["low"]
    return (rng >= float(min_atr) * df["atr"]).fillna(False)


def retest_broken_level(df: pd.DataFrame, direction: str, swing_lookback: int = 20,
                        tol_atr: float = 0.3) -> pd.Series:
    """Entry proxy for FVG/OB retest: price returns within tol_atr*ATR of the
    swing level that was just swept/broken (no in-progress HTF candle used)."""
    if direction == "SHORT":
        level = df["high"].shift(1).rolling(swing_lookback, min_periods=5).max()
    else:
        level = df["low"].shift(1).rolling(swing_lookback, min_periods=5).min()
    dist = (df["close"] - level).abs() / df["atr"].replace(0, float("nan"))
    return (dist <= float(tol_atr)).fillna(False)


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
    # Liquidity-sweep-reversal family (SMC/ICT forced into numeric rules)
    "sweep_reversal": {"fn": sweep_reversal, "directional": True},
    "structure_shift": {"fn": structure_shift, "directional": True},
    "displacement": {"fn": displacement, "directional": False},
    "retest_broken_level": {"fn": retest_broken_level, "directional": True},
    # htf_bias_po3 needs the HTF dataframe -> handled specially in evaluate_block
    "htf_bias_po3": {"fn": htf_bias_po3, "directional": True, "needs_htf": True},
}


def evaluate_block(name: str, df: pd.DataFrame, direction: str, params: dict[str, Any] | None = None,
                   df_htf: pd.DataFrame | None = None) -> pd.Series:
    """Evaluate a named block. Directional blocks receive `direction`; blocks
    marked needs_htf also receive the higher-timeframe dataframe (point-in-time
    joined inside the block). Returns a boolean Series aligned to df."""
    spec = BLOCKS.get(name)
    if spec is None:
        raise KeyError(f"unknown block: {name}")
    params = dict(params or {})
    fn = spec["fn"]
    if spec.get("needs_htf"):
        return fn(df, direction, df_htf=df_htf, **params).fillna(False).astype(bool)
    if spec["directional"]:
        return fn(df, direction, **params).fillna(False).astype(bool)
    return fn(df, **params).fillna(False).astype(bool)
