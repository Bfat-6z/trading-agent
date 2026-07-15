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

import threading
import time
from typing import Any

import pandas as pd
import requests

_DERIV_BASE = "https://fapi.binance.com/futures/data"


def _bounded_get(url: str, params: dict, hard_deadline: float = 10.0):
    """requests.get that CANNOT hang the caller (ck:debug 2026-07-08 root cause: the SSL
    handshake to fapi.binance.com/futures/data blocked run_once() >70s despite timeout=10 —
    the socket timeout doesn't reliably cover the TLS handshake on Windows). Runs in a daemon
    thread; if it doesn't finish within hard_deadline the thread is abandoned and we return
    None (fail-soft, caller degrades to no-deriv). The thread timeout is the GUARANTEE."""
    box: list = [None]

    def _run():
        try:
            box[0] = requests.get(url, params=params, timeout=(4, 6)).json()
        except Exception:
            box[0] = None
    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(hard_deadline)
    return box[0]                       # None = timed out (thread left to die) or errored


def fetch_deriv_series(symbol: str, timeframe: str, *, start_ms: int, end_ms: int) -> dict[int, dict[str, float]]:
    """Open-interest + top-trader long/short ACCOUNT ratio history, paged. Binance
    retains only ~30d of this data, so start is clamped. Returns {ts_ms: {"oi":..,
    "ls":..}} (sparse — the caller forward-fills onto bars point-in-time, no lookahead).
    Public futures/data endpoints (no auth). Fail-soft: partial/empty on any error."""
    symbol = symbol.upper()
    # Binance keeps only ~500 rows of these; at 15m that's ~5d (too shallow for backtest),
    # so use a COARSER 1h period (~20d of history) and forward-fill onto the 15m bars —
    # OI/positioning is a slow regime signal, hourly granularity is plenty and causal.
    dperiod = "1h" if timeframe in ("5m", "15m", "30m") else timeframe
    start = max(start_ms, end_ms - 30 * 24 * 3600 * 1000 + 60_000)
    out: dict[int, dict[str, float]] = {}
    _t0 = time.time()
    for ep, key, field in (("openInterestHist", "oi", "sumOpenInterest"),
                           ("topLongShortAccountRatio", "ls", "longShortRatio")):
        cursor, guard = start, 0
        while cursor < end_ms and guard < 100:
            if time.time() - _t0 > 25.0:          # total wall-clock budget for the whole call
                break                              # (never let deriv fetch dominate a cycle)
            guard += 1
            rows = _bounded_get(f"{_DERIV_BASE}/{ep}",
                                {"symbol": symbol, "period": dperiod, "limit": 500,
                                 "startTime": cursor, "endTime": end_ms})
            if not isinstance(rows, list) or not rows:
                break
            for r in rows:
                try:
                    out.setdefault(int(r["timestamp"]), {})[key] = float(r[field])
                except Exception:
                    pass
            last = int(rows[-1]["timestamp"])
            if last <= cursor:
                break
            cursor = last + 1
            if len(rows) < 500:
                break
            time.sleep(0.05)
    return dict(sorted(out.items()))

_TF_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000}
_MAX_LIMIT = 1000


def _iso_ms(ms: int) -> str:
    # MILLISECOND precision on purpose: compute_indicators derives ts_ms by parsing
    # this ISO string, so truncating to seconds would make the indicator df's ts_ms
    # (…000) disagree with the flow df's raw ts_ms (…999) and break the enrich join.
    import datetime
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat(timespec="milliseconds")


# ===========================================================================
# CROSS-PROCESS klines cache + shared ban-backoff (2026-07-16, spam-agent design).
# ROOT CAUSE of the recurring Binance -1003 IP bans: ~10 agents (mission, lane_farm,
# shadow_eval, forward_test, observers...) each fetch the SAME hot coins' SAME bar every
# cycle, on ONE IP, no shared cache, no shared backoff -> aggregate weight blows past
# ~2400/min. Fix: (a) per-bar per-key on-disk cache -> same (coin,tf,bar) fetched by N
# agents = ONE Binance call; (b) a shared backoff file -> a ban makes the WHOLE fleet
# stand down instead of retry-hammering. Public signature UNCHANGED (all 20+ callers keep
# working); backtests (months>2) bypass the cache but still honor the backoff. Cache
# fail-OPEN (miss/error -> direct fetch = old behavior); backoff fail-CLOSED (return []
# during a ban -> callers already treat empty as skip-symbol).
# ===========================================================================
import os as _os
import random as _random
from pathlib import Path as _Path
try:
    from binance.exceptions import BinanceAPIException as _BinanceAPIException
except Exception:                                   # pragma: no cover
    class _BinanceAPIException(Exception):
        status_code = None
        code = None

_CACHE_DIR = _Path(__file__).resolve().parent / "state" / "klines_cache"
_BACKOFF_FILE = _CACHE_DIR / "_backoff.json"
_MONTHS_TIERS = (0.12, 0.5, 1.0, 2.0)               # hot windows collapse to these; >2.0 = backtest = bypass
_CACHE_MAX_MONTHS = 2.0


def _months_bucket(months):
    m = float(months)
    if m > _CACHE_MAX_MONTHS + 1e-9:
        return None                                 # backtest window -> not cacheable
    for t in _MONTHS_TIERS:
        if m <= t * 1.000001:
            return t
    return None


def _bar_cache_key(symbol, timeframe, months, end_ms, with_deriv):
    bucket = _months_bucket(months)
    if bucket is None:
        return None
    tf_ms = _TF_MS[timeframe]
    bar_idx = int(end_ms) // tf_ms                   # right-edge bar -> auto-invalidates each bar
    return "%s_%s_%s_%d_%d" % (str(symbol).upper(), timeframe, bucket, bar_idx, int(bool(with_deriv)))


def _klines_backoff_active(now=None):
    try:
        from atomic_state import read_json
        p = read_json(_BACKOFF_FILE, default=None) or {}
        now = time.time() if now is None else now
        return float(p.get("backoff_until_epoch") or 0) > now
    except Exception:
        return False


def _record_klines_ban(exc):
    try:
        from atomic_state import read_json, write_json_atomic
        prev = float((read_json(_BACKOFF_FILE, default=None) or {}).get("cooldown_seconds") or 0)
        ra = getattr(exc, "retry_after", None)
        cooldown = float(ra) if ra else min(600.0, max(120.0, prev * 2 or 120.0))   # expo 120->600s
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        write_json_atomic(_BACKOFF_FILE, {"backoff_until_epoch": time.time() + cooldown,
            "cooldown_seconds": cooldown, "status_code": getattr(exc, "status_code", None),
            "reason": repr(exc)[:180]})
    except Exception:
        pass


def _is_ban(exc):
    return getattr(exc, "status_code", None) in (418, 429) or getattr(exc, "code", None) == -1003


def _sweep_stale_klines(timeframe, end_ms):
    try:
        cur = int(end_ms) // _TF_MS[timeframe]
        for f in _CACHE_DIR.glob("*_%s_*.json" % timeframe):   # Codex #5: ONLY this timeframe's
            if f.name == "_backoff.json":                       # keys — a 15m sweep must not evict
                continue                                        # valid current 1h/4h/1d cache files
            parts = f.stem.split("_")
            if len(parts) < 5 or parts[1] != timeframe:         # key = SYM_tf_bucket_baridx_deriv
                continue
            try:
                idx = int(parts[-2])
            except Exception:
                continue
            if idx < cur - 1:
                f.unlink(missing_ok=True)
    except Exception:
        pass


def fetch_klines_with_flow(symbol: str, timeframe: str, *, months: float, end_ms: int,
                           client: Any, sleep_between: float = 0.02,
                           with_deriv: bool = False) -> list[dict[str, Any]]:
    """Cross-process cached + ban-backoff wrapper (public signature unchanged). See the
    block above. Delegates to _fetch_klines_with_flow_direct for the real fetch."""
    key = _bar_cache_key(symbol, timeframe, months, end_ms, with_deriv)
    if key is None:                                  # backtest window -> bypass cache, keep ban gate
        if _klines_backoff_active():
            return []
        try:
            return _fetch_klines_with_flow_direct(symbol, timeframe, months=months, end_ms=end_ms,
                                                  client=client, sleep_between=sleep_between, with_deriv=with_deriv)
        except _BinanceAPIException as exc:          # Codex CRITICAL #4: a ban during a months=5
            if _is_ban(exc):                          # backtest must ALSO stand the fleet down
                _record_klines_ban(exc)
            raise
    _start_ms = int(end_ms) - int(months * 30 * 24 * 3600 * 1000)   # caller's exact window start
    try:                                             # --- cache read (fail-open) ---
        from atomic_state import read_json
        hit = read_json(_CACHE_DIR / (key + ".json"), default=None)
        if isinstance(hit, list):
            return [b for b in hit if int(b.get("ts_ms", 0)) >= _start_ms]   # strict slice -> exact window
    except Exception:
        pass
    if _klines_backoff_active():                     # --- ban gate: fail-CLOSED ---
        return []
    # Codex CRITICAL #3: serialize the first-of-bar miss so ~20 agents don't all fetch the
    # same key at once (which would recreate the burst). O_EXCL claim lock; losers wait for
    # the winner's cache write, then fail-open to a direct fetch if it never appears.
    _cache_path = _CACHE_DIR / (key + ".json")
    _lock = _CACHE_DIR / (key + ".lock")
    _got_lock = False
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            _fd = _os.open(str(_lock), _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY)
            _os.close(_fd)
            _got_lock = True
        except FileExistsError:
            try:                                     # steal a STALE lock (dead writer) after 10s
                if time.time() - _lock.stat().st_mtime > 10:
                    _lock.unlink(missing_ok=True)
            except Exception:
                pass
            if not _got_lock:
                from atomic_state import read_json as _rj
                for _ in range(20):                  # wait up to ~2s for the winner's write
                    time.sleep(0.1)
                    try:
                        h = _rj(_cache_path, default=None)
                        if isinstance(h, list):
                            return [b for b in h if int(b.get("ts_ms", 0)) >= _start_ms]
                    except Exception:
                        pass
        except Exception:
            pass
        # Codex #2: fetch a DETERMINISTIC window per (tier, bar) — normalize end to the bar
        # boundary so every caller in this bar fetches the byte-identical superset payload.
        eff_months = _months_bucket(months)
        _end_norm = (int(end_ms) // _TF_MS[timeframe]) * _TF_MS[timeframe]
        try:
            bars = _fetch_klines_with_flow_direct(symbol, timeframe, months=eff_months, end_ms=_end_norm,
                                                  client=client, sleep_between=sleep_between, with_deriv=with_deriv)
        except _BinanceAPIException as exc:
            if _is_ban(exc):
                _record_klines_ban(exc)              # whole fleet stands down
            raise                                    # preserve caller semantics (skip symbol)
        try:                                         # --- cache write (fail-open) ---
            # Codex #2: never cache a DEGRADED with_deriv payload (deriv fetch fail-soft ->
            # no oi/ls) over a potentially-richer one; skip the write, next call retries.
            _deriv_ok = (not with_deriv) or any("oi" in b for b in bars)
            if bars and _deriv_ok:
                from atomic_state import write_json_atomic
                write_json_atomic(_cache_path, bars)
                if _random.random() < 0.05:
                    _sweep_stale_klines(timeframe, end_ms)
        except Exception:
            pass
    finally:
        if _got_lock:
            try:
                _lock.unlink(missing_ok=True)
            except Exception:
                pass
    return [b for b in bars if int(b.get("ts_ms", 0)) >= _start_ms]   # slice tier to caller's window


def _fetch_klines_with_flow_direct(symbol: str, timeframe: str, *, months: float, end_ms: int,
                           client: Any, sleep_between: float = 0.02,
                           with_deriv: bool = False) -> list[dict[str, Any]]:
    """Page CLOSED klines and KEEP taker-buy volume so CVD can be computed. Only
    is_final bars (close_time < end_ms). Returns bars with open/high/low/close/
    volume/quote_volume/taker_buy_base/close_time/ts_ms. with_deriv=True also attaches
    point-in-time open-interest (`oi`) + long/short ratio (`ls_ratio`) forward-filled
    onto each bar (fail-soft; extra API weight — only the strategy paths enable it)."""
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
    bars = [seen[k] for k in sorted(seen)]
    if with_deriv and bars:
        try:                                          # forward-fill OI + L/S onto bars
            deriv = fetch_deriv_series(symbol, timeframe, start_ms=start_ms, end_ms=end_ms)
            dts = sorted(deriv)
            di, last_oi, last_ls = 0, None, None
            for b in bars:
                bt = int(b["ts_ms"])
                while di < len(dts) and dts[di] <= bt:   # only prints AT/BEFORE this bar close
                    dv = deriv[dts[di]]
                    if "oi" in dv:
                        last_oi = dv["oi"]
                    if "ls" in dv:
                        last_ls = dv["ls"]
                    di += 1
                if last_oi is not None:
                    b["oi"] = last_oi
                if last_ls is not None:
                    b["ls_ratio"] = last_ls
        except Exception:
            pass
    return bars


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
    # normalized: cvd_delta relative to trailing volume (comparable across coins).
    # bughunt 2026-07-08 + re-audit: clip ±10 + fill 0 as a robustness bound. NOTE (honest): the
    # mechanical method path serves cvd_delta_norm via method_lab.feature_frame on BOTH backtest and
    # live, so there was no mechanical train/serve skew; feature_frame's version differs slightly in
    # warmup (÷20 convolution vs min_periods, and a `volma>1e-9` vs `replace(0,nan)` near-zero band),
    # which only affects the first ~19 bars — never the evaluated last bar of a real series. This clip
    # just bounds outliers for compute_cvd_columns's own consumers (discretionary snapshot, strategy_blocks).
    df["cvd_delta_norm"] = (df["cvd_delta"] / vol.rolling(20, min_periods=5).mean().replace(0, float("nan"))
                            ).clip(-10.0, 10.0).fillna(0.0)
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
    """Attach CVD + funding columns to an indicator df (from compute_indicators),
    aligned STRICTLY by ts_ms and FAIL-CLOSED: every indicator bar must have a
    matching flow bar (same ts_ms), else raise. This makes silent misalignment
    (the old positional shortcut = latent lookahead) impossible. No-lookahead: cvd
    columns are per-bar causal, funding joined point-in-time."""
    out = indicator_df.copy().reset_index(drop=True)
    if not flow_bars:
        raise ValueError("enrich_indicator_df: no flow_bars provided (order-flow spec cannot be evaluated)")
    flow = compute_cvd_columns(flow_bars)
    flow = join_funding_point_in_time(flow, funding)
    if flow.empty:
        raise ValueError("enrich_indicator_df: flow frame empty after CVD compute")
    # exact ts_ms join
    flow_pos = {int(t): i for i, t in enumerate(flow["ts_ms"].to_numpy())}
    out_ts = [int(t) for t in out["ts_ms"].to_numpy()]
    idxs = [flow_pos.get(t) for t in out_ts]
    unmatched = sum(1 for x in idxs if x is None)
    if unmatched:
        raise ValueError(f"enrich_indicator_df: {unmatched}/{len(idxs)} indicator bars have no matching "
                         f"flow bar by ts_ms — refusing to enrich (would be a silent lookahead/NaN). "
                         f"Ensure both dfs come from the same flow bars with canonical ts_ms.")
    for col in CVD_COLS:
        if col in flow.columns:
            arr = flow[col].to_numpy()
            out[col] = [arr[i] for i in idxs]
        else:
            out[col] = 0.0 if col == "funding_rate" else float("nan")
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
