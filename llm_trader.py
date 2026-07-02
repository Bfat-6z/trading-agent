"""LLM-driven discretionary PAPER trader with context-conditioned self-learning.

The owner's design (not the mechanical prove-or-kill harness): plug a strong LLM
in as the decision brain. Each cycle it reads FULL market context per symbol
(price action, regime, funding, CVD, time-of-day) PLUS its own past trade outcomes
tagged by context, and decides LONG/SHORT/SKIP. It learns from mistakes
CONTEXTUALLY — a loss on one coin/regime/time doesn't blanket-ban the setup; the
same idea can win on another coin at another time. Markets are non-stationary; the
LLM weighs context rather than a static verdict.

RULES (owner, fixed — enforced in code, not left to the LLM):
- position size 5-10% of equity per trade
- leverage EXACTLY x5 or x10
- higher frequency (short loop)

SAFETY: PAPER-ONLY. This module has its OWN paper account and NEVER calls
futures_create_order / any live path; live_guard + ALLOW_LIVE_ORDERS untouched.
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Any

import backtest_chart_signal as cs
import orderflow_data as of
import paper_cost_model as pcm
import universe_selector as us

ROOT = Path(__file__).resolve().parent
LT_DIR = ROOT / "state" / "llm_trader"
ACCOUNT = LT_DIR / "account.json"
POSITIONS = LT_DIR / "positions.jsonl"
CLOSED = LT_DIR / "closed.jsonl"
MEMORY = LT_DIR / "memory.jsonl"          # context-tagged trade outcomes (self-learning)
PID_FILE = LT_DIR / "llm_trader.pid"
STOP_FILE = LT_DIR / "llm_trader.stop"
HEARTBEAT = LT_DIR / "llm_trader_heartbeat.json"

MODEL = os.environ.get("LLM_TRADER_MODEL", "cx/gpt-5.5")
BASE_URL = os.environ.get("LLM_TRADER_BASE", "http://localhost:20128/v1")
TF = "15m"
START_EQUITY = 100.0
MAX_HOLD_BARS = 32
# OWNER RULES (hard):
SIZE_PCT_MIN, SIZE_PCT_MAX = 5.0, 10.0
ALLOWED_LEVERAGE = (5, 10)


# ---------------------------------------------------------------------------
# account / io
# ---------------------------------------------------------------------------
def _load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try: out.append(json.loads(line))
            except Exception: pass
    return out


def _rewrite(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, default=str) + "\n" for r in rows), encoding="utf-8")


def _append(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def load_account() -> dict[str, Any]:
    if ACCOUNT.exists():
        try: return json.loads(ACCOUNT.read_text())
        except Exception: pass
    return {"equity": START_EQUITY, "realized": 0.0, "trades": 0, "wins": 0}


def save_account(a: dict[str, Any]) -> None:
    ACCOUNT.parent.mkdir(parents=True, exist_ok=True)
    ACCOUNT.write_text(json.dumps(a, indent=1, default=str), encoding="utf-8")


# ---------------------------------------------------------------------------
# context: real market features per symbol
# ---------------------------------------------------------------------------
def _regime(df) -> dict[str, Any]:
    i = len(df) - 1
    c = df["close"]
    ret20 = float(c.iloc[i] / c.iloc[i - 20] - 1) if i >= 20 else 0.0
    ema_f, ema_s = float(df["ema_fast"].iloc[i]), float(df["ema_slow"].iloc[i])
    adx = float(df["adx"].iloc[i]) if df["adx"].iloc[i] == df["adx"].iloc[i] else 0.0
    # Kaufman efficiency ratio (path efficiency)
    if i >= 20:
        net = abs(float(c.iloc[i] - c.iloc[i - 20]))
        path = float((c.diff().abs().iloc[i - 19:i + 1]).sum()) or 1.0
        er = round(net / path, 3)
    else:
        er = 0.0
    trend = "up" if ema_f > ema_s else "down"
    chop = "trending" if (adx >= 25 and er >= 0.35) else "choppy" if (adx < 20 or er < 0.25) else "mixed"
    return {"ret20_pct": round(ret20 * 100, 2), "trend": trend, "adx": round(adx, 1),
            "efficiency": er, "regime": chop, "atr_pct": round(float(df["atr"].iloc[i]) / float(c.iloc[i]) * 100, 2)}


def build_context(client: Any, symbols: list[str], now_ms: int) -> list[dict[str, Any]]:
    import backtest_data_fetcher as bf
    out = []
    for sym in symbols:
        try:
            fb = of.fetch_klines_with_flow(sym, TF, months=0.12, end_ms=now_ms, client=client, sleep_between=0.02)
            if len(fb) < 40:
                continue
            fund = of.fetch_funding_series(sym, months=0.12, end_ms=now_ms, client=client)
            ind = cs.compute_indicators(fb)
            enr = of.enrich_indicator_df(ind, fb, fund)
            i = len(enr) - 1
            closes = [round(float(x), 4) for x in enr["close"].iloc[-8:].tolist()]
            reg = _regime(enr)
            out.append({
                "symbol": sym, "price": round(float(enr["close"].iloc[i]), 4),
                "last8_closes": closes, **reg,
                "funding_rate": round(float(enr["funding_rate"].iloc[i]) if "funding_rate" in enr else 0.0, 6),
                "cvd_norm": round(float(enr["cvd_delta_norm"].iloc[i]) if "cvd_delta_norm" in enr and enr["cvd_delta_norm"].iloc[i]==enr["cvd_delta_norm"].iloc[i] else 0.0, 3),
                "atr": round(float(enr["atr"].iloc[i]), 4),
                "_ts": int(enr["ts_ms"].iloc[i]),
            })
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# context-tagged self-learning memory
# ---------------------------------------------------------------------------
def relevant_lessons(symbol: str, regime: str, k: int = 8) -> list[dict[str, Any]]:
    """Past outcomes for the SAME coin or SAME regime — the context-conditioned
    memory the LLM learns from (not a blanket ban)."""
    mem = _load(MEMORY)
    same = [m for m in mem if m.get("symbol") == symbol or m.get("regime") == regime]
    return (same or mem)[-k:]


# ---------------------------------------------------------------------------
# LLM decision (9router, OpenAI-compatible)
# ---------------------------------------------------------------------------
def _llm(system: str, user: str) -> str | None:
    """Call the configured LLM via the repo's 9router-authenticated client
    (handles NINEROUTER_API_KEY/base from .env). Returns text or None."""
    try:
        from llm_reasoning_agent import call_large_model
        return call_large_model(system, user, model=MODEL, max_tokens=700)
    except Exception:
        return None


def _extract_json(text: str) -> Any:
    if not text:
        return None
    a, b = text.find("["), text.rfind("]")
    if a >= 0 and b > a:
        try: return json.loads(text[a:b + 1])
        except Exception: pass
    a, b = text.find("{"), text.rfind("}")
    if a >= 0 and b > a:
        try: return json.loads(text[a:b + 1])
        except Exception: pass
    return None


def decide(context: list[dict[str, Any]], equity: float) -> list[dict[str, Any]]:
    """ONE batched LLM call for ALL symbols (sees them together -> relative
    selection; 1 call/cycle instead of N). Each symbol carries its own
    context-tagged lessons. Rules enforced in code. Returns validated decisions."""
    if not context:
        return []
    by_sym = {c["symbol"]: c for c in context}
    payload = []
    for ctx in context:
        lessons = relevant_lessons(ctx["symbol"], ctx["regime"])
        payload.append({"symbol": ctx["symbol"], **{k: v for k, v in ctx.items() if not k.startswith("_") and k != "symbol"},
                        "your_past_outcomes": [{"symbol": m.get("symbol"), "regime": m.get("regime"),
                                                "side": m.get("side"), "R": m.get("r"), "reason": m.get("reason")}
                                               for m in lessons]})
    sys = ("You are a discretionary crypto FUTURES scalper on PAPER money. You see several liquid coins with "
           "live context + YOUR OWN past trade outcomes (tagged by coin/regime). Learn from mistakes "
           "CONTEXTUALLY: a past loss does NOT blanket-ban a setup — the same idea can win on another coin or "
           "regime or time (markets are non-stationary). Pick only the BEST opportunities; SKIP the rest (SKIP is "
           "common and fine — no forced trades). For each coin you want to trade, return an object. "
           "Reply ONLY with a JSON ARRAY (may be empty): "
           "[{\"symbol\":\"BTCUSDT\",\"action\":\"LONG|SHORT\",\"leverage\":5|10,\"size_pct\":5-10,"
           "\"sl_pct\":0.5-5,\"tp_pct\":0.5-10,\"rationale\":\"why, citing context\"}]")
    usr = json.dumps({"equity": round(equity, 2), "coins": payload}, default=str)
    raw = _llm(sys, usr)
    arr = _extract_json(raw) if raw else None
    if isinstance(arr, dict):
        arr = [arr]
    if not isinstance(arr, list):
        return []
    decisions = []
    for dec in arr:
        if not isinstance(dec, dict):
            continue
        sym = str(dec.get("symbol", "")); ctx = by_sym.get(sym)
        action = str(dec.get("action", "SKIP")).upper()
        if not ctx or action not in ("LONG", "SHORT"):
            continue
        lev = 10 if int(dec.get("leverage", 5) or 5) >= 10 else 5   # ENFORCE x5/x10 only
        size_pct = max(SIZE_PCT_MIN, min(SIZE_PCT_MAX, float(dec.get("size_pct", 5) or 5)))  # ENFORCE 5-10%
        sl_pct = max(0.3, min(8.0, float(dec.get("sl_pct", 2) or 2)))
        tp_pct = max(0.3, min(15.0, float(dec.get("tp_pct", 3) or 3)))
        decisions.append({**ctx, "action": action, "leverage": lev, "size_pct": size_pct,
                          "sl_pct": sl_pct, "tp_pct": tp_pct, "rationale": str(dec.get("rationale", ""))[:240]})
    return decisions


# ---------------------------------------------------------------------------
# paper execution + resolution (never live)
# ---------------------------------------------------------------------------
def open_positions(decisions: list[dict[str, Any]], equity: float, now_iso: str) -> int:
    open_pos = _load(POSITIONS)
    open_syms = {p["symbol"] for p in open_pos}
    n = 0
    for d in decisions:
        if d["symbol"] in open_syms:
            continue
        entry = float(d["price"]); side = d["action"]; lev = d["leverage"]
        margin = equity * d["size_pct"] / 100.0
        notional = margin * lev
        qty = notional / entry if entry > 0 else 0.0
        sl = entry * (1 - d["sl_pct"]/100) if side == "LONG" else entry * (1 + d["sl_pct"]/100)
        tp = entry * (1 + d["tp_pct"]/100) if side == "LONG" else entry * (1 - d["tp_pct"]/100)
        open_pos.append({"symbol": d["symbol"], "side": side, "entry": entry, "qty": qty,
                         "margin": round(margin, 4), "leverage": lev, "sl": sl, "tp": tp,
                         "entry_ts": d["_ts"], "opened_at": now_iso, "regime": d["regime"],
                         "hour_utc": (int(d["_ts"]) // 3600000) % 24, "rationale": d["rationale"]})
        open_syms.add(d["symbol"]); n += 1
    _rewrite(POSITIONS, open_pos)
    return n


def resolve(client: Any, now_ms: int) -> int:
    import backtest_data_fetcher as bf
    open_pos = _load(POSITIONS)
    if not open_pos:
        return 0
    acct = load_account()
    still, closed_n = [], 0
    for p in open_pos:
        try:
            fb = of.fetch_klines_with_flow(p["symbol"], TF, months=0.06, end_ms=now_ms, client=client, sleep_between=0.02)
            fut = [b for b in fb if int(b["ts_ms"]) > int(p["entry_ts"])]
        except Exception:
            still.append(p); continue
        side, sl, tp = p["side"], float(p["sl"]), float(p["tp"])
        exit_px = reason = None
        for k, b in enumerate(fut):
            hi, lo = float(b["high"]), float(b["low"])
            if (side == "LONG" and lo <= sl) or (side == "SHORT" and hi >= sl):
                exit_px, reason = sl, "sl"; break
            if (side == "LONG" and hi >= tp) or (side == "SHORT" and lo <= tp):
                exit_px, reason = tp, "tp"; break
            if k + 1 >= MAX_HOLD_BARS:
                exit_px, reason = float(b["close"]), "timeout"; break
        if exit_px is None:
            still.append(p); continue
        entry, qty, lev = float(p["entry"]), float(p["qty"]), int(p["leverage"])
        gross = (exit_px - entry) * qty if side == "LONG" else (entry - exit_px) * qty
        tier = pcm.liquidity_tier(3e9)
        fee = (entry + exit_px) * qty * float(pcm.TAKER_FEE_RATE)
        net = gross - fee
        r = net / float(p["margin"]) if float(p["margin"]) > 0 else 0.0  # R vs margin risked
        acct["equity"] = round(float(acct["equity"]) + net, 4)
        acct["realized"] = round(float(acct["realized"]) + net, 4)
        acct["trades"] = int(acct["trades"]) + 1
        acct["wins"] = int(acct["wins"]) + (1 if net > 0 else 0)
        rec = {"symbol": p["symbol"], "side": side, "regime": p.get("regime"), "hour_utc": p.get("hour_utc"),
               "entry": entry, "exit": exit_px, "reason": reason, "net": round(net, 4), "r": round(r, 3),
               "leverage": lev, "rationale": p.get("rationale"), "closed_ts": now_ms}
        _append(CLOSED, rec)
        _append(MEMORY, rec)   # self-learning: outcome tagged by context
        closed_n += 1
    save_account(acct)
    _rewrite(POSITIONS, still)
    return closed_n


# ---------------------------------------------------------------------------
# loop
# ---------------------------------------------------------------------------
def run_once() -> dict[str, Any]:
    import time as _t
    from timebase import utc_now
    from tradingagents.binance.client import spot_client
    client = spot_client()
    now_ms = int(_t.time() * 1000)
    resolved = resolve(client, now_ms)
    acct = load_account()
    uni = us.select_universe(client, end_ms=now_ms, months=1.0, timeframe="1h",
                             min_daily_quote_volume=50_000_000.0, max_symbols=6)
    ctx = build_context(client, uni["selected"], now_ms)
    decisions = decide(ctx, float(acct["equity"]))
    opened = open_positions(decisions, float(acct["equity"]), utc_now())
    wr = round(acct["wins"] / acct["trades"], 3) if acct["trades"] else None
    return {"equity": acct["equity"], "trades": acct["trades"], "win_rate": wr,
            "opened": opened, "resolved": resolved, "open": len(_load(POSITIONS)),
            "considered": len(ctx), "acted": len(decisions), "model": MODEL, "live": "LOCKED"}


def _hb(last: dict[str, Any], status="running"):
    from atomic_state import write_json_atomic
    from timebase import utc_now
    write_json_atomic(HEARTBEAT, {"agent": "llm_trader", "pid": os.getpid(), "ts": utc_now(),
                                  "updated_at": utc_now(), "status": status, "last_run": last})


if __name__ == "__main__":
    import argparse, time
    ap = argparse.ArgumentParser(description="LLM discretionary PAPER trader (paper-only)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval-seconds", type=float, default=300.0)  # higher frequency
    a = ap.parse_args()
    LT_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    if a.once:
        print(json.dumps(run_once(), default=str))
    else:
        while not STOP_FILE.exists():
            try: res = run_once()
            except Exception as exc: res = {"error": str(exc)[:200]}
            _hb(res)
            t = time.time() + a.interval_seconds
            while time.time() < t and not STOP_FILE.exists():
                time.sleep(1)
        _hb({}, status="stopped")
