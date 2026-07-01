"""Edge-research harness — order-flow data path (HARNESS-A, backtestable subset).

The backtestable order-flow signals are CVD (volume delta) and funding. Per the
data-feasibility audit:
- CVD: derived per-bar from kline taker-buy volume (Binance kline index 9/10) —
  NO aggTrades needed, so it's cheap and backtestable over months.
- funding: futures_funding_rate has deep history; joined point-in-time.
- OI: futures_open_interest_hist has only ~30 days -> used as an optional regime
  FEATURE, never a primary signal (history too shallow to backtest reliably).

All features are no-lookahead: a bar's CVD uses only that bar's own taker-buy;
funding at a bar uses the last funding event with fundingTime <= bar close.
"""
from __future__ import annotations

import time
from typing import Any

import pandas as pd

_TF_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
_MAX_LIMIT = 1000


def _iso_ms(ms: int) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat(timespec="seconds")


def fetch_klines_with_flow(symbol: str, timeframe: str, *, months: float, end_ms: int,
                           client: Any, sleep_between: float = 0.02) -> list[dict[str, Any]]:
    """Page CLOSED klines and KEEP taker-buy volume so CVD can be computed. Only
    is_final bars (close_time < end_ms). Returns bars with open/high/low/close/
    volume/quote_volume/taker_buy_base/close_time/ts_ms."""
    symbol = symbol.upper()
    tf_ms = _TF_MS[timeframe]
    start_ms = end_ms - int(months * 30 * 24 * 3600 * 1000)
    seen: dict[int, dict[str, Any]] = {}
    cursor = start_ms
    guard = 0
    while cursor < end_ms and guard < 6000:
        guard += 1
        rows = client.futures_klines(symbol=symbol, interval=timeframe, startTime=cursor, limit=_MAX_LIMIT)
        if not rows:
            break
        for r in rows:
            close_time = int(r[6])
            if close_time >= end_ms:      # not yet closed relative to the run cutoff
                continue
            open_time = int(r[0])
            seen[open_time] = {
                # ISO strings so compute_indicators parses ts_ms correctly; ts_ms
                # int kept for compute_cvd_columns. Both built from these same bars.
                "open_time": _iso_ms(open_time), "close_time": _iso_ms(close_time),
                "ts_ms": close_time,
                "open": float(r[1]), "high": float(r[2]), "low": float(r[3]), "close": float(r[4]),
                "volume": float(r[5]), "quote_volume": float(r[7]),
                "taker_buy_base": float(r[9]), "taker_buy_quote": float(r[10]),
                "is_final": True,
            }
        last_open = int(rows[-1][0])
        nxt = last_open + tf_ms
        if nxt <= cursor:
            break
        cursor = nxt
        if len(rows) < _MAX_LIMIT:
            break
        if sleep_between:
            time.sleep(sleep_between)
    return [seen[k] for k in sorted(seen)]


def compute_cvd_columns(bars: list[dict[str, Any]]) -> pd.DataFrame:
    """Per-bar CVD delta + rolling/cumulative, all no-lookahead.
    cvd_delta = taker_buy_base - (volume - taker_buy_base) = 2*taker_buy - volume."""
    df = pd.DataFrame(bars)
    if df.empty:
        return df
    df = df.sort_values("ts_ms").reset_index(drop=True)
    vol = df["volume"].astype(float)
    tbb = df["taker_buy_base"].astype(float)
    df["cvd_delta"] = 2.0 * tbb - vol
    # buy fraction of volume (0..1); 0.5 = balanced
    df["buy_frac"] = (tbb / vol.replace(0, float("nan"))).fillna(0.5)
    # rolling cvd sum over a trailing window (trend of aggression)
    df["cvd_roll20"] = df["cvd_delta"].rolling(20, min_periods=5).sum()
    # normalized: cvd_delta relative to trailing volume (comparable across coins)
    df["cvd_delta_norm"] = df["cvd_delta"] / vol.rolling(20, min_periods=5).mean().replace(0, float("nan"))
    return df


def fetch_funding_series(symbol: str, *, months: float, end_ms: int, client: Any) -> list[dict[str, Any]]:
    """Funding rate history (deep). Returns [{fundingTime, fundingRate}] sorted."""
    symbol = symbol.upper()
    start_ms = end_ms - int(months * 30 * 24 * 3600 * 1000)
    seen: dict[int, float] = {}
    cursor = start_ms
    guard = 0
    while cursor < end_ms and guard < 400:
        guard += 1
        rows = client.futures_funding_rate(symbol=symbol, startTime=cursor, endTime=end_ms, limit=1000)
        if not rows:
            break
        for r in rows:
            seen[int(r["fundingTime"])] = float(r["fundingRate"])
        last = int(rows[-1]["fundingTime"])
        if last <= cursor:
            break
        cursor = last + 1
        if len(rows) < 1000:
            break
    return [{"fundingTime": t, "fundingRate": seen[t]} for t in sorted(seen)]


CVD_COLS = ("cvd_delta", "buy_frac", "cvd_roll20", "cvd_delta_norm", "funding_rate")


def enrich_indicator_df(indicator_df: pd.DataFrame, flow_bars: list[dict[str, Any]],
                        funding: list[dict[str, Any]]) -> pd.DataFrame:
    """Attach CVD + funding columns to an indicator df (from compute_indicators).
    The indicator df and the flow bars are built from the SAME klines in ascending
    time order, so alignment is positional when lengths match (robust to differing
    ts_ms parsing between paths); otherwise fall back to a ts_ms join. No-lookahead:
    cvd columns are per-bar causal, funding joined point-in-time."""
    flow = compute_cvd_columns(flow_bars)
    flow = join_funding_point_in_time(flow, funding)
    out = indicator_df.copy().reset_index(drop=True)
    if flow.empty:
        for col in CVD_COLS:
            out[col] = 0.0 if col == "funding_rate" else float("nan")
        return out
    if len(flow) == len(out):
        for col in CVD_COLS:
            out[col] = flow[col].to_numpy() if col in flow.columns else float("nan")
        return out
    # length mismatch -> align by ts_ms (assumes both ts_ms are true epoch ms)
    flow_idx = flow.set_index("ts_ms")
    ts = out["ts_ms"].to_numpy()
    for col in CVD_COLS:
        src = flow_idx[col] if col in flow_idx.columns else None
        out[col] = ([src.get(t, float("nan")) for t in ts] if src is not None else float("nan"))
    return out


def join_funding_point_in_time(df: pd.DataFrame, funding: list[dict[str, Any]]) -> pd.DataFrame:
    """Attach the last funding rate with fundingTime <= bar close (no lookahead)."""
    if df.empty or not funding:
        df["funding_rate"] = 0.0
        return df
    import numpy as np
    ft = np.array([f["fundingTime"] for f in funding])
    fr = np.array([f["fundingRate"] for f in funding])
    idx = np.searchsorted(ft, df["ts_ms"].to_numpy(), side="right") - 1
    vals = np.where(idx >= 0, fr[np.clip(idx, 0, len(fr) - 1)], 0.0)
    df = df.copy()
    df["funding_rate"] = vals
    return df
