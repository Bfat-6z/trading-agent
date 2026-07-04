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


def feature_frame(bars: list[dict[str, Any]]) -> list[dict[str, float]]:
    """One feature row per bar (the columns methods reference). Values known AT
    THAT BAR's close — no lookahead."""
    if len(bars) < 60:
        return []
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
        rows.append({
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
