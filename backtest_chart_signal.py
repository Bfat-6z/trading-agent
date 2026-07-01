"""Phase 3 — deterministic, no-lookahead chart signal + bracket backtest.

Setup (owner direction: candles + MA + volume):
  EMA20/50 trend + 1h HTF alignment + ADX(14)>25 regime gate + volume>=1.5x
  + pullback-to-EMA20-then-reclaim candle. Structure SL=1.5*ATR, TP=3.0*ATR.

Everything is computed on CLOSED candles only. Indicators are vectorized but a
signal for bar i uses ONLY data through bar i (the just-closed candle), entry is
the NEXT bar's open. The 1h HTF gate is point-in-time joined: each 5m bar sees
the most recent 1h bar that had already CLOSED at that 5m bar's close time — no
in-progress 1h leak.

Costs come from paper_cost_model (Phase-2 pessimistic tiers). Funding charged
once per 8h boundary crossed. Backtest is a multi-bar bracket with a pessimistic
SL-first tie-break when a bar spans both SL and TP.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable

import pandas as pd

from paper_cost_model import TAKER_FEE_RATE, fill_bps, liquidity_tier

# Frozen parameter set (pre-registered before any holdout peek).
EMA_FAST = 20
EMA_SLOW = 50
ADX_PERIOD = 14
ADX_MIN = 25.0
ATR_PERIOD = 14
VOL_MA = 20
VOL_MIN = 1.5
SL_ATR_MULT = 1.5
TP_ATR_MULT = 3.0
OVEREXT_ATR = 2.0
MAX_HOLD_BARS = 48
FUNDING_INTERVAL_MS = 8 * 3600 * 1000
FUNDING_RATE = Decimal("0.0001")  # pessimistic ~0.01%/interval


def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    prev_c = c.shift(1)
    tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _adx(df: pd.DataFrame, period: int) -> pd.Series:
    h, l = df["high"], df["low"]
    up = h.diff()
    dn = -l.diff()
    plus_dm = (((up > dn) & (up > 0)) * up).astype("float64")
    minus_dm = (((dn > up) & (dn > 0)) * dn).astype("float64")
    prev_c = df["close"].shift(1)
    tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    atr_safe = atr.replace(0, float("nan"))
    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_safe)
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_safe)
    di_sum = (plus_di + minus_di).replace(0, float("nan"))
    dx = (100 * (plus_di - minus_di).abs() / di_sum).astype("float64")
    return dx.ewm(alpha=1 / period, adjust=False).mean()


def _bars_to_df(bars: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame({
        "open_time": [b["open_time"] for b in bars],
        "close_time": [b["close_time"] for b in bars],
        "open": [float(b["open"]) for b in bars],
        "high": [float(b["high"]) for b in bars],
        "low": [float(b["low"]) for b in bars],
        "close": [float(b["close"]) for b in bars],
        "volume": [float(b.get("volume") or 0.0) for b in bars],
    })
    # close_time -> epoch milliseconds, unit-safe (pandas may use us/ns dtype).
    dt = pd.to_datetime(df["close_time"], utc=True).dt.tz_localize(None)
    df["ts_ms"] = (dt.astype("datetime64[ms]").astype("int64"))
    return df


def compute_indicators(bars: list[dict[str, Any]]) -> pd.DataFrame:
    df = _bars_to_df(bars)
    df["ema_fast"] = _ema(df["close"], EMA_FAST)
    df["ema_slow"] = _ema(df["close"], EMA_SLOW)
    df["atr"] = _atr(df, ATR_PERIOD)
    df["adx"] = _adx(df, ADX_PERIOD)
    df["vol_ma"] = df["volume"].rolling(VOL_MA).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma"]
    return df


def htf_trend_at(df_1h: pd.DataFrame, ts_ms: int) -> int | None:
    """Point-in-time 1h trend at a 5m bar's close time. Returns +1 (up), -1
    (down), or None if not enough closed 1h history. Uses ONLY 1h bars that had
    already CLOSED at ts_ms (no in-progress 1h leak)."""
    closed = df_1h[df_1h["ts_ms"] <= ts_ms]
    if len(closed) < EMA_SLOW:
        return None
    row = closed.iloc[-1]
    ef, es = row["ema_fast"], row["ema_slow"]
    if pd.isna(ef) or pd.isna(es):
        return None
    return 1 if ef > es else -1


def signal_at(df: pd.DataFrame, i: int, df_1h: pd.DataFrame) -> dict[str, Any] | None:
    """Signal from the just-closed bar i. Entry is bar i+1 open. No-lookahead:
    uses only rows <= i and 1h bars closed by df.ts_ms[i]."""
    if i < max(EMA_SLOW, ADX_PERIOD, VOL_MA) + 1 or i + 1 >= len(df):
        return None
    cur = df.iloc[i]
    prev = df.iloc[i - 1]
    ef, es, adx_v, atr_v, vr = cur["ema_fast"], cur["ema_slow"], cur["adx"], cur["atr"], cur["vol_ratio"]
    if any(pd.isna(x) for x in (ef, es, adx_v, atr_v, vr)):
        return None  # fail-open: missing data -> no trade
    if adx_v < ADX_MIN or vr < VOL_MIN or atr_v <= 0:
        return None
    if abs(cur["close"] - ef) / atr_v > OVEREXT_ATR:
        return None  # overextended
    htf = htf_trend_at(df_1h, int(cur["ts_ms"]))
    if htf is None:
        return None

    prev_ema = prev["ema_fast"]
    if pd.isna(prev_ema):
        return None
    # Pullback-reclaim: prior bar dipped to/below its EMA20, current bar reclaims
    # above EMA20 with a bullish (LONG) / bearish (SHORT) close.
    long_ok = (ef > es and htf == 1 and prev["low"] <= prev_ema
               and cur["close"] > ef and cur["close"] > cur["open"])
    short_ok = (ef < es and htf == -1 and prev["high"] >= prev_ema
                and cur["close"] < ef and cur["close"] < cur["open"])
    if long_ok:
        side = "LONG"
    elif short_ok:
        side = "SHORT"
    else:
        return None
    return {"side": side, "index": i + 1, "feature_ts": cur["close_time"],
            "atr": float(atr_v), "ref_close": float(cur["close"])}


def _apply_slip(price: float, side: str, bps: Decimal, *, entry: bool) -> float:
    """Adverse slippage: entry fills worse, exit fills worse."""
    factor = float(bps) / 10000.0
    if entry:
        return price * (1 + factor) if side == "LONG" else price * (1 - factor)
    return price * (1 - factor) if side == "LONG" else price * (1 + factor)


def simulate_trade(df: pd.DataFrame, sig: dict[str, Any], quote_volume_24h: float,
                   exit_cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Multi-bar bracket from entry (bar sig['index'] open) forward. Pessimistic:
    if a bar spans both SL and TP, assume SL first. Real tiered costs + funding.

    exit_cfg (optional) parameterizes the exit for the sweep harness:
      sl_atr, tp_atr, min_rr, regime_exit (bool), adx_exit, max_hold_bars.
    Defaults reproduce the original hard-coded behavior."""
    cfg = exit_cfg or {}
    sl_atr = float(cfg.get("sl_atr", SL_ATR_MULT))
    tp_atr = float(cfg.get("tp_atr", TP_ATR_MULT))
    min_rr = float(cfg.get("min_rr", 1.5))
    use_regime_exit = bool(cfg.get("regime_exit", True))
    adx_exit = float(cfg.get("adx_exit", 20))
    max_hold = int(cfg.get("max_hold_bars", MAX_HOLD_BARS))
    idx = sig["index"]
    if idx >= len(df):
        return None
    side = sig["side"]
    entry_bar = df.iloc[idx]
    tier = liquidity_tier(quote_volume_24h)
    entry_px = _apply_slip(float(entry_bar["open"]), side, fill_bps(tier), entry=True)
    atr = sig["atr"]
    if side == "LONG":
        sl = entry_px - sl_atr * atr
        tp = entry_px + tp_atr * atr
    else:
        sl = entry_px + sl_atr * atr
        tp = entry_px - tp_atr * atr
    rr = abs(tp - entry_px) / max(1e-12, abs(entry_px - sl))
    if rr < min_rr:
        return None

    entry_ts = int(entry_bar["ts_ms"])
    exit_px = None
    reason = None
    exit_ts = entry_ts
    end = min(len(df), idx + 1 + max_hold)
    for j in range(idx, end):
        bar = df.iloc[j]
        hi, lo = float(bar["high"]), float(bar["low"])
        # regime exit on close: EMA flip or ADX death
        if side == "LONG":
            hit_sl = lo <= sl
            hit_tp = hi >= tp
        else:
            hit_sl = hi >= sl
            hit_tp = lo <= tp
        if hit_sl and hit_tp:  # pessimistic tie-break
            exit_px = _apply_slip(sl, side, fill_bps(tier, is_stop=True), entry=False); reason = "sl"; exit_ts = int(bar["ts_ms"]); break
        if hit_sl:
            exit_px = _apply_slip(sl, side, fill_bps(tier, is_stop=True), entry=False); reason = "sl"; exit_ts = int(bar["ts_ms"]); break
        if hit_tp:
            exit_px = _apply_slip(tp, side, fill_bps(tier), entry=False); reason = "tp"; exit_ts = int(bar["ts_ms"]); break
        # regime exit (only after the entry bar)
        if use_regime_exit and j > idx and not pd.isna(bar["ema_fast"]) and not pd.isna(bar["ema_slow"]):
            flip = bar["ema_fast"] < bar["ema_slow"] if side == "LONG" else bar["ema_fast"] > bar["ema_slow"]
            if flip or (not pd.isna(bar["adx"]) and bar["adx"] < adx_exit):
                exit_px = _apply_slip(float(bar["close"]), side, fill_bps(tier), entry=False); reason = "regime_exit"; exit_ts = int(bar["ts_ms"]); break
    if exit_px is None:  # time stop
        last = df.iloc[end - 1]
        exit_px = _apply_slip(float(last["close"]), side, fill_bps(tier), entry=False); reason = "timeout"; exit_ts = int(last["ts_ms"])

    # gross return in R units (per 1 unit qty, normalized to risk)
    risk_per_unit = abs(entry_px - sl)
    gross = (exit_px - entry_px) if side == "LONG" else (entry_px - exit_px)
    # fees: taker both legs on notional (use price*1 unit); express in price terms
    fee = (entry_px + abs(exit_px)) * float(TAKER_FEE_RATE)
    # funding: one interval per 8h boundary crossed (pessimistic, charged against)
    intervals = max(0, (exit_ts // FUNDING_INTERVAL_MS) - (entry_ts // FUNDING_INTERVAL_MS))
    funding = entry_px * float(FUNDING_RATE) * intervals
    net = gross - fee - funding
    r_multiple = net / risk_per_unit if risk_per_unit > 0 else 0.0
    return {"side": side, "reason": reason, "entry": entry_px, "exit": exit_px, "sl": sl, "tp": tp,
            "gross": gross, "fee": fee, "funding": funding, "net": net, "r_multiple": r_multiple,
            "tier": tier, "entry_ts": entry_ts, "exit_ts": exit_ts, "bars_held": (exit_ts - entry_ts) // 300000}


def backtest_symbol(bars_5m: list[dict[str, Any]], bars_1h: list[dict[str, Any]], quote_volume_24h: float,
                    *, start_ts_ms: int | None = None, end_ts_ms: int | None = None,
                    signal_fn: Any | None = None, exit_cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Run a setup over a 5m series (optionally restricted to [start,end) by bar
    close time — for train/holdout split). Returns closed trades.

    signal_fn(df, i, df_1h)->sig|None lets the sweep harness inject a compiled
    strategy; default is the built-in EMA-pullback signal_at. exit_cfg
    parameterizes the bracket exit (sl_atr/tp_atr/etc)."""
    sig_fn = signal_fn or signal_at
    df = compute_indicators(bars_5m)
    df_1h = compute_indicators(bars_1h)
    trades: list[dict[str, Any]] = []
    ts_to_idx = {int(t): k for k, t in enumerate(df["ts_ms"].tolist())}
    i = max(EMA_SLOW, ADX_PERIOD, VOL_MA) + 1
    while i < len(df) - 1:
        ts = int(df.iloc[i]["ts_ms"])
        if start_ts_ms is not None and ts < start_ts_ms:
            i += 1; continue
        if end_ts_ms is not None and ts >= end_ts_ms:
            break
        sig = sig_fn(df, i, df_1h)
        if sig:
            tr = simulate_trade(df, sig, quote_volume_24h, exit_cfg)
            if tr:
                # EMBARGO: an in-sample trade (end_ts_ms set) must fully CLOSE
                # before the split, otherwise its exit scan consumes holdout-window
                # price data and biases in-sample expectancy (a sweep would select
                # on that leak). Drop trades that exit at/after the split boundary.
                if end_ts_ms is not None and int(tr["exit_ts"]) >= int(end_ts_ms):
                    break  # this and all later signals would also cross the seam
                trades.append(tr)
                # no overlapping trade on the same symbol: resume after the exit bar
                exit_idx = ts_to_idx.get(int(tr["exit_ts"]), i + 1)
                i = max(i + 1, exit_idx + 1)
                continue
        i += 1
    return trades
