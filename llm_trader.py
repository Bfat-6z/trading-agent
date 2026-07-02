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
import chart_smc as smc
import llm_trader_charts as ltc
import llm_trader_memory as ltm
import llm_trader_risk as lr
import llm_trader_scorecard as ls
import orderflow_data as of
import paper_cost_model as pcm
import universe_selector as us

ROOT = Path(__file__).resolve().parent
LT_DIR = ROOT / "state" / "llm_trader"
ACCOUNT = LT_DIR / "account.json"
POSITIONS = LT_DIR / "positions.jsonl"
CLOSED = LT_DIR / "closed.jsonl"
MEMORY = LT_DIR / "memory.jsonl"          # context-tagged trade outcomes (self-learning)
SCORECARD = LT_DIR / "scorecard.json"     # measured-edge scorecard (plan #5-#9)
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
# SCOPE + FREQUENCY (owner: wider coin scan, higher frequency). Universe is
# re-selected each cycle by quote-volume; more concurrent slots so a wider scan
# actually turns into more live trades (the batched decision is still ONE LLM
# call regardless of coin count, so breadth is ~free on the model side).
UNIVERSE_MAX = int(os.environ.get("LLM_TRADER_UNIVERSE_MAX", "30"))
UNIVERSE_MIN_QVOL = float(os.environ.get("LLM_TRADER_MIN_QVOL", "20000000"))
# Owner: UNLIMITED number of positions — accepts correlation risk for bigger
# upside. No trade-count cap (50 >> the 30-coin universe, 1-per-symbol, so it
# never binds). MARGIN is the only physical limit: at 5-10%/trade on $100 you can
# hold ~10-19 positions before capital is fully deployed (95% cap leaves a hair).
# The daily -15% breaker stays as the sole backstop (stops NEW trades after a bad
# day; does not close positions).
MAX_CONCURRENT = int(os.environ.get("LLM_TRADER_MAX_CONCURRENT", "50"))
MAX_TOTAL_MARGIN_PCT = float(os.environ.get("LLM_TRADER_MAX_MARGIN_PCT", "95"))


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


# charts saved next to the UI so the static server serves them (/charts/<f>.png),
# lets the dashboard show the EXACT entry chart the LLM saw for each trade.
CHARTS_DIR = ROOT.parent / "horizon-ui" / "charts"


def _save_entry_chart(b64: str | None, symbol: str, ts: int) -> str | None:
    """Persist the base64 entry chart to horizon-ui/charts/ and return a
    server-relative path. Keeps only the most recent ~250 charts."""
    if not b64:
        return None
    try:
        import base64 as _b64
        CHARTS_DIR.mkdir(parents=True, exist_ok=True)
        fn = f"{symbol}_{int(ts)}.png"
        (CHARTS_DIR / fn).write_bytes(_b64.b64decode(b64))
        files = sorted(CHARTS_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime)
        for old in files[:-250]:
            try: old.unlink()
            except Exception: pass
        return "charts/" + fn
    except Exception:
        return None


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


def _htf_trend(closes, step: int) -> str:
    """Higher-timeframe trend from the 15m close array, resampled every `step`
    bars (4=1h, 16=4h). up/down/flat vs a short EMA of the resampled series."""
    r = closes[::step]
    if len(r) < 6:
        return "n/a"
    import numpy as _np
    a = 2.0 / (min(20, len(r)) + 1.0)
    e = r[0]
    for x in r[1:]:
        e = a * x + (1 - a) * e
    last = r[-1]
    return "up" if last > e * 1.001 else "down" if last < e * 0.999 else "flat"


def _features(enr, fb) -> dict[str, Any]:
    """Rich numeric context (owner request A): EMA20/50/200 stack, volume surge,
    multi-window returns, multi-timeframe trend — the numbers a chart trader reads
    alongside the picture."""
    import numpy as _np
    c = enr["close"].to_numpy(dtype=float)
    i = len(c) - 1
    def ema(p):
        a = 2.0 / (p + 1.0); e = c[0]
        for x in c[1:]:
            e = a * x + (1 - a) * e
        return e
    e20, e50, e200 = ema(20), ema(50), ema(200)
    px = c[i]
    if px > e20 > e50 > e200:
        stack = "bull_stack"
    elif px < e20 < e50 < e200:
        stack = "bear_stack"
    else:
        stack = "mixed"
    def ret(n):
        return round(float(c[i] / c[i - n] - 1) * 100, 2) if i >= n else 0.0
    vr = float(enr["vol_ratio"].iloc[i]) if "vol_ratio" in enr and enr["vol_ratio"].iloc[i] == enr["vol_ratio"].iloc[i] else 1.0
    rsi = float(ltc._rsi(c)[-1])
    rsi_state = "oversold" if rsi < 30 else "overbought" if rsi > 70 else "neutral"
    return {
        "ema20": round(float(e20), 4), "ema50": round(float(e50), 4), "ema200": round(float(e200), 4),
        "ema_stack": stack, "px_vs_ema20_pct": round(float(px / e20 - 1) * 100, 2) if e20 else 0.0,
        "vol_ratio": round(vr, 2), "ret5_pct": ret(5), "ret50_pct": ret(50),
        "rsi14": round(rsi, 1), "rsi_state": rsi_state,
        "htf_1h_trend": _htf_trend(c, 4), "htf_4h_trend": _htf_trend(c, 16),
    }


def build_context(client: Any, symbols: list[str], now_ms: int) -> list[dict[str, Any]]:
    import backtest_data_fetcher as bf
    out = []
    for sym in symbols:
        try:
            fb = of.fetch_klines_with_flow(sym, TF, months=0.12, end_ms=now_ms, client=client, sleep_between=0.02)
            # CLOSED bars only (plan #13, VTL time-gating): drop the still-forming
            # candle so every decision input is immutable — its high/low/close and
            # derived indicators would otherwise repaint within the bar.
            bar_ms = of._TF_MS[TF]
            fb = [b for b in fb if int(b["ts_ms"]) + bar_ms <= now_ms]
            if len(fb) < 40:
                continue
            fund = of.fetch_funding_series(sym, months=0.12, end_ms=now_ms, client=client)
            ind = cs.compute_indicators(fb)
            enr = of.enrich_indicator_df(ind, fb, fund)
            i = len(enr) - 1
            closes = [round(float(x), 4) for x in enr["close"].iloc[-8:].tolist()]
            reg = _regime(enr)
            feats = _features(enr, fb)
            out.append({
                "symbol": sym, "price": round(float(enr["close"].iloc[i]), 4),
                "last8_closes": closes, **reg, **feats,
                "funding_rate": round(float(enr["funding_rate"].iloc[i]) if "funding_rate" in enr else 0.0, 6),
                "cvd_norm": round(float(enr["cvd_delta_norm"].iloc[i]) if "cvd_delta_norm" in enr and enr["cvd_delta_norm"].iloc[i]==enr["cvd_delta_norm"].iloc[i] else 0.0, 3),
                "atr": round(float(enr["atr"].iloc[i]), 4),
                # 24h quote volume (96 x 15m bars) drives the fee/slippage
                # liquidity tier; missing data -> 0.0 -> "micro" (pessimistic).
                "_quote_vol_24h": round(sum(float(b.get("quote_volume", 0.0)) for b in fb[-96:]), 0),
                # last ~220 closed bars for chart rendering (underscore -> never
                # sent as text; used only to draw the candlestick+EMA+vol image).
                "_bars": fb[-220:],
                "_ts": int(enr["ts_ms"].iloc[i]),
            })
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# context-tagged self-learning memory
# ---------------------------------------------------------------------------
def relevant_lessons(symbol: str, regime: str, k: int = 8) -> list[dict[str, Any]]:
    """LEGACY (kept for API compatibility / manual inspection only): last-k raw
    outcomes for the same coin or regime. decide() no longer injects this —
    the distilled llm_trader_memory context replaced it (plan 260702 #10/#11)
    because 8 raw rows discard most history and let one outlier dominate."""
    mem = _load(MEMORY)
    same = [m for m in mem if m.get("symbol") == symbol or m.get("regime") == regime]
    return (same or mem)[-k:]


def memory_context() -> dict[str, Any]:
    """Distilled learning context injected into decide()'s prompt each cycle.

    Aggregates ALL closed trades (not the last-8 raw rows) into grouped stats,
    data-phrased lessons and rationale-vs-outcome recents via the pure
    llm_trader_memory module — plan 260702 checklist #10/#11. Reads CLOSED
    (canonical append-only log written by resolve); llm_trader_memory
    guarantees malformed rows are skipped, so this can't kill the loop."""
    return ltm.build_memory_context(_load(CLOSED))


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


def _env_llm() -> tuple[str, str]:
    """(base_url, api_key) for the vision call — os.environ first, then .env."""
    base = os.environ.get("NINEROUTER_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or ""
    key = os.environ.get("NINEROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    if not key:
        try:
            for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if k == "NINEROUTER_BASE_URL" and not base:
                        base = v
                    elif k == "NINEROUTER_API_KEY" and not key:
                        key = v
        except Exception:
            pass
    return (base.rstrip("/") or BASE_URL.rstrip("/")), key


def _llm_vision(system: str, text: str, images: list[tuple[str, str]]) -> str | None:
    """Vision call: send text + rendered chart PNGs to gpt-5.5 (verified to accept
    images on 9router). images = [(label, base64_png)]. Returns text or None."""
    base, key = _env_llm()
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for label, b64 in images:
        content.append({"type": "text", "text": f"Chart — {label}:"})
        content.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}})
    body = json.dumps({"model": MODEL, "max_tokens": 1200, "temperature": 0.3,
                       "messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": content}]}).encode()
    req = urllib.request.Request(base + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + key}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read().decode())
        return d["choices"][0]["message"]["content"]
    except Exception:
        return None


def _validate_decisions(arr: Any, by_sym: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Parse an LLM decision array and ENFORCE owner rules in code (x5/x10 only,
    5-10% size, sane SL/TP). LLM only proposes within these bounds."""
    if isinstance(arr, dict):
        arr = [arr]
    if not isinstance(arr, list):
        return []
    out = []
    for dec in arr:
        if not isinstance(dec, dict):
            continue
        sym = str(dec.get("symbol", "")); ctx = by_sym.get(sym)
        action = str(dec.get("action", "SKIP")).upper()
        if not ctx or action not in ("LONG", "SHORT"):
            continue
        lev = 10 if int(dec.get("leverage", 5) or 5) >= 10 else 5          # x5/x10 only
        size_pct = max(SIZE_PCT_MIN, min(SIZE_PCT_MAX, float(dec.get("size_pct", 5) or 5)))  # 5-10%
        sl_pct = max(0.3, min(8.0, float(dec.get("sl_pct", 2) or 2)))
        tp_pct = max(0.3, min(15.0, float(dec.get("tp_pct", 3) or 3)))
        out.append({**ctx, "action": action, "leverage": lev, "size_pct": size_pct,
                    "sl_pct": sl_pct, "tp_pct": tp_pct, "rationale": str(dec.get("rationale", ""))[:240]})
    return out


_MEMORY_RULE = ("Learn from your MEMORY block CONTEXTUALLY: the counts are evidence to weigh, not bans — a past "
                "loss does NOT blanket-ban a setup; the same idea can win on another coin/regime/time (markets are "
                "non-stationary). Pick only the BEST setups; SKIP is common and fine — no forced trades. "
                "Owner rules: leverage EXACTLY 5 or 10; size 5-10% of equity; respect your capacity limits.")
_DECISION_SCHEMA = ("Reply ONLY with a JSON ARRAY (may be empty): [{\"symbol\":\"BTCUSDT\",\"action\":\"LONG|SHORT|SKIP\","
                    "\"leverage\":5|10,\"size_pct\":5-10,\"sl_pct\":0.5-5,\"tp_pct\":0.5-10,\"rationale\":\"cite the chart/levels\"}]")


def _decide_numeric(context: list[dict[str, Any]], equity: float,
                    status: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Numeric-only decision (no charts) — used as the vision fallback and when
    no charts render. ONE batched text call over the given coins."""
    if not context:
        return []
    by_sym = {c["symbol"]: c for c in context}
    payload = [{"symbol": c["symbol"], **{k: v for k, v in c.items() if not k.startswith("_") and k != "symbol"}}
               for c in context]
    sys = ("You are a discretionary crypto FUTURES scalper on PAPER money reading numeric context per coin. "
           + _MEMORY_RULE + " " + _DECISION_SCHEMA)
    usr = json.dumps({"equity": round(equity, 2), "your_status": status or {},
                      "memory": memory_context(), "coins": payload}, default=str)
    return _validate_decisions(_extract_json(_llm(sys, usr)), by_sym)


def _activity_score(c: dict[str, Any]) -> float:
    """Cheap 'is this coin interesting right now' rank — |recent move| +
    volume surge + order-flow imbalance. Picks which coins get a chart."""
    return (abs(float(c.get("ret20_pct", 0) or 0))
            + 4.0 * abs(float(c.get("vol_ratio", 1) or 1) - 1.0)
            + 3.0 * abs(float(c.get("cvd_norm", 0) or 0)))


def decide(context: list[dict[str, Any]], equity: float,
           status: dict[str, Any] | None = None, *, max_charts: int = 5) -> list[dict[str, Any]]:
    """CHART-BASED decision (owner request A+B): scan all coins numerically, pick
    the most active ones, RENDER their candlestick+EMA+volume charts and let
    gpt-5.5 SEE them (vision) before deciding — the way a discretionary trader
    scans a watchlist then opens charts. Falls back to a numeric-only decision on
    the same shortlist if the vision call fails, so it never stalls."""
    if not context:
        return []
    # shortlist the most active coins to chart (wider scope -> better candidates)
    ranked = sorted(context, key=_activity_score, reverse=True)
    shortlist = ranked[:max_charts]
    by_sym = {c["symbol"]: c for c in shortlist}
    images, charted = [], []
    for c in shortlist:
        bars = c.get("_bars")
        if not bars:
            continue
        # SMC read (owner's revived detectors): trend/bias + nearest S/R zones.
        sm = smc.smc_summary(bars, c["symbol"], TF)
        c["_smc"] = sm.get("summary") or {}
        b64 = ltc.render_chart(c["symbol"], bars, tf=TF, hlines=(sm.get("hlines") or None))
        if b64:
            c["_chart_b64"] = b64   # carry to open_positions so the EXACT chart the
            images.append((c["symbol"], b64)); charted.append(c)  # LLM saw is persisted
    if not images:
        return _decide_numeric(shortlist, equity, status)   # nothing rendered -> numeric
    # compact numeric + SMC read to accompany each chart (no raw bars in text)
    coins_txt = [{"symbol": c["symbol"], "smc": c.get("_smc", {}),
                  **{k: v for k, v in c.items() if not k.startswith("_") and k != "symbol"}}
                 for c in charted]
    market_overview = [{"symbol": c["symbol"], "ret20_pct": c.get("ret20_pct"),
                        "regime": c.get("regime")} for c in ranked[:20]]
    sys = ("You are a discretionary crypto FUTURES scalper on PAPER money. You are shown CANDLESTICK charts "
           "(with EMA20/50/200, a volume panel, and an RSI(14) panel) for the most active coins, plus their "
           "numeric context and a broad market overview. READ THE CHARTS: trend & EMA stack/slope, structure "
           "(breakouts, ranges, support/resistance), momentum vs exhaustion, volume confirmation, and RSI "
           "(oversold <30 leans LONG mean-reversion, overbought >70 leans SHORT) — but RSI is a HINT, not a "
           "rule: in a strong trend oversold can stay oversold, so only fade RSI WITH structure/level support. "
           "Each coin also has an 'smc' block from a no-lookahead market-structure engine: trend/bias/confidence, "
           "the nearest SUPPORT and RESISTANCE zone (price range + strength + quality + touch count) drawn on the "
           "chart as SUP/RES lines, and an invalidation level. Prefer LONGs off a strong support zone with bullish "
           "structure, SHORTs off resistance with bearish structure; avoid buying into overhead resistance or "
           "shorting into support. " + _MEMORY_RULE + " " + _DECISION_SCHEMA)
    text = json.dumps({"equity": round(equity, 2), "your_status": status or {},
                       "memory": memory_context(), "charted_coins": coins_txt,
                       "market_overview": market_overview}, default=str)
    out = _validate_decisions(_extract_json(_llm_vision(sys, text, images)), by_sym)
    if out:
        return out
    return _decide_numeric(charted, equity, status)   # vision failed/empty -> numeric fallback


# ---------------------------------------------------------------------------
# paper execution + resolution (never live)
# ---------------------------------------------------------------------------
def _day_anchor(acct: dict[str, Any], now_ms: int, equity: float) -> float:
    """Start-of-UTC-day equity for the daily-loss breaker. Persisted in the
    account file and rolled when the UTC day changes, so the breaker always
    compares today's realized losses against where the day actually started
    (plan #4 — hopit kill-switch lesson)."""
    day = int(now_ms // 86_400_000)
    if int(acct.get("day_key") or -1) != day:
        acct["day_key"] = day
        acct["day_start_equity"] = float(equity)
        save_account(acct)
    return float(acct.get("day_start_equity") or equity)


def open_positions(decisions: list[dict[str, Any]], equity: float, now_iso: str,
                   now_ms: int | None = None) -> int:
    """Open paper positions for validated decisions, behind fail-closed
    pre-trade governance (plan #4, HKUDS enforcement + hopit kill-switch):
    the daily-loss breaker gates the whole cycle; total-margin and
    max-concurrent caps gate each individual open."""
    import time as _t
    open_pos = _load(POSITIONS)
    open_syms = {p["symbol"] for p in open_pos}
    now_ms = int(now_ms if now_ms is not None else _t.time() * 1000)
    acct = load_account()
    day_start = _day_anchor(acct, now_ms, equity)
    blocked, why = lr.daily_breaker(_load(CLOSED), day_start, now_ms)
    if blocked:
        _append(LT_DIR / "governance.jsonl",
                {"ts_ms": now_ms, "event": "daily_breaker_block", "why": why,
                 "skipped": [d.get("symbol") for d in decisions]})
        _rewrite(POSITIONS, open_pos)
        return 0
    n = 0
    for d in decisions:
        if d["symbol"] in open_syms:
            continue
        side = d["action"]; lev = d["leverage"]
        # Entry is a MARKET fill: apply adverse slippage by liquidity tier
        # (plan item #3 — the zero-slip entry was structurally optimistic).
        quote_vol = float(d.get("_quote_vol_24h", 0.0) or 0.0)
        tier = pcm.liquidity_tier(quote_vol)
        slip = float(pcm.fill_bps(tier)) / 10000.0
        raw_px = float(d["price"])
        entry = raw_px * (1 + slip) if side == "LONG" else raw_px * (1 - slip)
        margin = equity * d["size_pct"] / 100.0
        # Per-open caps (fail-closed): total margin + concurrent-position limit.
        ok, cap_why = lr.can_open(margin, equity, open_pos,
                                  max_total_margin_pct=MAX_TOTAL_MARGIN_PCT,
                                  max_concurrent=MAX_CONCURRENT)
        if not ok:
            _append(LT_DIR / "governance.jsonl",
                    {"ts_ms": now_ms, "event": "can_open_block", "why": cap_why,
                     "symbol": d["symbol"]})
            continue
        notional = margin * lev
        qty = notional / entry if entry > 0 else 0.0
        sl = entry * (1 - d["sl_pct"]/100) if side == "LONG" else entry * (1 + d["sl_pct"]/100)
        tp = entry * (1 + d["tp_pct"]/100) if side == "LONG" else entry * (1 - d["tp_pct"]/100)
        # Forced-liquidation price (plan item #1): stored at open so resolve()
        # can rank liq ahead of SL pessimistically on every bar.
        mmr = lr.mmr_for(d["symbol"])
        liq_px = lr.liquidation_price(entry, lev, side, mmr)
        chart_rel = _save_entry_chart(d.get("_chart_b64"), d["symbol"], d["_ts"])
        open_pos.append({"symbol": d["symbol"], "side": side, "entry": entry, "qty": qty,
                         "margin": round(margin, 4), "leverage": lev, "sl": sl, "tp": tp,
                         "liq_px": liq_px, "mmr": mmr, "quote_vol_24h": quote_vol, "tier": tier,
                         "entry_ts": d["_ts"], "opened_at": now_iso, "regime": d["regime"],
                         "chart": chart_rel,
                         "hour_utc": (int(d["_ts"]) // 3600000) % 24, "rationale": d["rationale"]})
        open_syms.add(d["symbol"]); n += 1
    _rewrite(POSITIONS, open_pos)
    return n


def resolve(client: Any, now_ms: int) -> int:
    """Resolve open paper positions against CLOSED bars — honest exit model.

    Plan 260702-0900 items #1-#3 wired via llm_trader_risk (pure math):
    - forced liquidation checked FIRST on every bar (pessimistic liq->sl->tp);
      before this, reason could only be sl/tp/timeout and the scorecard's
      liq_count was structurally zero — false safety evidence.
    - funding charged per 8h event over the hold window as a REAL P&L leg
      (it was previously only an LLM feature, never money).
    - stop-market and timeout fills slip adversely by liquidity tier; a stop
      gaps through its price (3x multiplier), it never fills exactly.
    """
    open_pos = _load(POSITIONS)
    if not open_pos:
        return 0
    acct = load_account()
    still, closed_n = [], 0
    for p in open_pos:
        try:
            fb = of.fetch_klines_with_flow(p["symbol"], TF, months=0.06, end_ms=now_ms, client=client, sleep_between=0.02)
            # CLOSED bars only (plan #13): exits are judged on immutable candles;
            # the forming bar is re-examined next cycle once it closes.
            bar_ms = of._TF_MS[TF]
            fut = [b for b in fb if int(b["ts_ms"]) > int(p["entry_ts"])
                   and int(b["ts_ms"]) + bar_ms <= now_ms]
        except Exception:
            still.append(p); continue
        side, sl, tp = p["side"], float(p["sl"]), float(p["tp"])
        entry, qty, lev = float(p["entry"]), float(p["qty"]), int(p["leverage"])
        margin = float(p["margin"])
        # Positions opened before liq_px was stored: recompute (pessimistic mmr).
        liq_px = float(p.get("liq_px") or lr.liquidation_price(entry, lev, side, lr.mmr_for(p["symbol"])))
        # Liquidity tier for exit costs; fallback sums 24h (96 x 15m) quote
        # volume from the fetched bars. Unknown -> 0.0 -> "micro" (pessimistic).
        quote_vol = float(p.get("quote_vol_24h") or 0.0)
        if quote_vol <= 0.0:
            quote_vol = sum(float(b.get("quote_volume", 0.0)) for b in fb[-96:])
        tier = pcm.liquidity_tier(quote_vol)
        exit_px = reason = None
        exit_ts = int(p["entry_ts"])
        for k, b in enumerate(fut):
            hit = lr.exit_check(b, side, liq_px, sl, tp)  # pessimistic: liq -> sl -> tp
            if hit is not None:
                exit_px, reason = hit
                exit_ts = int(b["ts_ms"]); break
            if k + 1 >= MAX_HOLD_BARS:
                exit_px, reason = float(b["close"]), "timeout"
                exit_ts = int(b["ts_ms"]); break
        if exit_px is None:
            still.append(p); continue
        # Exit slippage: stop-market gaps through the stop, timeout is a plain
        # market order, TP is a resting limit (fills at its price), liquidation
        # net is pinned to -margin by net_pnl so its fill is informational.
        if reason == "sl":
            slip = float(pcm.fill_bps(tier, is_stop=True)) / 10000.0
            exit_px = exit_px * (1 - slip) if side == "LONG" else exit_px * (1 + slip)
        elif reason == "timeout":
            slip = float(pcm.fill_bps(tier)) / 10000.0
            exit_px = exit_px * (1 - slip) if side == "LONG" else exit_px * (1 + slip)
        # Funding as P&L: charge every 8h event inside (entry_ts, exit_ts].
        # Fetch failure -> 0.0 (cannot fabricate rates) but recorded, so a
        # systematic gap stays visible in closed.jsonl.
        try:
            fund = of.fetch_funding_series(p["symbol"], months=0.06, end_ms=now_ms, client=client)
            events = [(int(f["fundingTime"]), float(f["fundingRate"])) for f in fund]
        except Exception:
            events = []
        funding = lr.funding_cost(side, qty, entry, events, int(p["entry_ts"]), exit_ts)
        fee = float(lr.trade_costs(entry, exit_px, qty, quote_vol)["fee"])
        net = lr.net_pnl(side, entry, exit_px, qty, margin, fee, funding,
                         liquidated=(reason == "liquidation"))
        r = net / margin if margin > 0 else 0.0  # R vs margin risked
        acct["equity"] = round(float(acct["equity"]) + net, 4)
        acct["realized"] = round(float(acct["realized"]) + net, 4)
        acct["trades"] = int(acct["trades"]) + 1
        acct["wins"] = int(acct["wins"]) + (1 if net > 0 else 0)
        rec = {"symbol": p["symbol"], "side": side, "regime": p.get("regime"), "hour_utc": p.get("hour_utc"),
               "entry": entry, "exit": exit_px, "reason": reason, "net": round(net, 4), "r": round(r, 3),
               "fee": round(fee, 4), "funding": round(funding, 4), "liq_px": round(liq_px, 6), "tier": tier,
               "leverage": lev, "rationale": p.get("rationale"), "chart": p.get("chart"), "closed_ts": now_ms}
        _append(CLOSED, rec)
        _append(MEMORY, rec)   # self-learning: outcome tagged by context
        closed_n += 1
    save_account(acct)
    _rewrite(POSITIONS, still)
    return closed_n


# ---------------------------------------------------------------------------
# scorecard + loop
# ---------------------------------------------------------------------------
def _benchmark(client: Any, closed: list[dict[str, Any]]) -> dict[str, Any] | None:
    """BTC buy-hold over the same wall-clock window as the closed trades
    (plan #8 — HKUDS excess_return / hopit vs-buy-hold alpha). None until the
    window is meaningful (>=2 trades spanning >=1h); never fabricated."""
    ts = sorted(int(c.get("closed_ts") or 0) for c in closed if c.get("closed_ts"))
    if len(ts) < 2 or ts[-1] - ts[0] < 3_600_000:
        return None
    try:
        kl = client.futures_klines(symbol="BTCUSDT", interval="1h",
                                   startTime=ts[0], endTime=ts[-1], limit=1000)
        p0, p1 = float(kl[0][4]), float(kl[-1][4])
        btc = (p1 / p0 - 1) * 100
        agent = (sum(float(c.get("net") or 0) for c in closed) / START_EQUITY) * 100
        return {"btc_ret_pct": round(btc, 3), "agent_ret_pct": round(agent, 3),
                "excess_pct": round(agent - btc, 3)}
    except Exception:
        return None


def refresh_scorecard(client: Any) -> dict[str, Any]:
    """Recompute + persist the measured-edge scorecard (plan #5-#9). This is the
    ONLY thing allowed to answer 'is the LLM any good?' — measured P&L with
    bootstrap CI + permutation p-value, never vibes."""
    closed = _load(CLOSED)
    card = ls.scorecard(closed, benchmark=_benchmark(client, closed))
    LT_DIR.mkdir(parents=True, exist_ok=True)
    SCORECARD.write_text(json.dumps(card, indent=1, default=str), encoding="utf-8")
    return card


def run_once() -> dict[str, Any]:
    import time as _t
    from timebase import utc_now
    from tradingagents.binance.client import spot_client
    client = spot_client()
    now_ms = int(_t.time() * 1000)
    resolved = resolve(client, now_ms)
    acct = load_account()
    equity = float(acct["equity"])
    card = refresh_scorecard(client)
    open_now = _load(POSITIONS)
    margin_used = sum(float(x.get("margin") or 0) for x in open_now)
    blocked, why = lr.daily_breaker(_load(CLOSED), _day_anchor(acct, now_ms, equity), now_ms)
    status = {
        "scorecard": {"n": card["metrics"]["n"], "win_rate": card["metrics"]["win_rate"],
                      "mean_r": card["metrics"]["mean_r"], "liq_count": card["metrics"]["liq_count"],
                      "verdict": card["verdict"]["code"]},
        "capacity": {"open": len(open_now), "max_concurrent": MAX_CONCURRENT,
                     "margin_used_pct": round(margin_used / equity * 100, 1) if equity > 0 else 100.0,
                     "margin_cap_pct": MAX_TOTAL_MARGIN_PCT,
                     "daily_breaker": ("BLOCKED: " + why) if blocked else "ok"},
    }
    uni = us.select_universe(client, end_ms=now_ms, months=1.0, timeframe="1h",
                             min_daily_quote_volume=UNIVERSE_MIN_QVOL, max_symbols=UNIVERSE_MAX)
    ctx = build_context(client, uni["selected"], now_ms)
    decisions = decide(ctx, equity, status=status)
    opened = open_positions(decisions, equity, utc_now(), now_ms=now_ms)
    wr = round(acct["wins"] / acct["trades"], 3) if acct["trades"] else None
    return {"equity": acct["equity"], "trades": acct["trades"], "win_rate": wr,
            "opened": opened, "resolved": resolved, "open": len(_load(POSITIONS)),
            "considered": len(ctx), "acted": len(decisions), "model": MODEL,
            "verdict": card["verdict"]["code"], "breaker": status["capacity"]["daily_breaker"],
            "live": "LOCKED"}


def _hb(last: dict[str, Any], status="running"):
    from atomic_state import write_json_atomic
    from timebase import utc_now
    write_json_atomic(HEARTBEAT, {"agent": "llm_trader", "pid": os.getpid(), "ts": utc_now(),
                                  "updated_at": utc_now(), "status": status, "last_run": last})


if __name__ == "__main__":
    import argparse, time
    ap = argparse.ArgumentParser(description="LLM discretionary PAPER trader (paper-only)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval-seconds", type=float, default=90.0)  # higher frequency (cycle is LLM-bound ~2m; small sleep = back-to-back)
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
