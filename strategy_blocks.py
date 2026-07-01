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
    """A confirmed swing high is known only `right` bars AFTER it forms; the flag
    is set on the confirmation bar j=i+right (no lookahead). Strict definition:
    high[i] is STRICTLY greater than every other bar in [i-left, i+right] (so
    plateaus are excluded and the max is unique). Vectorized at j: h[i]=shift(right);
    right side = max[i+1,i+right]; left side = max[i-left,i-1]. All indices <= j."""
    h = df["high"]
    hi = h.shift(right)                                        # high at i (=j-right)
    right_max = h.rolling(right, min_periods=right).max() if right >= 1 else pd.Series(float("-inf"), index=h.index)
    left_max = h.shift(right + 1).rolling(left, min_periods=left).max() if left >= 1 else pd.Series(float("-inf"), index=h.index)
    flag = (hi > right_max) & (hi > left_max)
    return flag.fillna(False)


def _swing_low(df: pd.DataFrame, left: int, right: int) -> pd.Series:
    l = df["low"]
    lo = l.shift(right)
    right_min = l.rolling(right, min_periods=right).min() if right >= 1 else pd.Series(float("inf"), index=l.index)
    left_min = l.shift(right + 1).rolling(left, min_periods=left).min() if left >= 1 else pd.Series(float("inf"), index=l.index)
    flag = (lo < right_min) & (lo < left_min)
    return flag.fillna(False)


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
# MEAN-REVERSION / BREAKOUT (direction 2). Numeric, no-lookahead (rolling causal).
# ---------------------------------------------------------------------------

def bb_reversion(df: pd.DataFrame, direction: str, period: int = 20, k: float = 2.0) -> pd.Series:
    """Bollinger-band reversion: prior bar closed OUTSIDE the band, current bar
    closes back INSIDE — a stretched move reverting. LONG on lower band, SHORT on
    upper. Rolling SMA/std are causal; prior comparison uses shift(1)."""
    ma = df["close"].rolling(period, min_periods=period).mean()
    sd = df["close"].rolling(period, min_periods=period).std()
    lower = ma - float(k) * sd
    upper = ma + float(k) * sd
    prev_close = df["close"].shift(1)
    if direction == "LONG":
        return ((prev_close < lower.shift(1)) & (df["close"] > lower)).fillna(False)
    return ((prev_close > upper.shift(1)) & (df["close"] < upper)).fillna(False)


def vwap_reversion(df: pd.DataFrame, direction: str, window: int = 48, dist_atr: float = 1.0) -> pd.Series:
    """Rolling-VWAP reversion: price was >= dist_atr ATRs away from the trailing
    VWAP on the prior bar, current bar turns back toward it. No-lookahead (rolling
    VWAP + shift)."""
    pv = (df["close"] * df["volume"]).rolling(window, min_periods=window).sum()
    vv = df["volume"].rolling(window, min_periods=window).sum()
    vwap = pv / vv.replace(0, float("nan"))
    dist = (df["close"] - vwap) / df["atr"].replace(0, float("nan"))
    prev_dist = dist.shift(1)
    if direction == "LONG":
        return ((prev_dist <= -float(dist_atr)) & (df["close"] > df["open"])).fillna(False)
    return ((prev_dist >= float(dist_atr)) & (df["close"] < df["open"])).fillna(False)


def breakout_retest(df: pd.DataFrame, direction: str, lookback: int = 20,
                    tol_atr: float = 0.3, break_within: int = 10) -> pd.Series:
    """Genuine breakout-retest: price BROKE a prior swing level within the last
    `break_within` bars, then RETESTS it (close within tol_atr*ATR) while holding
    the breakout side. All windows are trailing (causal)."""
    if direction == "LONG":
        level = df["high"].shift(1).rolling(lookback, min_periods=5).max()
        broke = df["close"].shift(1).rolling(break_within, min_periods=1).max() > level
        near = ((df["close"] - level).abs() / df["atr"].replace(0, float("nan"))) <= float(tol_atr)
        hold = df["close"] >= level
        return (broke & near & hold).fillna(False)
    level = df["low"].shift(1).rolling(lookback, min_periods=5).min()
    broke = df["close"].shift(1).rolling(break_within, min_periods=1).min() < level
    near = ((df["close"] - level).abs() / df["atr"].replace(0, float("nan"))) <= float(tol_atr)
    hold = df["close"] <= level
    return (broke & near & hold).fillna(False)


# ---------------------------------------------------------------------------
# ORDER-FLOW (Family A: CVD + funding). These read columns added by
# orderflow_data (cvd_delta_norm, buy_frac, funding_rate). All are no-lookahead
# because those columns are themselves causal. If a column is absent (df not
# enriched) the block returns all-False (safe no-op).
# ---------------------------------------------------------------------------

def _col_or_false(df: pd.DataFrame, col: str) -> pd.Series | None:
    if col not in df.columns:
        return None
    return df[col]


def cvd_aggression(df: pd.DataFrame, direction: str, min_norm: float = 0.5) -> pd.Series:
    """Volume-delta aggression in the trade direction: net taker BUYING (>= +min)
    supports LONG, net taker SELLING (<= -min) supports SHORT. Uses cvd_delta_norm
    (per-bar CVD normalized by trailing volume)."""
    c = _col_or_false(df, "cvd_delta_norm")
    if c is None:
        return pd.Series(False, index=df.index)
    return (c >= float(min_norm)) if direction == "LONG" else (c <= -float(min_norm))


def cvd_reversal(df: pd.DataFrame, direction: str, min_norm: float = 0.5) -> pd.Series:
    """Aggression FLIP: the prior bar pushed against the trade, the current bar
    flips in-favor — buyers/sellers exhausted then reversed."""
    c = _col_or_false(df, "cvd_delta_norm")
    if c is None:
        return pd.Series(False, index=df.index)
    prev = c.shift(1)
    if direction == "LONG":
        return (prev <= -float(min_norm)) & (c >= float(min_norm))
    return (prev >= float(min_norm)) & (c <= -float(min_norm))


def funding_extreme_contrarian(df: pd.DataFrame, direction: str, min_rate: float = 0.0003) -> pd.Series:
    """Crowd-imbalance contrarian: very POSITIVE funding = crowded longs -> fade
    with SHORT; very NEGATIVE funding = crowded shorts -> fade with LONG."""
    c = _col_or_false(df, "funding_rate")
    if c is None:
        return pd.Series(False, index=df.index)
    return (c <= -float(min_rate)) if direction == "LONG" else (c >= float(min_rate))


def buy_frac_extreme(df: pd.DataFrame, direction: str, thresh: float = 0.6) -> pd.Series:
    """Taker buy fraction of volume beyond a threshold in the trade direction."""
    c = _col_or_false(df, "buy_frac")
    if c is None:
        return pd.Series(False, index=df.index)
    return (c >= float(thresh)) if direction == "LONG" else (c <= (1.0 - float(thresh)))


# ---------------------------------------------------------------------------
# TIER-1 seeds (public/academic evidence; meta-loop Layer 1). No-lookahead.
# ---------------------------------------------------------------------------

def ts_momentum(df: pd.DataFrame, direction: str, lookback: int = 20) -> pd.Series:
    """[T3] Time-series momentum (Liu-Tsyvinski): trailing return over `lookback`
    bars > 0 => LONG regime, < 0 => SHORT. Causal: close[i]/close[i-lookback]-1
    uses only past bars."""
    ret = df["close"] / df["close"].shift(lookback) - 1.0
    return (ret > 0).fillna(False) if direction == "LONG" else (ret < 0).fillna(False)


def funding_zscore_fade(df: pd.DataFrame, direction: str, window: int = 48, z: float = 2.0) -> pd.Series:
    """[T2] Funding z-score fade: z = (funding - trailing_mean)/trailing_std over
    `window`. Very POSITIVE z (crowded longs) -> fade with SHORT; very NEGATIVE ->
    LONG. Reads the enriched funding_rate column; rolling stats are causal. Distinct
    from funding_extreme_contrarian (absolute threshold) — this is regime-relative."""
    fr = _col_or_false(df, "funding_rate")
    if fr is None:
        return pd.Series(False, index=df.index)
    mp = max(5, window // 2)
    mean = fr.rolling(window, min_periods=mp).mean()
    sd = fr.rolling(window, min_periods=mp).std()
    zscore = (fr - mean) / sd.replace(0, float("nan"))
    return (zscore <= -float(z)).fillna(False) if direction == "LONG" else (zscore >= float(z)).fillna(False)


# ---------------------------------------------------------------------------
# ROUND-2 agent-proposed blocks (new mechanisms, learned from the dead ledger).
# All causal: trailing rolling + shift(+n) only, no shift(-n)/centered windows.
# ---------------------------------------------------------------------------

def squeeze_release_break(df: pd.DataFrame, direction: str, bb_period: int = 20, bb_k: float = 2.0,
                          kc_mult: float = 1.5, squeeze_min_bars: int = 6, box_lookback: int = 20) -> pd.Series:
    """Vol compression->expansion ignition: Bollinger width inside Keltner width
    (a 'squeeze') for >= squeeze_min_bars, then RELEASE + close breaks the box
    extreme. Fires only at the ignition point (rare, high-conviction)."""
    ma = df["close"].rolling(bb_period, min_periods=bb_period).mean()
    sd = df["close"].rolling(bb_period, min_periods=bb_period).std()
    sq = (2 * float(bb_k) * sd) < (2 * float(kc_mult) * df["atr"])
    was = sq.shift(1).rolling(squeeze_min_bars, min_periods=squeeze_min_bars).min() >= 1
    rel = was & (~sq)
    hi = df["high"].shift(1).rolling(box_lookback, min_periods=box_lookback).max()
    lo = df["low"].shift(1).rolling(box_lookback, min_periods=box_lookback).min()
    sig = rel & (df["close"] > hi) if direction == "LONG" else rel & (df["close"] < lo)
    return sig.fillna(False)


def donchian_breakout_committed(df: pd.DataFrame, direction: str, n: int = 20, k: float = 0.25) -> pd.Series:
    """Turtle-style committed breakout with an ATR buffer beyond a fresh N-bar
    extreme (wide-stop trend entry; the logical OPPOSITE of breakout_retest)."""
    dh = df["high"].shift(1).rolling(n, min_periods=n).max()
    dl = df["low"].shift(1).rolling(n, min_periods=n).min()
    sig = (df["close"] > dh + float(k) * df["atr"]) if direction == "LONG" else (df["close"] < dl - float(k) * df["atr"])
    return sig.fillna(False)


def session_range_breakout(df: pd.DataFrame, direction: str, asia_end: int = 7, win_end: int = 16) -> pd.Series:
    """Asia-range breakout at the London/NY session (calendar axis; no dead block
    reads the clock). LONG = first window-session bar to close above today's Asia
    high. Causal: within-day cummax/cummin of the completed Asia session."""
    dt = pd.to_datetime(df["ts_ms"], unit="ms")
    hod = dt.dt.hour
    day = dt.dt.floor("D")
    asia = (hod >= 0) & (hod < int(asia_end))
    win = (hod >= int(asia_end)) & (hod < int(win_end))
    ah = df["high"].where(asia).groupby(day).cummax().groupby(day).ffill()
    al = df["low"].where(asia).groupby(day).cummin().groupby(day).ffill()
    lg = win & (df["close"] > ah) & (df["close"].shift(1) <= ah.shift(1)) & ah.notna()
    sh = win & (df["close"] < al) & (df["close"].shift(1) >= al.shift(1)) & al.notna()
    return (lg if direction == "LONG" else sh).fillna(False)


def cvd_trend_divergence(df: pd.DataFrame, direction: str, lookback: int = 20,
                         p_thr: float = 1.0, c_thr: float = 0.5) -> pd.Series:
    """Flow DISAGREES with price (dead CVD blocks required agreement): price makes
    a >= p_thr*ATR move over lookback while net taker flow (rolling cvd) leans the
    other way -> absorption/exhaustion. Reads enriched cvd_delta_norm."""
    c = _col_or_false(df, "cvd_delta_norm")
    if c is None:
        return pd.Series(False, index=df.index)
    pc = (df["close"] - df["close"].shift(lookback)) / df["atr"].replace(0, float("nan"))
    cs = c.rolling(lookback, min_periods=lookback).sum()
    sh = (pc >= float(p_thr)) & (cs <= -float(c_thr))
    lg = (pc <= -float(p_thr)) & (cs >= float(c_thr))
    return (lg if direction == "LONG" else sh).fillna(False)


def donchian_multitouch_fade(df: pd.DataFrame, direction: str, level_lookback: int = 48,
                             tol_atr: float = 0.5, min_touches: int = 3) -> pd.Series:
    """Fade a shelf validated >= min_touches times (stacked liquidity likely
    defended), not one fresh wick (unlike the dead sweep_reversal). Wide structural
    stop beyond the level implied by exit_cfg."""
    mp = max(5, level_lookback // 2)
    res = df["high"].shift(1).rolling(level_lookback, min_periods=mp).max()
    sup = df["low"].shift(1).rolling(level_lookback, min_periods=mp).min()
    atr = df["atr"].replace(0, float("nan"))
    nr = ((res - df["high"]).abs() <= float(tol_atr) * atr).rolling(level_lookback, min_periods=mp).sum()
    ns = ((df["low"] - sup).abs() <= float(tol_atr) * atr).rolling(level_lookback, min_periods=mp).sum()
    sh = (df["high"] >= res) & (df["close"] < res) & (nr >= int(min_touches))
    lg = (df["low"] <= sup) & (df["close"] > sup) & (ns >= int(min_touches))
    return (lg if direction == "LONG" else sh).fillna(False)


def kaufman_efficiency_regime(df: pd.DataFrame, er_window: int = 20, er_min: float = 0.35) -> pd.Series:
    """Kaufman Efficiency Ratio regime gate (direction-agnostic): net travel /
    total path over er_window. ER>=er_min = efficient/trending (enables trend
    triggers); path-efficiency, distinct from DI-based ADX."""
    net = (df["close"] - df["close"].shift(er_window)).abs()
    path = (df["close"] - df["close"].shift(1)).abs().rolling(er_window, min_periods=er_window).sum()
    er = net / path.replace(0, float("nan"))
    return (er >= float(er_min)).fillna(False)


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
    # Order-flow family A (CVD + funding; read enriched columns)
    "cvd_aggression": {"fn": cvd_aggression, "directional": True},
    "cvd_reversal": {"fn": cvd_reversal, "directional": True},
    "funding_extreme_contrarian": {"fn": funding_extreme_contrarian, "directional": True},
    "buy_frac_extreme": {"fn": buy_frac_extreme, "directional": True},
    # Mean-reversion / breakout (direction 2)
    "bb_reversion": {"fn": bb_reversion, "directional": True},
    "vwap_reversion": {"fn": vwap_reversion, "directional": True},
    "breakout_retest": {"fn": breakout_retest, "directional": True},
    # Tier-1 seeds (meta-loop Layer 1)
    "ts_momentum": {"fn": ts_momentum, "directional": True},
    "funding_zscore_fade": {"fn": funding_zscore_fade, "directional": True},
    # Round-2 agent-proposed blocks (new mechanisms)
    "squeeze_release_break": {"fn": squeeze_release_break, "directional": True},
    "donchian_breakout_committed": {"fn": donchian_breakout_committed, "directional": True},
    "session_range_breakout": {"fn": session_range_breakout, "directional": True},
    "cvd_trend_divergence": {"fn": cvd_trend_divergence, "directional": True},
    "donchian_multitouch_fade": {"fn": donchian_multitouch_fade, "directional": True},
    "kaufman_efficiency_regime": {"fn": kaufman_efficiency_regime, "directional": False},
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
