"""Method Lab — the honest autonomous research -> test -> curate loop.

A candidate trading "method" is a serializable predicate (a small, safe DSL over
per-bar features — NOT arbitrary code), so methods can be GENERATED (by an LLM
researching how others trade) and persisted, never hand-blessed. Each method is
walk-forward backtested across many coins on REAL history with a pessimistic cost
model, then scored with the same bootstrap-CI + permutation-p scorecard the live
bot uses. A method SURVIVES only if it is positive out-of-sample AND its p-value
beats a Bonferroni-corrected bar (guards the data-mining trap of testing many
rules). Survivors feed the live LLM prompt; losers are logged with why.

No method is trusted because it sounds good — only because it survived the data.
PAPER / OFFLINE only: pure compute over historical klines, never places an order.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import numpy as np

import llm_trader_scorecard as ls

ROOT = Path(__file__).resolve().parent
LAB_DIR = ROOT / "state" / "method_lab"
SURVIVORS = LAB_DIR / "survivors.json"
KILLED = LAB_DIR / "killed.jsonl"
LEDGER = LAB_DIR / "ledger.json"

# ---------------------------------------------------------------------------
# per-bar features (self-contained; mirror the live bot's read)
# ---------------------------------------------------------------------------

def _ema(x: np.ndarray, p: int) -> np.ndarray:
    a = 2.0 / (p + 1.0)
    out = np.empty_like(x, dtype=float)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def _rsi(c: np.ndarray, p: int = 14) -> np.ndarray:
    n = len(c)
    out = np.full(n, 50.0)
    if n <= p:
        return out
    d = np.diff(c)
    g = np.where(d > 0, d, 0.0)
    l = np.where(d < 0, -d, 0.0)
    ag, al = g[:p].mean(), l[:p].mean()
    for i in range(p, n):
        if i > p:
            ag = (ag * (p - 1) + g[i - 1]) / p
            al = (al * (p - 1) + l[i - 1]) / p
        if ag <= 1e-12 and al <= 1e-12:
            out[i] = 50.0
            continue
        out[i] = 100.0 - 100.0 / (1.0 + ag / (al if al > 1e-12 else 1e-12))
    return out


def _wilder_atr(h: np.ndarray, lo: np.ndarray, c: np.ndarray, p: int = 14) -> np.ndarray:
    n = len(c)
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1]))
    atr = np.zeros(n)
    if n > p:
        atr[p] = tr[1:p + 1].mean()
        for i in range(p + 1, n):
            atr[i] = (atr[i - 1] * (p - 1) + tr[i]) / p
    return atr


def _adx(h: np.ndarray, lo: np.ndarray, c: np.ndarray, p: int = 14) -> np.ndarray:
    """Wilder ADX (trend strength 0-100). No lookahead: each i uses only data <= i."""
    n = len(c)
    out = np.zeros(n)
    if n < 2 * p + 2:
        return out
    tr = np.zeros(n); pdm = np.zeros(n); ndm = np.zeros(n)
    for i in range(1, n):
        up = h[i] - h[i - 1]; dn = lo[i - 1] - lo[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        ndm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1]))
    atr = tr[1:p + 1].sum(); sp = pdm[1:p + 1].sum(); sn = ndm[1:p + 1].sum()
    dx = np.zeros(n)
    for i in range(p + 1, n):
        atr = atr - atr / p + tr[i]
        sp = sp - sp / p + pdm[i]
        sn = sn - sn / p + ndm[i]
        if atr > 1e-12:
            pdi = 100 * sp / atr; ndi = 100 * sn / atr
            s = pdi + ndi
            dx[i] = 100 * abs(pdi - ndi) / s if s > 1e-12 else 0.0
    out[2 * p] = dx[p + 1:2 * p + 1].mean()
    for i in range(2 * p + 1, n):
        out[i] = (out[i - 1] * (p - 1) + dx[i]) / p
    return out


def _supertrend(h: np.ndarray, lo: np.ndarray, c: np.ndarray, p: int = 10, mult: float = 3.0) -> np.ndarray:
    """SuperTrend direction (+1 up / -1 down). No lookahead (band at i uses close[i], prior band)."""
    n = len(c)
    out = np.zeros(n)
    if n < p + 2:
        return out
    atr = _wilder_atr(h, lo, c, p)
    hl2 = (h + lo) / 2.0
    ub = np.zeros(n); lb = np.zeros(n); d = np.ones(n, dtype=int)
    for i in range(p, n):
        bub = hl2[i] + mult * atr[i]; blb = hl2[i] - mult * atr[i]
        ub[i] = bub if (ub[i - 1] == 0 or bub < ub[i - 1] or c[i - 1] > ub[i - 1]) else ub[i - 1]
        lb[i] = blb if (lb[i - 1] == 0 or blb > lb[i - 1] or c[i - 1] < lb[i - 1]) else lb[i - 1]
        d[i] = 1 if c[i] > ub[i - 1] else (-1 if c[i] < lb[i - 1] else d[i - 1])
        out[i] = d[i]
    return out


def feature_frame(bars: list[dict[str, Any]], funding: list | None = None) -> list[dict[str, float]]:
    """One feature row per bar (the columns methods reference). Values known AT
    THAT BAR's close — no lookahead. `funding` (optional): raw Binance funding
    prints; forward-filled per bar, z-scored over the trailing 90 prints (~30d),
    using only prints at/before the bar (no lookahead)."""
    if len(bars) < 60:
        return []
    fts: list[int] = []
    frs: list[float] = []
    if funding:
        pairs = []
        for f in funding:
            try:
                if isinstance(f, dict):
                    pairs.append((int(f["fundingTime"]), float(f["fundingRate"])))
                else:
                    pairs.append((int(f[0]), float(f[1])))
            except Exception:
                pass
        pairs.sort()
        fts = [t for t, _ in pairs]
        frs = [r for _, r in pairs]
    fidx = -1
    c = np.array([float(b["close"]) for b in bars])
    h = np.array([float(b["high"]) for b in bars])
    lo = np.array([float(b["low"]) for b in bars])
    v = np.array([float(b.get("volume") or 0.0) for b in bars])
    e20, e50, e200 = _ema(c, 20), _ema(c, 50), _ema(c, 200)
    rsi = _rsi(c)
    volma = np.convolve(v, np.ones(20) / 20, mode="full")[:len(v)]
    # 4h EMA cross (owner's method: "4h EMA cross -> dump"). Resample 15m->4h
    # (every 16 bars); state = fast>slow; cross fires on the first 15m bar of a new
    # 4h bucket where the state flipped. No lookahead: bucket k uses close[16k] (past).
    h4 = c[::16]
    if len(h4) >= 25:
        ef4, es4 = _ema(h4, 9), _ema(h4, 21)
        st4 = np.where(ef4 > es4, 1, -1)
    else:
        st4 = np.zeros(len(h4), dtype=int)
    # --- extended TA indicators (2026-07-07, owner: broaden the futures indicator set).
    # All from OHLCV up to & INCLUDING bar i (no lookahead); additive feature columns
    # the lane farm can then test empirically instead of trusting internet folklore.
    macd_line = _ema(c, 12) - _ema(c, 26)
    macd_hist = macd_line - _ema(macd_line, 9)
    adx14 = _adx(h, lo, c, 14)
    st_dir = _supertrend(h, lo, c, 10, 3.0)
    tp = (h + lo + c) / 3.0
    roc10 = np.zeros(len(c)); roc10[10:] = (c[10:] / c[:-10] - 1.0) * 100.0
    bb_pctb = np.full(len(c), 0.5); bb_width = np.zeros(len(c))
    stoch_k = np.full(len(c), 50.0); cci20 = np.zeros(len(c)); wr14 = np.full(len(c), -50.0)
    vwap20 = c.astype(float).copy()
    for _i in range(len(c)):
        if _i >= 19:
            w = c[_i - 19:_i + 1]; m = float(w.mean()); sdv = float(w.std())
            if sdv > 0:
                bb_pctb[_i] = float((c[_i] - (m - 2 * sdv)) / (4 * sdv))
                bb_width[_i] = float(4 * sdv / m * 100) if m else 0.0
            wt = tp[_i - 19:_i + 1]; mt = float(wt.mean()); md = float(np.abs(wt - mt).mean())
            cci20[_i] = float((tp[_i] - mt) / (0.015 * md)) if md > 0 else 0.0
            vw = v[_i - 19:_i + 1]; sv = float(vw.sum())
            vwap20[_i] = float((tp[_i - 19:_i + 1] * vw).sum() / sv) if sv > 0 else float(c[_i])
        if _i >= 13:
            hh = float(h[_i - 13:_i + 1].max()); ll = float(lo[_i - 13:_i + 1].min())
            if hh > ll:
                stoch_k[_i] = float((c[_i] - ll) / (hh - ll) * 100.0)
                wr14[_i] = float((c[_i] - hh) / (hh - ll) * 100.0)
    stoch_d = np.convolve(stoch_k, np.ones(3) / 3.0, mode="full")[:len(c)]
    rows = []
    for i in range(len(bars)):
        vr = float(v[i] / volma[i]) if i >= 20 and volma[i] > 0 else 1.0
        k = i // 16
        state4 = int(st4[k]) if k < len(st4) else 0
        cross4 = int(st4[k]) if (0 < k < len(st4) and i % 16 == 0 and st4[k] != st4[k - 1]) else 0
        # TikTok-research features (2026-07-04): session hour (UTC), 20-bar range
        # compression, and breakout vs the PRIOR 20-bar extreme (current bar
        # excluded from the reference -> no self-breakout, no lookahead).
        try:
            hour_utc = int((int(bars[i].get("ts_ms") or 0) // 3600000) % 24)
        except Exception:
            hour_utc = -1
        if i >= 21:
            hi20 = float(h[i - 20:i].max()); lo20 = float(lo[i - 20:i].min())
            rng20 = (hi20 - lo20) / c[i] * 100 if c[i] else 99.0
            brk20 = (c[i] / hi20 - 1) * 100 if hi20 else 0.0
            brkdn20 = (c[i] / lo20 - 1) * 100 if lo20 else 0.0
        else:
            rng20, brk20, brkdn20 = 99.0, 0.0, 0.0
        # Quant-harvest features (2026-07-05): capitulation-family expansion.
        # streaks: consecutive red/green CLOSES ending at bar i (incl. i).
        sd = su = 0
        j = i
        while j > 0 and c[j] < c[j - 1]:
            sd += 1; j -= 1
        j = i
        while j > 0 and c[j] > c[j - 1]:
            su += 1; j -= 1
        # drawdown vs the 96-bar (24h) rolling high / rally vs rolling low.
        if i >= 96:
            hi96 = float(h[i - 96:i + 1].max()); lo96 = float(lo[i - 96:i + 1].min())
            dd96 = (c[i] / hi96 - 1) * 100 if hi96 else 0.0
            ral96 = (c[i] / lo96 - 1) * 100 if lo96 else 0.0
        else:
            dd96, ral96 = 0.0, 0.0
        # ATR14 % of price (true-range EMA), day-of-week (0=Mon..6=Sun UTC).
        if i >= 15:
            trs = [max(h[k] - lo[k], abs(h[k] - c[k - 1]), abs(lo[k] - c[k - 1])) for k in range(i - 13, i + 1)]
            atrp = (sum(trs) / 14.0) / c[i] * 100 if c[i] else 0.0
        else:
            atrp = 0.0
        try:
            dow = int(((int(bars[i].get("ts_ms") or 0) // 86400000) + 4) % 7)   # epoch day 0 = Thu
        except Exception:
            dow = -1
        # signed streak (+up/-down), single-bar return in ATR units, close position
        streak = su if su > 0 else -sd
        ret1 = (c[i] / c[i - 1] - 1) * 100 if i >= 1 and c[i - 1] else 0.0
        bar_z = round(float(ret1 / atrp), 3) if atrp > 0.05 else 0.0
        rng_hl = h[i] - lo[i]
        close_pos = round(min(1.0, max(0.0, float((c[i] - lo[i]) / rng_hl))), 3) if rng_hl > 0 else 0.5
        # funding: forward-fill latest print at/before this bar; z over prior 90 prints
        f_bps = 0.0
        f_z = 0.0
        if fts:
            bts = int(bars[i].get("ts_ms") or 0)
            while fidx + 1 < len(fts) and fts[fidx + 1] <= bts:
                fidx += 1
            if fidx >= 0:
                f_bps = frs[fidx] * 10000.0
                w = frs[max(0, fidx - 89):fidx + 1]
                if len(w) >= 10:
                    mu = sum(w) / len(w)
                    var = sum((x - mu) ** 2 for x in w) / len(w)
                    sd_ = var ** 0.5
                    if sd_ > 1e-12:
                        f_z = (frs[fidx] - mu) / sd_
        rows.append({
            "streak": streak, "bar_z": bar_z, "close_pos": close_pos,
            "funding_rate_bps": round(float(f_bps), 3), "funding_z": round(float(f_z), 3),
            "dd_from_high96_pct": round(max(0.0, float(-dd96)), 3),   # positive % below the 24h high
            "streak_down": sd, "streak_up": su, "dd96_pct": round(float(dd96), 3),
            "rally96_pct": round(float(ral96), 3), "atr_pct": round(float(atrp), 3), "dow": dow,
            "hour_utc": hour_utc, "range20_pct": round(float(rng20), 3),
            "brk20_pct": round(float(brk20), 3), "brkdn20_pct": round(float(brkdn20), 3),
            "ema4h_state": state4, "ema4h_cross": cross4,
            "i": i, "close": float(c[i]), "high": float(h[i]), "low": float(lo[i]),
            "rsi14": float(rsi[i]),
            "px_vs_ema20": float(c[i] / e20[i] - 1) * 100 if e20[i] else 0.0,
            "px_vs_ema50": float(c[i] / e50[i] - 1) * 100 if e50[i] else 0.0,
            "px_vs_ema200": float(c[i] / e200[i] - 1) * 100 if e200[i] else 0.0,
            "ema_stack": (1 if c[i] > e20[i] > e50[i] else -1 if c[i] < e20[i] < e50[i] else 0),
            "vol_ratio": round(vr, 3),
            "ret5": float(c[i] / c[i - 5] - 1) * 100 if i >= 5 else 0.0,
            "ret20": float(c[i] / c[i - 20] - 1) * 100 if i >= 20 else 0.0,
            # --- extended TA (2026-07-07) ---
            "macd_hist": round(float(macd_hist[i]), 5),
            "macd_state": (1 if macd_hist[i] > 0 else -1),
            "adx": round(float(adx14[i]), 2),
            "supertrend_dir": int(st_dir[i]),
            "bb_pctb": round(float(bb_pctb[i]), 3),
            "bb_width_pct": round(float(bb_width[i]), 3),
            "stoch_k": round(float(stoch_k[i]), 2),
            "stoch_d": round(float(stoch_d[i]), 2),
            "cci20": round(float(cci20[i]), 2),
            "williams_r": round(float(wr14[i]), 2),
            "roc10": round(float(roc10[i]), 3),
            "px_vs_vwap20": round(float(c[i] / vwap20[i] - 1) * 100, 3) if vwap20[i] else 0.0,
        })
    return rows


# ---------------------------------------------------------------------------
# method DSL — a method is {id, name, entry conditions, side, sl_pct, tp_pct}
# condition = {"feat": <name>, "op": <one of>, "val": <number>}
# ---------------------------------------------------------------------------

_OPS: dict[str, Callable[[float, float], bool]] = {
    "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
}


def _cond_ok(row: dict[str, float], cond: dict[str, Any]) -> bool:
    f = cond.get("feat")
    op = _OPS.get(cond.get("op"))
    if f is None or op is None or f not in row:
        return False
    try:
        return op(float(row[f]), float(cond.get("val")))
    except Exception:
        return False


def method_fires(row: dict[str, float], method: dict[str, Any]) -> bool:
    conds = method.get("when") or []
    return bool(conds) and all(_cond_ok(row, c) for c in conds)


# ---------------------------------------------------------------------------
# backtest one method over one coin's history (one position at a time)
# ---------------------------------------------------------------------------

FEE_RT = 0.0008          # round-trip taker fee + slippage (~0.08%), pessimistic
TIMEOUT_BARS = 16        # ~4h on 15m


def backtest_method(rows: list[dict[str, float]], method: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    side = method.get("side", "LONG")
    sl_pct = float(method.get("sl_pct", 1.5)) / 100.0
    tp_pct = float(method.get("tp_pct", 2.5)) / 100.0
    trades: list[dict[str, Any]] = []
    i = 200                       # warmup (EMA200)
    n = len(rows)
    while i < n - 1:
        if not method_fires(rows[i], method):
            i += 1
            continue
        entry = rows[i]["close"]
        if side == "LONG":
            sl, tp = entry * (1 - sl_pct), entry * (1 + tp_pct)
        else:
            sl, tp = entry * (1 + sl_pct), entry * (1 - tp_pct)
        # walk forward bar-by-bar, pessimistic (SL checked before TP)
        exit_px, reason, j = None, None, i
        for j in range(i + 1, min(i + 1 + TIMEOUT_BARS, n)):
            hi, low = rows[j]["high"], rows[j]["low"]
            if side == "LONG":
                if low <= sl:
                    exit_px, reason = sl, "sl"; break
                if hi >= tp:
                    exit_px, reason = tp, "tp"; break
            else:
                if hi >= sl:
                    exit_px, reason = sl, "sl"; break
                if low <= tp:
                    exit_px, reason = tp, "tp"; break
        if exit_px is None:
            exit_px, reason = rows[min(i + TIMEOUT_BARS, n - 1)]["close"], "timeout"
            j = min(i + TIMEOUT_BARS, n - 1)
        gross = (exit_px / entry - 1) if side == "LONG" else (entry - exit_px) / entry
        net = gross - FEE_RT
        trades.append({"symbol": symbol, "side": side, "entry": entry, "exit": exit_px,
                       "reason": reason, "r": net / sl_pct, "net": net, "bar": i})
        i = j + 1               # no overlapping positions
    return trades


# ---------------------------------------------------------------------------
# curate: survive only if OOS-positive AND permutation p beats Bonferroni bar
# ---------------------------------------------------------------------------

def evaluate_method(method: dict[str, Any], frames: dict[str, list[dict[str, float]]],
                    n_methods: int = 1, oos_frac: float = 0.3) -> dict[str, Any]:
    """Backtest across all coins; split each coin's trades into in-sample (early)
    and out-of-sample (late 30%). Judge on OOS with the scorecard. Bonferroni:
    the p-value bar tightens by the number of methods tested this round."""
    all_tr: list[dict[str, Any]] = []
    for sym, rows in frames.items():
        all_tr.extend(backtest_method(rows, method, sym))
    if len(all_tr) < 20:
        return {"id": method["id"], "survived": False, "reason": "too_few_trades",
                "n": len(all_tr), "oos_mean_r": None, "pvalue": None}
    # TEMPORAL split per symbol: every coin contributes its EARLY bars to
    # in-sample and its LATE bars to OOS — no calendar overlap across coins
    # (the old symbol-major cut let correlated coins leak the same window).
    cutbar = {sym: int(len(rows) * (1 - oos_frac)) for sym, rows in frames.items()}
    oos = [t for t in all_tr if t["bar"] >= cutbar.get(t["symbol"], 1 << 60)]
    if len(oos) < 15:
        return {"id": method["id"], "survived": False, "reason": "too_few_oos",
                "n": len(all_tr), "oos_n": len(oos), "oos_mean_r": None, "pvalue": None}
    card = ls.scorecard(oos)
    m = card.get("metrics", {})
    p = card.get("pvalue")
    mean_r = m.get("mean_r")
    total_net = sum(t["net"] for t in oos)
    bar = 0.05 / max(1, n_methods)                 # Bonferroni-corrected significance
    survived = bool(mean_r is not None and mean_r > 0 and total_net > 0
                    and p is not None and p < bar)
    return {
        "id": method["id"], "name": method.get("name"), "desc": method.get("desc"),
        "side": method.get("side"), "survived": survived,
        "n": len(all_tr), "oos_n": len(oos), "oos_mean_r": round(mean_r, 4) if mean_r is not None else None,
        "oos_total_net_pct": round(total_net * 100, 3),
        "oos_win_rate": m.get("win_rate"), "pvalue": p, "p_bar": round(bar, 5),
        "reason": "survived" if survived else ("p>=bar" if (p is not None and p >= bar)
                                               else "not_positive"),
    }


def run_lab(methods: list[dict[str, Any]], frames: dict[str, list[dict[str, float]]]) -> dict[str, Any]:
    """Backtest + curate every candidate method; persist survivors + killed ledger.

    Multiple-testing control is BH-FDR (q=0.05), NOT Bonferroni-over-the-pool:
    the audit showed 0.05/n crosses the permutation p-floor once the growing pool
    passes ~250 methods, after which NOTHING could ever survive; and survivors
    flipped on pool growth alone. Hysteresis: a previous survivor keeps its seat
    while it stays individually significant (p<=0.05, positive OOS), so one round
    of pool churn can't evict a working method."""
    # Second brain (P2): never spend a backtest on a canonical duplicate. The pool
    # dedups by id STRING only, so two ids with identical side+conditions+sl/tp
    # both reach here (and would double-count as independent trials). Keep the
    # first occurrence (seed order wins).
    try:
        from method_canonical import method_hash as _mh
        _seen: set[str] = set()
        _uniq = []
        for _m in methods:
            _h = _mh(_m)
            if _h in _seen:
                continue
            _seen.add(_h)
            _uniq.append(_m)
        methods = _uniq
    except Exception:
        pass
    n = len(methods)
    results = [evaluate_method(mth, frames, n_methods=1) for mth in methods]   # raw p
    try:
        prev = {r.get("id") for r in json.loads(SURVIVORS.read_text(encoding="utf-8")) if r.get("survived")}
    except Exception:
        prev = set()
    # BH-FDR over methods with a valid, positive-OOS result
    cand = sorted([r for r in results if r.get("pvalue") is not None
                   and (r.get("oos_mean_r") or 0) > 0 and (r.get("oos_total_net_pct") or 0) > 0],
                  key=lambda r: r["pvalue"])
    m = max(1, len(results)); q = 0.05; thr = 0.0
    for i, r in enumerate(cand, 1):
        if r["pvalue"] <= q * i / m:
            thr = max(thr, r["pvalue"])
    for r in results:
        pos = (r.get("pvalue") is not None and (r.get("oos_mean_r") or 0) > 0
               and (r.get("oos_total_net_pct") or 0) > 0)
        bh = bool(pos and thr > 0 and r["pvalue"] <= thr)
        grace = bool(pos and r["id"] in prev and r["pvalue"] <= 0.05)
        r["survived"] = bh or grace
        r["reason"] = ("survived_bh" if bh else "survived_grace" if grace
                       else r.get("reason") if r.get("reason") in ("too_few_trades", "too_few_oos")
                       else ("p>fdr" if pos else "not_positive"))
    survivors = [r for r in results if r.get("survived")]
    killed = [r for r in results if not r.get("survived")]
    LAB_DIR.mkdir(parents=True, exist_ok=True)
    survivors.sort(key=lambda r: (r.get("oos_mean_r") or -9), reverse=True)
    SURVIVORS.write_text(json.dumps(survivors, indent=1), encoding="utf-8")
    with KILLED.open("w", encoding="utf-8") as fh:
        for r in killed:
            fh.write(json.dumps(r) + "\n")
    ledger = {"tested": n, "survived": len(survivors), "killed": len(killed),
              "coins": len(frames), "survivor_ids": [r["id"] for r in survivors]}
    LEDGER.write_text(json.dumps(ledger, indent=1), encoding="utf-8")
    return {"ledger": ledger, "results": results}
