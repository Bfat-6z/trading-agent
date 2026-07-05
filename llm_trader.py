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
PENDING = LT_DIR / "pending.jsonl"        # limit orders waiting to fill (no FOMO market entry)
MEMORY = LT_DIR / "memory.jsonl"          # context-tagged trade outcomes (self-learning)
SCORECARD = LT_DIR / "scorecard.json"     # measured-edge scorecard (plan #5-#9)
PID_FILE = LT_DIR / "llm_trader.pid"
STOP_FILE = LT_DIR / "llm_trader.stop"
HEARTBEAT = LT_DIR / "llm_trader_heartbeat.json"

MODEL = os.environ.get("LLM_TRADER_MODEL", "cx/gpt-5.5")
BASE_URL = os.environ.get("LLM_TRADER_BASE", "http://localhost:20128/v1")
# Squeeze the model: no artificial token ceiling + max reasoning effort. The
# endpoint accepts reasoning_effort=high (verified: it engages deeper reasoning)
# and large max_tokens, so let it THINK as long as it needs. These are ceilings,
# not forced usage; a slow deep cycle just runs less often (loop is sequential).
MAX_DECISION_TOKENS = int(os.environ.get("LLM_TRADER_MAX_TOKENS", "16000"))
MAX_REFLECT_TOKENS = int(os.environ.get("LLM_TRADER_REFLECT_TOKENS", "6000"))
REASONING_EFFORT = os.environ.get("LLM_TRADER_REASONING_EFFORT", "high")
LLM_TIMEOUT = float(os.environ.get("LLM_TRADER_LLM_TIMEOUT", "300"))
TF = "15m"
START_EQUITY = 100.0
MAX_HOLD_BARS = 32
# OWNER RULES (hard): law = 5-10% size, x5/x10 only. Owner 2026-07-04 ('danh vol
# to len'): bias to the TOP of his size law (8-10%) and require BIG entry volume.
SIZE_PCT_MIN, SIZE_PCT_MAX = float(os.environ.get("LLM_TRADER_SIZE_MIN", "8")), 10.0
ALLOWED_LEVERAGE = (5, 10)
# entries need strong participation: vol_ratio >= this (his method: EMA+VOL+price;
# research: breakout on sub-1.5x volume = fakeout). A+ capitulation needs >=1.8 anyway.
MIN_ENTRY_VOL = float(os.environ.get("LLM_TRADER_MIN_ENTRY_VOL", "1.5"))
# PROVEN-ONLY (the fix for the bleed, owner 2026-07-04 'fix cai bat on do di'):
# 77 measured trades say LLM discretionary entries are -EV (-$0.14/trade); the one
# measured +EV play is the lab survivor set. In this mode the bot trades ONLY when
# a survivor's mechanical condition fires on live bars — evaluated by the SAME
# method_lab code that backtested it (zero mapping drift). The lab keeps testing
# new methods 24/7; new survivors auto-arm, dropped survivors auto-disarm.
PROVEN_ONLY = os.environ.get("LLM_TRADER_PROVEN_ONLY", "1") == "1"
# MISSION (boss's boss, 2026-07-05): grow \$100 -> \$1000, sizing at the bot's
# discretion. Sizing chosen by HALF-KELLY math on capitulation_long's FULL-SCALE
# stats (win 54.7%, ~1.6R payoff => full Kelly ~26% risk/fire is suicide-variance;
# half-ish = 5% equity risk/fire): margin 20% x10 with the method's 2.5% stop.
# Expected +0.73%/fire, ~316 fires to 10x. Leverage stays within the owner's
# x5/x10 law. Caps (95% total margin) + daily breaker still govern.
MISSION_START = float(os.environ.get("LLM_TRADER_MISSION_START", "100"))
MISSION_TARGET = float(os.environ.get("LLM_TRADER_MISSION_TARGET", "1000"))
# Size is NOT hardcoded (owner: 'let the agent REASON from the data, don't pick a
# number'). Each fire is sized by fractional-Kelly computed from THAT method's OWN
# measured win-rate + payoff (full-scale OOS stats in survivors.json). Strong edge
# -> bigger bet, weak edge -> smaller, all derived, all auto-updating as the lab
# re-measures. KELLY_FRACTION 0.25 (quarter-Kelly) is the estimation-error-robust
# standard for a finite sample with correlated concurrent fires. Per-trade margin
# is clamped so one bet can't dominate; the 95% total-margin cap governs slot count
# emergently (no fixed slot number).
KELLY_FRACTION = float(os.environ.get("LLM_TRADER_KELLY_FRACTION", "0.25"))
MECH_SIZE_MIN, MECH_SIZE_MAX = 5.0, 33.0   # margin % clamp per fire
# SCOPE + FREQUENCY (owner: wider coin scan, higher frequency). Universe is
# re-selected each cycle by quote-volume; more concurrent slots so a wider scan
# actually turns into more live trades (the batched decision is still ONE LLM
# call regardless of coin count, so breadth is ~free on the model side).
UNIVERSE_MAX = int(os.environ.get("LLM_TRADER_UNIVERSE_MAX", "220"))  # FULL validated universe (~205 coins @ $5M): signals change per 15m bar-close, so a 2-3min full sweep misses nothing
UNIVERSE_MIN_QVOL = float(os.environ.get("LLM_TRADER_MIN_QVOL", "50000000"))  # LIQUID established coins only: capitulation on $5-20M micro-caps = falling-knife (they dump straight, do not mean-revert). The edge is real only where liquidity is.
# Owner (2026-07-05): "danh may con vol 1h to thoi ... neu danh chart 1h thi quet
# 1h, 15m thi quet 15m, 4h thi quet 4h". So the universe is ranked by RECENT money
# flow on the timeframe we actually trade — not stale 24h volume (a coin can be big
# on 24h yet dead right now). The $50M/24h floor stays as an anti-trash liquidity
# base; among those, keep the hottest by recent SCAN_TF volume.
SCAN_TF = os.environ.get("LLM_TRADER_SCAN_TF", "1h")               # timeframe whose recent volume ranks the universe
SCAN_WINDOW_BARS = int(os.environ.get("LLM_TRADER_SCAN_WINDOW", "24"))   # sum this many recent SCAN_TF bars = "hot money flow now"
UNIVERSE_HOT_TOP = int(os.environ.get("LLM_TRADER_HOT_TOP", "60"))       # keep top-N liquid coins by recent SCAN_TF volume
UNIVERSE_REFRESH_SEC = float(os.environ.get("LLM_TRADER_UNIVERSE_REFRESH", "1800"))  # rebuild the hot list every ~30 min (bounds klines API calls)
UNIVERSE_CACHE = LT_DIR / "universe_cache.json"
# Owner: UNLIMITED number of positions — accepts correlation risk for bigger
# upside. No trade-count cap (50 >> the 30-coin universe, 1-per-symbol, so it
# never binds). MARGIN is the only physical limit: at 5-10%/trade on $100 you can
# hold ~10-19 positions before capital is fully deployed (95% cap leaves a hair).
# The daily -15% breaker stays as the sole backstop (stops NEW trades after a bad
# day; does not close positions).
MAX_CONCURRENT = int(os.environ.get("LLM_TRADER_MAX_CONCURRENT", "50"))
MAX_TOTAL_MARGIN_PCT = float(os.environ.get("LLM_TRADER_MAX_MARGIN_PCT", "95"))
# Owner (2026-07-05): "bo cai phanh ngay do di" — DISABLE the daily-loss breaker.
# Owner accepts the risk (paper account). Set to "1" to re-arm. NOTE: with this OFF
# there is NO daily circuit-breaker; a run of losing capitulation fires on a hard
# down-day can compound past -15% with nothing halting new entries. The per-position
# sizing (mech_sizing), $50M liquidity floor and 95% total-margin cap are the only
# remaining guards. Live orders stay LOCKED regardless.
DAILY_BREAKER_ON = os.environ.get("LLM_TRADER_DAILY_BREAKER", "0") != "0"
# DATA-ACCUMULATION mode (owner): trade B+ setups to build a measured track record
# fast, instead of sitting idle waiting for the rare A+. Lower confluence gate +
# an explicit "act, don't over-skip" directive. Honest tradeoff: more trades on a
# not-yet-proven strategy = faster data AND faster bleed; the scorecard is the judge.
MIN_CONFLUENCE = int(os.environ.get("LLM_TRADER_MIN_CONFLUENCE", "2"))
EXPLORE_MODE = os.environ.get("LLM_TRADER_EXPLORE", "1") == "1"


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
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(r, default=str) + "\n" for r in rows), encoding="utf-8")
    os.replace(tmp, path)   # atomic: concurrent readers never see a half-written file


def _append(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def _dedupe_closed(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop double-booked closed trades (a position can only close once; a
    concurrent-loop overlap can append it twice with different closed_ts). Key on
    the trade identity, NOT closed_ts, so a re-book collapses to one."""
    seen, out = set(), []
    for r in rows:
        if r.get("net") is None:
            out.append(r); continue
        k = (r.get("symbol"), r.get("side"), round(float(r.get("entry", 0) or 0), 4),
             round(float(r.get("exit", 0) or 0), 4), round(float(r.get("net", 0) or 0), 4), r.get("reason"))
        if k in seen:
            continue
        seen.add(k); out.append(r)
    return out


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


_WHALE_PATH = ROOT / "state" / "agent_memory" / "whale_flow_latest.json"


def _whale_flow_map() -> dict[str, dict[str, Any]]:
    """Per-symbol whale/liquidation pressure from whale_flow_observer (public
    Telegram t.me scraping — BinanceLiquidations/WhaleBotAlerts/whale_alert_io...).
    Compact + best-effort; {} if the observer hasn't written yet."""
    try:
        wf = json.loads(_WHALE_PATH.read_text(encoding="utf-8"))
        out = {}
        for sym, r in (wf.get("by_symbol") or {}).items():
            out[sym] = {"side": r.get("pressure_side"), "score": r.get("pressure_score"),
                        "events": r.get("event_count"),
                        "long_liq": round(float(r.get("long_liquidation_notional", 0) or 0), 0),
                        "short_liq": round(float(r.get("short_liquidation_notional", 0) or 0), 0)}
        return out
    except Exception:
        return {}


def build_context(client: Any, symbols: list[str], now_ms: int) -> list[dict[str, Any]]:
    import backtest_data_fetcher as bf
    out = []
    whale = _whale_flow_map()   # per-symbol Telegram whale/liquidation pressure
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
                "whale": whale.get(sym),   # Telegram whale/liquidation pressure (may be None)
                "funding_rate": round(float(enr["funding_rate"].iloc[i]) if "funding_rate" in enr else 0.0, 6),
                "cvd_norm": round(float(enr["cvd_delta_norm"].iloc[i]) if "cvd_delta_norm" in enr and enr["cvd_delta_norm"].iloc[i]==enr["cvd_delta_norm"].iloc[i] else 0.0, 3),
                "atr": round(float(enr["atr"].iloc[i]), 4),
                # 24h quote volume (96 x 15m bars) drives the fee/slippage
                # liquidity tier; missing data -> 0.0 -> "micro" (pessimistic).
                "_quote_vol_24h": round(sum(float(b.get("quote_volume", 0.0)) for b in fb[-96:]), 0),
                # last ~220 closed bars for chart rendering (underscore -> never
                # sent as text; used only to draw the candlestick+EMA+vol image).
                "_bars": fb[-220:],
                "_funding": fund[-120:] if fund else None,   # for funding-based proven methods
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


_LAB_SURVIVORS = ROOT / "state" / "method_lab" / "survivors.json"


def _proven_methods_block() -> str:
    """Inject the Method Lab survivors — mechanical methods that beat a walk-forward
    out-of-sample + Bonferroni permutation test on real history — so the bot FAVORS
    setups that are actually proven, not just plausible. Auto-updates as the lab
    re-curates: a method that stops surviving simply disappears from the prompt."""
    try:
        surv = json.loads(_LAB_SURVIVORS.read_text(encoding="utf-8"))
    except Exception:
        surv = []
    surv = [s for s in surv if s.get("survived")]
    if not surv:
        return ""
    lines = []
    for s in surv[:6]:
        lines.append(f"- {s.get('side')} \"{s.get('desc')}\" — PROVEN out-of-sample: mean R "
                     f"{s.get('oos_mean_r')}, win {s.get('oos_win_rate')}, p={s.get('pvalue')} over "
                     f"{s.get('oos_n')} trades. When this exact setup appears, strongly favor taking it.")
    return ("=== PROVEN METHODS (survived walk-forward on real data — these have measured edge; "
            "prioritize them) ===\n" + "\n".join(lines) + "\n=== END PROVEN METHODS ===\n\n")


_REFLECT_PATH = LT_DIR / "self_reflection.json"


def _reflect() -> dict[str, Any]:
    """Meta-cognition: the bot REASONS about its own results and self-directs. It
    reads its P&L, measured mistakes, proven methods, and recent rationale-vs-
    outcome, thinks about what is actually working vs its habits, and emits
    directives that get injected into future decisions. Best-effort."""
    try:
        closed = _dedupe_closed(_load(CLOSED))
        booked = [c for c in closed if c.get("net") is not None]
        if len(booked) < 8:
            return {}
        acct = load_account()
        try:
            surv = json.loads(_LAB_SURVIVORS.read_text(encoding="utf-8"))
        except Exception:
            surv = []
        recent = [{"sym": c.get("symbol"), "side": c.get("side"), "net": round(float(c.get("net", 0)), 3),
                   "reason": c.get("reason"), "i_said": (c.get("rationale") or "")[:110]} for c in booked[-12:]]
        state = {"equity": acct.get("equity"), "realized": acct.get("realized"),
                 "n_trades": len(booked), "win_rate": round(sum(1 for c in booked if c["net"] > 0) / len(booked), 3),
                 "measured_mistakes": ltm.mistake_lessons(closed),
                 "proven_methods": [s.get("desc") for s in surv if s.get("survived")],
                 "recent_rationale_vs_outcome": recent}
        mode = ("You are in DATA-ACCUMULATION mode: the goal is to build a measured track record, so your directives "
                "must IMPROVE trade SELECTION (avoid the specific traps that lose), NOT stop trading altogether. "
                "Do NOT say 'default SKIP' or 'max 1 trade' — instead say which setups to PREFER vs AVOID. "
                if EXPLORE_MODE else "")
        sysp = ("You are the trading agent reflecting on your OWN performance (meta-cognition). Think HARD and be "
                "brutally honest: you are losing money, so vague optimism is useless. Reason about WHY you lose, "
                "whether your stated rationales actually matched outcomes (did 'bull stack' setups get stopped?), "
                "what is genuinely working (proven_methods) versus your reflexive habits, and what you must change. "
                + mode +
                "Reply ONLY a JSON object: {\"reflection\":\"3-4 sentences of honest self-analysis\","
                "\"directives\":[\"3-5 concrete changes to apply to your next decisions\"]}")
        raw = _llm(sysp, json.dumps(state, default=str), max_tokens=MAX_REFLECT_TOKENS)
        obj = None
        if raw:                          # parse the OBJECT (not _extract_json, which
            a, b = raw.find("{"), raw.rfind("}")   # would grab the inner directives array)
            if a >= 0 and b > a:
                try:
                    obj = json.loads(raw[a:b + 1])
                except Exception:
                    obj = None
        if isinstance(obj, dict) and obj.get("directives"):
            # strip markup — this text reaches a publicly tunneled page via innerHTML
            _cln = lambda x: str(x).replace("<", "").replace(">", "")
            obj["reflection"] = _cln(obj.get("reflection", ""))
            obj["directives"] = [_cln(d) for d in obj.get("directives", [])][:6]
            import time as _t
            obj["ts"] = int(_t.time() * 1000)
            LT_DIR.mkdir(parents=True, exist_ok=True)
            _REFLECT_PATH.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
            return obj
    except Exception:
        pass
    return {}


def _maybe_reflect(now_ms: int, min_gap_ms: int = 1_800_000) -> None:
    """Run meta-cognition at most every ~30 min (it's an extra LLM call)."""
    try:
        last = 0
        if _REFLECT_PATH.exists():
            last = int(json.loads(_REFLECT_PATH.read_text(encoding="utf-8")).get("ts", 0))
        if now_ms - last >= min_gap_ms:
            _reflect()
    except Exception:
        pass


def _reflection_block() -> str:
    """Inject the bot's own self-reflection directives into the decision prompt."""
    try:
        obj = json.loads(_REFLECT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return ""
    ds = obj.get("directives") or []
    if not ds:
        return ""
    head = (obj.get("reflection", "").strip() + "\n") if obj.get("reflection") else ""
    return ("=== YOUR SELF-REFLECTION (you reasoned about your own results — follow your own conclusions) ===\n"
            + head + "\n".join(f"- {d}" for d in ds[:6]) + "\n=== END REFLECTION ===\n\n")


def _kelly_size_pct(win: float, payoff: float, sl_pct: float, lev: int) -> float:
    """Fractional-Kelly margin % derived from a method's measured edge.

    Binary Kelly (fraction of equity to RISK) = p - (1-p)/b, b = payoff (tp/sl).
    Take KELLY_FRACTION of it (quarter-Kelly), then convert risk -> margin:
    on a stop we lose sl_pct*leverage of the margin, so margin% = risk% /
    (sl_pct*lev). Clamped. Non-positive edge -> min size (shouldn't happen: only
    survivors fire). Everything comes from the data, nothing is picked by hand."""
    try:
        b = max(0.2, float(payoff))
        f_full = float(win) - (1.0 - float(win)) / b           # Kelly risk fraction
        risk = max(0.0, f_full) * KELLY_FRACTION
        size = risk / (max(0.003, float(sl_pct) / 100.0) * max(1, lev)) * 100.0
        return round(max(MECH_SIZE_MIN, min(MECH_SIZE_MAX, size)), 2)
    except Exception:
        return MECH_SIZE_MIN


_ARMED_METHODS = ROOT / "state" / "method_lab" / "armed_methods.json"


def _survivor_methods() -> list[dict[str, Any]]:
    """Methods the mission bot is allowed to fire, joined to their DSL conditions.

    Source of truth is armed_methods.json — a STABLE, hand-curated set validated on
    the liquid universe with deep-optimal SL/TP/timeout. The 3-hourly method_lab
    runner rewrites survivors.json on its own ($5M) universe; letting that govern real
    money silently disarmed the liquid-validated set (owner saw 'only LONG' after it
    dropped everything but capitulation). So live arming reads armed_methods.json and
    falls back to survivors.json only if the curated file is absent."""
    try:
        from method_seeds import SEED_METHODS
        defs = {m["id"]: m for m in SEED_METHODS}
        pool = ROOT / "state" / "method_lab" / "methods_pool.jsonl"
        if pool.exists():
            for line in pool.read_text(encoding="utf-8").splitlines():
                try:
                    m = json.loads(line)
                    defs[m["id"]] = m
                except Exception:
                    pass
        src = None
        if _ARMED_METHODS.exists():
            src = json.loads(_ARMED_METHODS.read_text(encoding="utf-8"))
        if not src:
            surv = json.loads(_LAB_SURVIVORS.read_text(encoding="utf-8"))
            src = [{"id": s["id"], "oos_win_rate": s.get("oos_win_rate"),
                    "oos_mean_r": s.get("oos_mean_r")} for s in surv if s.get("survived")]
        out = []
        for s in src:
            d = defs.get(s["id"])
            if not d:
                continue
            m = {**d}
            if s.get("sl_pct") is not None:      # deep-optimal exits override the pool default
                m["sl_pct"] = s["sl_pct"]
            if s.get("tp_pct") is not None:
                m["tp_pct"] = s["tp_pct"]
            m["timeout"] = s.get("timeout")
            m["oos_win_rate"] = s.get("oos_win_rate")
            m["oos_mean_r"] = s.get("oos_mean_r")
            out.append(m)
        return out
    except Exception:
        return []


def _mechanical_decisions(context: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """PROVEN-ONLY decide: fire a trade ONLY where a lab-survivor method's DSL
    condition holds on the coin's live CLOSED bars — evaluated with method_lab's
    own feature_frame/method_fires (the exact code that proved it). Execution is
    faithful to what was backtested: the method's own sl/tp %, x10 (owner wants
    size at max conviction), no structure override, no trailing, 16-bar timeout."""
    import method_lab as ml
    import mech_sizing as msz
    methods = _survivor_methods()
    if not methods:
        return []
    # PASS 1: find every (coin, method) that FIRES this bar (one method per coin).
    fires, ctxs = [], {}
    for c in context:
        bars = c.get("_bars")
        if not bars or len(bars) < 220:
            continue
        try:
            row = ml.feature_frame(bars, funding=c.get("_funding"))[-1]
        except Exception:
            continue
        for m in methods:
            if ml.method_fires(row, m):
                fires.append((c["symbol"], m["id"]))
                ctxs[c["symbol"]] = (c, m, row)
                break
    if not fires:
        return []
    # PASS 2: BATCH size the whole firing cluster together — correlation is a
    # property of the cluster, not a single fire (risk review). mech_sizing does
    # empirical Kelly + LCB shrinkage + correlation divisor + drawdown governor +
    # aggregate exposure cap; margins here are FINAL (open_positions uses them raw).
    try:
        dists = json.loads((LT_DIR.parent / "method_lab" / "survivor_distributions.json").read_text(encoding="utf-8"))
    except Exception:
        dists = {}
    # Selection-bias guard (Codex review): a method's distribution is winner-biased
    # until it is confirmed on truly out-of-sample LIVE forward-test data. Mark
    # forward_confirmed only when the shadow ledger shows >=30 fresh trades that are
    # net-positive; mech_sizing then lifts the half-size haircut for that method.
    side_by_id = {m["id"]: m.get("side") for m in methods}
    for mid, d in dists.items():
        d["side"] = side_by_id.get(mid)          # side-aware crisis correlation in mech_sizing
    try:
        fstats = json.loads((ROOT / "state" / "forward_test" / "shadow_stats.json").read_text(encoding="utf-8")).get("methods", {})
        for mid, d in dists.items():
            fs = fstats.get(mid) or {}
            d["forward_confirmed"] = bool((fs.get("n") or 0) >= 30 and (fs.get("net_pct") or 0) > 0
                                          and (fs.get("mean_r") or 0) > 0)
    except Exception:
        pass
    sized = {(o["coin"], o["method"]): o for o in msz.size_fires(fires, dists, lev=10)}
    out = []
    for (sym, mid) in fires:
        o = sized.get((sym, mid))
        if not o:                                    # sizer skipped it (edge shrank away / capped out)
            _append(LT_DIR / "governance.jsonl", {"event": "proven_fire_unsized", "symbol": sym, "method": mid})
            continue
        c, m, row = ctxs[sym]
        chart = None
        try:
            chart = ltc.render_chart(sym, c["_bars"], tf=TF, title_suffix=" · PROVEN " + mid)
        except Exception:
            pass
        _slp, _tpp = float(m.get("sl_pct", 2.5)), float(m.get("tp_pct", 4.0))
        out.append({**c, "action": m.get("side", "LONG"), "leverage": 10,
                    "size_pct": o["margin_pct"], "sl_pct": _slp, "tp_pct": _tpp, "entry_px": None,
                    "_mech": True, "_max_hold": int(m.get("timeout") or 16), "_chart_b64": chart,
                    "rationale": f"PROVEN {mid} (win {m.get('oos_win_rate')}, sized {o['margin_pct']}% margin, "
                                 f"cluster={len(fires)}): {m.get('desc','')[:60]}"})
        _append(LT_DIR / "governance.jsonl",
                {"event": "proven_fire", "symbol": sym, "method": mid, "margin_pct": o["margin_pct"],
                 "cluster_size": len(fires), "rsi": row.get("rsi14"), "vol": row.get("vol_ratio")})
    return out


def _mistakes_block() -> str:
    """Surface the measured failure-mode lessons PROMINENTLY in the system prompt
    (not buried in the memory JSON) so the model actually corrects them — the
    'learn from your own mistakes' channel. Computed from realized P&L each cycle."""
    try:
        ms = ltm.mistake_lessons(_dedupe_closed(_load(CLOSED)))
    except Exception:
        ms = []
    if not ms:
        return ""
    lines = "\n".join(f"- {m}" for m in ms)
    return ("=== YOUR MEASURED MISTAKES (from your OWN losing trades — actively correct these; "
            "do NOT repeat them) ===\n" + lines + "\n=== END MISTAKES ===\n\n")


def memory_context() -> dict[str, Any]:
    """Distilled learning context injected into decide()'s prompt each cycle.

    Aggregates ALL closed trades (not the last-8 raw rows) into grouped stats,
    data-phrased lessons and rationale-vs-outcome recents via the pure
    llm_trader_memory module — plan 260702 checklist #10/#11. Reads CLOSED
    (canonical append-only log written by resolve); llm_trader_memory
    guarantees malformed rows are skipped, so this can't kill the loop."""
    return ltm.build_memory_context(_dedupe_closed(_load(CLOSED)))


# ---------------------------------------------------------------------------
# LLM decision (9router, OpenAI-compatible)
# ---------------------------------------------------------------------------
def _llm(system: str, user: str, max_tokens: int | None = None) -> str | None:
    """Text-only chat call via the SAME direct 9router path that _llm_vision uses
    (reliable), not call_large_model (which hangs here). Full reasoning effort + no
    tight token ceiling. Returns text or None."""
    if max_tokens is None:
        max_tokens = MAX_DECISION_TOKENS
    base, key = _env_llm()
    if not base or not key:
        try:
            from llm_reasoning_agent import call_large_model
            return call_large_model(system, user, model=MODEL, max_tokens=max_tokens)
        except Exception:
            return None
    body = json.dumps({"model": MODEL, "max_tokens": max_tokens, "temperature": 0.3,
                       "reasoning_effort": REASONING_EFFORT,
                       "messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": user}]}).encode()
    req = urllib.request.Request(base + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + key}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
            return json.loads(r.read().decode())["choices"][0]["message"]["content"]
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


_PLAYBOOK_CACHE: str | None = None


def _playbook() -> str:
    """The researched 15m TA playbook (ta_playbook.md) injected into the decision
    prompt so gpt-5.5 reads charts by an explicit confluence checklist (EMA stack,
    S/R zones, RSI-50, candle+volume triggers) instead of winging it. Cached;
    empty string if the file is missing (feature degrades gracefully)."""
    global _PLAYBOOK_CACHE
    if _PLAYBOOK_CACHE is None:
        try:
            _PLAYBOOK_CACHE = (ROOT / "ta_playbook.md").read_text(encoding="utf-8").strip()
        except Exception:
            _PLAYBOOK_CACHE = ""
    return _PLAYBOOK_CACHE


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
    body = json.dumps({"model": MODEL, "max_tokens": MAX_DECISION_TOKENS, "temperature": 0.3,
                       "reasoning_effort": REASONING_EFFORT,
                       "messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": content}]}).encode()
    req = urllib.request.Request(base + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + key}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
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
        # LIMIT entry FIRST: keep entry_px only if it's a FAVORABLE pullback limit
        # within ~5% (LONG below price / SHORT above); else treat as market.
        entry_px = None
        try:
            ep = float(dec.get("entry_px") or 0)
            px = float(ctx["price"])
            if ep > 0 and abs(ep / px - 1) <= 0.05 and (
                    (action == "LONG" and ep < px) or (action == "SHORT" and ep > px)):
                entry_px = round(ep, 8)
        except Exception:
            entry_px = None
        # HARD GATE (code-enforced; the model ignores prompt bans): block the
        # measured chase — LONG at RSI>=65 while EXTENDED above EMA20. Judged at the
        # EFFECTIVE entry: a pullback LIMIT at/below the EMA20 zone is exactly the
        # disciplined behavior we want, so it passes (audit: the old order blocked it).
        try:
            rsi = float(ctx.get("rsi14") or 50)
            ext = float(ctx.get("px_vs_ema20_pct") or 0)
            px = float(ctx["price"])
            eff_ext = ext if entry_px is None else ((entry_px / px) * (1 + ext / 100.0) - 1) * 100.0
            if action == "LONG" and rsi >= 65 and eff_ext > 0.5:
                _append(LT_DIR / "governance.jsonl",
                        {"event": "gate_block_chase", "symbol": sym, "rsi": rsi, "ext_pct": ext,
                         "entry_px": entry_px, "eff_ext_pct": round(eff_ext, 3),
                         "note": "LONG RSI>=65 extended at effective entry — the measured noise-stop chase"})
                continue
            # VOL GATE (owner 'danh vol to len' + his EMA+VOL+price method): no
            # entry without strong participation. The one recent TP win entered at
            # vol 2.99x; the stopped-out losers mostly entered on quiet bars.
            vr = float(ctx.get("vol_ratio") or 1.0)
            if vr < MIN_ENTRY_VOL:
                _append(LT_DIR / "governance.jsonl",
                        {"event": "gate_block_low_vol", "symbol": sym, "vol_ratio": vr,
                         "min": MIN_ENTRY_VOL})
                continue
        except Exception:
            pass
        out.append({**ctx, "action": action, "leverage": lev, "size_pct": size_pct,
                    "sl_pct": sl_pct, "tp_pct": tp_pct, "entry_px": entry_px,
                    "rationale": str(dec.get("rationale", ""))[:240]})
    return out


_MEMORY_RULE = ("Learn from your MEMORY block CONTEXTUALLY: the counts are evidence to weigh, not bans — a past "
                "loss does NOT blanket-ban a setup; the same idea can win on another coin/regime/time (markets are "
                "non-stationary). Pick only the BEST setups; SKIP is common and fine — no forced trades. "
                "Owner rules: leverage EXACTLY 5 or 10; size 8-10% of equity (owner wants size at the TOP of his "
                "5-10 law); entries REQUIRE vol_ratio>=1.5 (his EMA+VOLUME+price method — quiet-bar entries are "
                "code-rejected, so don't propose them; wait for the volume bar or set a limit into it).")
_DECISION_SCHEMA = (
    "THINK step-by-step FIRST, then decide. Output EXACTLY two sections separated by a line '===DECISIONS==='.\n"
    "THINKING:\nReason out loud. For EACH charted coin, in order: (1) TREND — EMA stack + slope + MTF agree? "
    "(2) LOCATION — is price AT a proven support/resistance zone or a BOS/CHoCH retest, or stranded mid-range? "
    "(3) CONFLUENCE — count INDEPENDENT signals from DISTINCT families (trend / location / momentum-trigger+volume "
    f"/ whale); RSI+candle+volume = ONE family. (4) GATE — does it clear >={MIN_CONFLUENCE} confluences AND give "
    "R:R>=1.5 after ~0.1% fees to a REAL zone? Which of YOUR MEASURED MISTAKES would this setup repeat? "
    + ("You are in DATA-ACCUMULATION mode: ACT on solid B+ setups that clear the gate to build a measured track "
       "record — do NOT skip everything waiting for the rare perfect A+. Take up to 1-3 qualifying coins per cycle, "
       "but ZERO is the correct answer when every candidate is the same trap: a LONG at RSI>=65 extended above "
       "EMA20 is a CHASE (your measured #1 loss pattern — code will reject it anyway); wait for the PULLBACK to the "
       "zone/EMA20 or set a limit there instead. Do not relabel a chase as 'Best only'." if EXPLORE_MODE else
       "Be STRICT: default SKIP — most coins should be SKIP, taking a marginal trade is the mistake you keep making.")
    + " PREFER A LIMIT ENTRY: set entry_px at a PULLBACK level (support-zone edge / EMA20 for a long, "
    "resistance edge / EMA20 for a short) so you buy the dip / sell the rip at a GOOD price — do NOT FOMO-chase the "
    "current extended price. Only omit entry_px (market enter) for a confirmed breakout that won't retrace. A limit "
    "waits for price to come to you and cancels if it runs away — this is how a disciplined trader enters.\n"
    "===DECISIONS===\nThen a JSON ARRAY (may be empty) of ONLY coins that PASSED the gate: "
    "[{\"symbol\":\"BTCUSDT\",\"action\":\"LONG|SHORT|SKIP\",\"leverage\":5|10,\"size_pct\":5-10,"
    "\"entry_px\":<limit price, or omit for market>,\"sl_pct\":0.5-5,\"tp_pct\":0.5-10,"
    "\"rationale\":\"cite levels + how many confluences\"}]")


_THINK_PATH = LT_DIR / "thinking_latest.json"


def _split_thinking(raw: str | None) -> Any:
    """Split the model's 'THINKING: ... ===DECISIONS=== [json]' reply: persist the
    reasoning trace (for the dashboard) and return the parsed decisions JSON. Falls
    back to whole-text JSON extraction if the model skipped the delimiter."""
    if not raw:
        return None
    think, _, tail = raw.partition("===DECISIONS===")
    if not tail:                       # model didn't use the delimiter
        think, tail = "", raw
    trace = think.replace("THINKING:", "").strip()
    if trace:
        try:
            import time as _t
            LT_DIR.mkdir(parents=True, exist_ok=True)
            _THINK_PATH.write_text(json.dumps({"ts": int(_t.time() * 1000), "thinking": trace[:4000]},
                                              ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return _extract_json(tail)


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
           + _reflection_block() + _proven_methods_block() + _mistakes_block() + _MEMORY_RULE + " " + _DECISION_SCHEMA)
    usr = json.dumps({"equity": round(equity, 2), "your_status": status or {},
                      "memory": memory_context(), "coins": payload}, default=str)
    return _validate_decisions(_split_thinking(_llm(sys, usr)), by_sym)


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
    # A+ OVERRIDE: the lab-proven capitulation setup (rsi<22 + vol>=1.8) is too rare
    # to ever miss — a coin printing it gets charted regardless of activity rank
    # (top-5-only scanning would have missed the LTC/INJ flushes outside the list).
    aplus = [c for c in ranked if float(c.get("rsi14") or 50) < 22
             and float(c.get("vol_ratio") or 1) >= 1.8]
    for c in aplus:
        if c not in shortlist:
            shortlist = [c] + shortlist[:max_charts - 1]
            c["a_plus_pure"] = True   # flagged in coins_txt so the model sees it
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
    mem = memory_context()   # stats + lessons + MISTAKES + recent (computed once)
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
           "shorting into support. Each coin may also carry a 'whale' block (public Telegram whale/liquidation "
           "flow: pressure_side LONG/SHORT + score, and long/short liquidation $). Treat it as a LAGGING, NOISY "
           "confluence hint ONLY — whale pressure agreeing with your chart setup adds a little confidence; a big "
           "opposite-side liquidation can mark a flush/reversal; NEVER trade on whale flow alone or chase it.\n\n"
           + (_playbook() and ("=== TRADING PLAYBOOK (apply this) ===\n" + _playbook() + "\n=== END PLAYBOOK ===\n\n"))
           + _reflection_block() + _proven_methods_block() + _mistakes_block() + _MEMORY_RULE + " " + _DECISION_SCHEMA)
    text = json.dumps({"equity": round(equity, 2), "your_status": status or {},
                       "memory": mem, "charted_coins": coins_txt,
                       "market_overview": market_overview}, default=str)
    out = _validate_decisions(_split_thinking(_llm_vision(sys, text, images)), by_sym)
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


def _structure_sl_tp(side: str, entry: float, d: dict[str, Any]) -> tuple[float | None, float | None]:
    """Place the stop BEYOND real structure (nearest S/R zone edge or SMC
    invalidation) + 0.5*ATR — not the LLM's arbitrary %. TP targets the opposing
    zone. Two hard audit rules: (a) if the structure stop is further than 6% we do
    NOT clamp the stop back INSIDE the zone (that engineers a noise-stop in a known
    bounce area) — we fall back to the LLM %; (b) if a REAL opposing zone caps the
    reward below 1.5R we return (None, None) = SKIP, never synthesize a TP through
    the zone the SMC engine says will reject price. The 1.8R synthetic TP is used
    only when NO opposing zone exists. Owner: 'SL/TP must fit the chart.'"""
    smc = d.get("_smc") or {}
    atr = float(d.get("atr") or 0.0)
    buf = 0.5 * atr
    sup = smc.get("nearest_support") or {}
    res = smc.get("nearest_resistance") or {}
    inval = smc.get("invalidation")
    sl = tp = None
    zone_tp = False
    try:
        if side == "LONG":
            base = (float(sup["lo"]) if sup.get("lo") and float(sup["lo"]) < entry
                    else float(inval) if inval and float(inval) < entry else None)
            if base:
                sl = base - buf
            if res.get("lo") and float(res["lo"]) > entry:
                tp = float(res["lo"]); zone_tp = True
        else:
            base = (float(res["hi"]) if res.get("hi") and float(res["hi"]) > entry
                    else float(inval) if inval and float(inval) > entry else None)
            if base:
                sl = base + buf
            if sup.get("hi") and float(sup["hi"]) < entry:
                tp = float(sup["hi"]); zone_tp = True
    except Exception:
        sl = tp = None
        zone_tp = False
    if sl is not None:
        risk = abs(entry - sl)
        if risk > entry * 0.06:
            sl = None                       # structure too far -> NOT a structure trade (never clamp into the zone)
        elif risk < entry * 0.004:          # widen a hair AWAY from the zone (deeper beyond structure)
            sl = entry * (1 - 0.004) if side == "LONG" else entry * (1 + 0.004)
    if sl is None:                          # no usable structure -> the LLM's stop
        sl = entry * (1 - d["sl_pct"] / 100) if side == "LONG" else entry * (1 + d["sl_pct"] / 100)
    risk = abs(entry - sl)
    min_tp = 1.5 * risk
    if zone_tp:
        reward = (tp - entry) if side == "LONG" else (entry - tp)
        if reward < min_tp:
            return None, None               # structurally bad R:R -> SKIP the trade
    else:
        tp = entry + 1.8 * risk if side == "LONG" else entry - 1.8 * risk
    return sl, tp


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
    blocked, why = lr.daily_breaker(_load(CLOSED), day_start, now_ms) if DAILY_BREAKER_ON else (False, "")
    if blocked:
        _append(LT_DIR / "governance.jsonl",
                {"ts_ms": now_ms, "event": "daily_breaker_block", "why": why,
                 "skipped": [d.get("symbol") for d in decisions]})
        _rewrite(POSITIONS, open_pos)
        return 0
    n = 0
    pend_syms = {q["symbol"] for q in _load(PENDING)}
    for d in decisions:
        if d["symbol"] in open_syms:
            continue
        # LIMIT entry (owner: 'wait for the pullback, don't FOMO market in'): queue a
        # pending order at the limit price instead of a market fill; _resolve_pending
        # fills it if price comes to the level, cancels if it runs away / expires.
        if d.get("entry_px"):
            if d["symbol"] not in pend_syms:
                _append(PENDING, {"symbol": d["symbol"], "side": d["action"], "entry_px": float(d["entry_px"]),
                                  "leverage": d["leverage"], "size_pct": d["size_pct"],
                                  "sl_pct": d["sl_pct"], "tp_pct": d["tp_pct"], "smc": d.get("_smc") or {},
                                  "atr": float(d.get("atr") or 0), "quote_vol_24h": float(d.get("_quote_vol_24h", 0) or 0),
                                  "vol": d.get("vol_ratio"), "regime": d.get("regime"), "chart_b64": d.get("_chart_b64"),
                                  "price0": float(d["price"]), "placed_ms": now_ms, "ts": d.get("_ts"),
                                  "rationale": d.get("rationale", "")})
                pend_syms.add(d["symbol"])
            else:
                _append(LT_DIR / "governance.jsonl",
                        {"ts_ms": now_ms, "event": "pending_duplicate_skip", "symbol": d["symbol"]})
            # a limit-intent decision NEVER falls through to a market open — the
            # audit repro'd exactly that (market-bought the FOMO price it was
            # built to avoid) whenever the symbol already had a pending order.
            continue
        side = d["action"]; lev = d["leverage"]
        # Entry is a MARKET fill: apply adverse slippage by liquidity tier
        # (plan item #3 — the zero-slip entry was structurally optimistic).
        quote_vol = float(d.get("_quote_vol_24h", 0.0) or 0.0)
        tier = pcm.liquidity_tier(quote_vol)
        raw_px = float(d["price"])
        if d.get("_maker"):
            entry = raw_px                    # resting limit fills AT its price (maker)
        else:
            slip = float(pcm.fill_bps(tier)) / 10000.0
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
        if d.get("_mech"):
            # PROVEN method: execute EXACTLY what was backtested — its own sl/tp %,
            # no structure override (that machinery wasn't part of the proof).
            sl = entry * (1 - d["sl_pct"] / 100) if side == "LONG" else entry * (1 + d["sl_pct"] / 100)
            tp = entry * (1 + d["tp_pct"] / 100) if side == "LONG" else entry * (1 - d["tp_pct"] / 100)
        else:
            sl, tp = _structure_sl_tp(side, entry, d)   # SL beyond structure, not arbitrary %
            if sl is None:                                # structurally bad R:R (TP would shoot through a real zone)
                _append(LT_DIR / "governance.jsonl",
                        {"ts_ms": now_ms, "event": "gate_block_tp_through_zone", "symbol": d["symbol"]})
                continue
        # Forced-liquidation price (plan item #1): stored at open so resolve()
        # can rank liq ahead of SL pessimistically on every bar.
        mmr = lr.mmr_for(d["symbol"])
        liq_px = lr.liquidation_price(entry, lev, side, mmr)
        chart_rel = _save_entry_chart(d.get("_chart_b64"), d["symbol"], d["_ts"])
        open_pos.append({"symbol": d["symbol"], "side": side, "entry": entry, "qty": qty,
                         "margin": round(margin, 4), "leverage": lev, "sl": sl, "tp": tp,
                         "liq_px": liq_px, "mmr": mmr, "quote_vol_24h": quote_vol, "tier": tier,
                         "entry_ts": d["_ts"], "opened_at": now_iso, "regime": d["regime"],
                         "fill_bar_ts": d.get("_fill_bar_ts"),
                         "mech": bool(d.get("_mech")), "max_hold": (int(d.get("_max_hold") or 16) if d.get("_mech") else None),
                         "chart": chart_rel, "vol": d.get("vol_ratio"),   # volume at entry (owner watches this)
                         "hour_utc": (int(d["_ts"]) // 3600000) % 24, "rationale": d["rationale"]})
        open_syms.add(d["symbol"]); n += 1
    _rewrite(POSITIONS, open_pos)
    return n


def _resolve_pending(client: Any, equity: float, now_iso: str, now_ms: int) -> int:
    """Fill / cancel limit orders (owner: 'set a pending order, don't FOMO in'). A
    limit fills when a CLOSED bar's range touches it (LONG: low<=limit; SHORT:
    high>=limit) -> opens via the normal open path at the limit price. It cancels if
    price runs >2% past the level (ran away) or after ~2h (expired)."""
    pend = _load(PENDING)
    if not pend:
        return 0
    open_syms = {p["symbol"] for p in _load(POSITIONS)}
    bar_ms = of._TF_MS[TF]
    EXPIRE_MS = 8 * bar_ms
    still, filled = [], 0
    for po in pend:
        sym, side, limit = po["symbol"], po["side"], float(po["entry_px"])
        if sym in open_syms:
            _append(LT_DIR / "pending_events.jsonl",   # drop, but never silently
                    {"symbol": sym, "side": side, "event": "dropped_position_exists", "ts": now_ms})
            continue
        try:
            fb = of.fetch_klines_with_flow(sym, TF, months=0.05, end_ms=now_ms, client=client, sleep_between=0.02)
            fut = [b for b in fb if int(b["ts_ms"]) > int(po["placed_ms"]) and int(b["ts_ms"]) + bar_ms <= now_ms]
        except Exception:
            still.append(po); continue
        fill_ts = None
        for b in fut:
            if (side == "LONG" and float(b["low"]) <= limit) or (side == "SHORT" and float(b["high"]) >= limit):
                fill_ts = int(b["ts_ms"]); break
        if fill_ts is not None:                       # FILLED -> open at the limit price
            d = {"symbol": sym, "action": side, "price": limit, "leverage": po["leverage"],
                 "size_pct": po["size_pct"], "sl_pct": po["sl_pct"], "tp_pct": po["tp_pct"], "entry_px": None,
                 "_smc": po.get("smc") or {}, "atr": po.get("atr", 0), "_quote_vol_24h": po.get("quote_vol_24h", 0),
                 "vol_ratio": po.get("vol"), "_maker": True,
                 "regime": po.get("regime"), "_chart_b64": po.get("chart_b64"),
                 "_ts": fill_ts - 1, "_fill_bar_ts": fill_ts,
                 "rationale": (po.get("rationale") or "") + " [limit filled]"}
            got = open_positions([d], equity, now_iso, now_ms=now_ms)
            if got:
                filled += 1
            else:
                _append(LT_DIR / "pending_events.jsonl",
                        {"symbol": sym, "side": side, "event": "fill_blocked", "ts": now_ms})
            continue
        expired = now_ms - int(po["placed_ms"]) > EXPIRE_MS
        px_now = float(fut[-1]["close"]) if fut else float(po.get("price0") or limit)
        # run-away must measure movement SINCE PLACEMENT — measuring from the limit
        # level insta-cancelled every deep (A+ flush) limit even with price flat.
        p0 = float(po.get("price0") or limit)
        ran = ((side == "LONG" and px_now > max(p0, limit) * 1.02)
               or (side == "SHORT" and px_now < min(p0, limit) * 0.98))
        if expired or ran:
            _append(LT_DIR / "pending_events.jsonl",   # NOT closed.jsonl (would pollute the feed/stats)
                    {"symbol": sym, "side": side, "event": "limit_cancelled",
                     "why": "expired" if expired else "ran_away", "ts": now_ms})
            continue
        still.append(po)
    _rewrite(PENDING, still)
    return filled


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
        # Breakeven + trailing stop (fixes 'trade went into profit then closed at a
        # full loss'): after a bar CLOSES having reached +1R, ratchet the stop to
        # breakeven+fees; past +2R, trail 1R behind the run's peak. No lookahead —
        # the stop only moves AFTER a bar is survived (the old stop is checked
        # first, pessimistically, so an intrabar dip to the old stop still counts).
        sl_orig = sl
        risk = abs(entry - sl_orig)
        peak = entry
        # TRUE breakeven must cover round-trip fees + the STOP slippage resolve()
        # itself charges by tier (audit: 12bps flat made every 'BE' exit a
        # guaranteed loss — micro tier slips 150bps). Skip the BE ratchet entirely
        # when the buffer eats most of 1R (placing a fake-BE stop is worse).
        _stop_slip = float(pcm.fill_bps(tier, is_stop=True)) / 10000.0
        BE_BUF = 2 * float(pcm.TAKER_FEE_RATE) + _stop_slip + 0.0002
        fb_ts = int(p.get("fill_bar_ts") or -1)
        is_mech = bool(p.get("mech"))
        hold_cap = int(p.get("max_hold") or MAX_HOLD_BARS)   # proven methods: 16 bars, as backtested
        for k, b in enumerate(fut):
            if int(b["ts_ms"]) == fb_ts:
                # the bar our limit filled on: intrabar sequence is unknown — a TP
                # touch may have happened BEFORE the fill, so only ADVERSE exits
                # (liq/sl) may fire here; TP is never booked off the fill bar.
                lo_, hi_ = float(b["low"]), float(b["high"])
                hit = None
                if side == "LONG":
                    if lo_ <= liq_px: hit = (liq_px, "liquidation")
                    elif lo_ <= sl: hit = (sl, "sl")
                else:
                    if hi_ >= liq_px: hit = (liq_px, "liquidation")
                    elif hi_ >= sl: hit = (sl, "sl")
            else:
                hit = lr.exit_check(b, side, liq_px, sl, tp)  # pessimistic: liq -> sl -> tp
            if hit is not None:
                exit_px, reason = hit
                # a stop that has ratcheted to/above breakeven is a managed exit
                if reason == "sl" and ((side == "LONG" and sl >= entry * (1 + BE_BUF))
                                        or (side == "SHORT" and sl <= entry * (1 - BE_BUF))):
                    reason = "trail"
                exit_ts = int(b["ts_ms"]); break
            if k + 1 >= hold_cap:
                exit_px, reason = float(b["close"]), "timeout"
                exit_ts = int(b["ts_ms"]); break
            if int(b["ts_ms"]) == fb_ts:
                # fill bar's HIGH/LOW may pre-date our fill — feeding it into the
                # trailing peak would arm a breakeven stop off a move we may never
                # have held through (optimistic leak, twin of the TP-off-fill-bar
                # bug). The ratchet starts from the NEXT bar.
                continue
            if is_mech:
                continue                       # proven methods run EXACTLY as backtested: no trailing
            if risk > 0:                       # ratchet AFTER surviving this bar
                if side == "LONG":
                    peak = max(peak, float(b["high"]))
                    mr = (peak - entry) / risk
                    if mr >= 1.0 and BE_BUF * entry < 0.9 * risk:
                        sl = max(sl, entry * (1 + BE_BUF))
                    if mr >= 2.0:
                        sl = max(sl, peak - risk)
                else:
                    peak = min(peak, float(b["low"]))
                    mr = (entry - peak) / risk
                    if mr >= 1.0 and BE_BUF * entry < 0.9 * risk:
                        sl = min(sl, entry * (1 - BE_BUF))
                    if mr >= 2.0:
                        sl = min(sl, peak + risk)
        if exit_px is None:
            still.append(p); continue
        # Exit slippage: stop-market gaps through the stop, timeout is a plain
        # market order, TP is a resting limit (fills at its price), liquidation
        # net is pinned to -margin by net_pnl so its fill is informational.
        if reason in ("sl", "trail"):
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
               "leverage": lev, "margin": round(margin, 4), "vol": p.get("vol"),
               "rationale": p.get("rationale"), "chart": p.get("chart"), "closed_ts": now_ms}
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
    closed = _dedupe_closed(_load(CLOSED))   # never let a double-booked trade inflate the edge
    card = ls.scorecard(closed, benchmark=_benchmark(client, closed))
    LT_DIR.mkdir(parents=True, exist_ok=True)
    SCORECARD.write_text(json.dumps(card, indent=1, default=str), encoding="utf-8")
    return card


def _hot_universe(client: Any, now_ms: int) -> list[str]:
    """Universe ranked by RECENT volume on the timeframe we trade (owner: 'danh may
    con vol 1h to thoi'). Anti-trash 24h floor ($50M) is the liquidity base — a 1h
    volume spike on a micro-cap is a pump-and-dump, exactly the falling knife we just
    got burned by — so we rank WITHIN the liquid pool by recent SCAN_TF money flow and
    keep the hottest N. Cached ~30 min to bound the per-coin klines calls. Paper only."""
    import json as _j
    try:
        if UNIVERSE_CACHE.exists():
            c = _j.loads(UNIVERSE_CACHE.read_text(encoding="utf-8"))
            if (c.get("scan_tf") == SCAN_TF and c.get("selected")
                    and (now_ms - int(c.get("ts_ms", 0))) < UNIVERSE_REFRESH_SEC * 1000):
                return list(c["selected"])
    except Exception:
        pass
    ticks = client.futures_ticker()
    pool = sorted(
        [(t["symbol"], float(t.get("quoteVolume", 0) or 0)) for t in ticks
         if t.get("symbol", "").endswith("USDT") and "_" not in t["symbol"]
         and float(t.get("quoteVolume", 0) or 0) >= UNIVERSE_MIN_QVOL],
        key=lambda x: -x[1])[:max(UNIVERSE_MAX, 300)]
    scored: list[tuple[str, float]] = []
    for sym, _qv in pool:
        try:
            kl = client.futures_klines(symbol=sym, interval=SCAN_TF, limit=SCAN_WINDOW_BARS)
            scored.append((sym, sum(float(k[7]) for k in kl)))   # k[7] = quote asset volume per bar
        except Exception:
            continue
    scored.sort(key=lambda x: -x[1])
    selected = [s for s, _v in scored[:UNIVERSE_HOT_TOP]]
    if not selected:                                              # klines all failed -> fall back to 24h order
        selected = [s for s, _ in pool[:UNIVERSE_HOT_TOP]]
    try:
        UNIVERSE_CACHE.write_text(_j.dumps({"scan_tf": SCAN_TF, "ts_ms": now_ms,
            "window_bars": SCAN_WINDOW_BARS, "n_pool": len(pool),
            "selected": selected}), encoding="utf-8")
    except Exception:
        pass
    return selected


def run_once() -> dict[str, Any]:
    import time as _t
    from timebase import utc_now
    from tradingagents.binance.client import spot_client
    client = spot_client()
    now_ms = int(_t.time() * 1000)
    resolved = resolve(client, now_ms)
    acct = load_account()
    equity = float(acct["equity"])
    pend_filled = _resolve_pending(client, equity, utc_now(), now_ms)   # fill/cancel limit orders
    if pend_filled:
        acct = load_account(); equity = float(acct["equity"])
    card = refresh_scorecard(client)
    _maybe_reflect(now_ms)   # meta-cognition: bot reasons about its own results ~every 30 min
    open_now = _load(POSITIONS)
    margin_used = sum(float(x.get("margin") or 0) for x in open_now)
    blocked, why = (lr.daily_breaker(_load(CLOSED), _day_anchor(acct, now_ms, equity), now_ms)
                    if DAILY_BREAKER_ON else (False, "off"))
    status = {
        "scorecard": {"n": card["metrics"]["n"], "win_rate": card["metrics"]["win_rate"],
                      "mean_r": card["metrics"]["mean_r"], "liq_count": card["metrics"]["liq_count"],
                      "verdict": card["verdict"]["code"]},
        "capacity": {"open": len(open_now), "max_concurrent": MAX_CONCURRENT,
                     "margin_used_pct": round(margin_used / equity * 100, 1) if equity > 0 else 100.0,
                     "margin_cap_pct": MAX_TOTAL_MARGIN_PCT,
                     "daily_breaker": ("BLOCKED: " + why) if blocked else "ok"},
    }
    # Universe = the hottest liquid coins by RECENT volume on the timeframe we trade
    # (owner: "danh may con vol 1h to thoi"). $50M/24h floor keeps out micro-cap
    # falling-knives; SCAN_TF volume ranks who is actually moving right now.
    try:
        selected = _hot_universe(client, now_ms)
        if not selected:
            raise ValueError("empty universe")
    except Exception:
        selected = us.select_universe(client, end_ms=now_ms, months=1.0, timeframe="1h",
                                      min_daily_quote_volume=UNIVERSE_MIN_QVOL,
                                      max_symbols=UNIVERSE_MAX)["selected"]
    ctx = build_context(client, selected, now_ms)
    if PROVEN_ONLY:
        # the bleed fix: no discretionary entries — only lab-proven survivors fire.
        decisions = _mechanical_decisions(ctx)
    else:
        decisions = decide(ctx, equity, status=status)
    opened = open_positions(decisions, equity, utc_now(), now_ms=now_ms)
    wr = round(acct["wins"] / acct["trades"], 3) if acct["trades"] else None
    return {"equity": acct["equity"], "trades": acct["trades"], "win_rate": wr,
            "opened": opened, "resolved": resolved, "open": len(_load(POSITIONS)),
            "considered": len(ctx), "acted": len(decisions), "model": MODEL,
            "mode": "PROVEN_ONLY" if PROVEN_ONLY else "DISCRETIONARY",
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
    if a.once:
        # --once must not claim the loop's PID file (audit: it confused the
        # supervisor's duplicate detection into killing/keeping the wrong one).
        print(json.dumps(run_once(), default=str))
    else:
        # single-instance lock: a second resident loop double-books closes (the
        # duplicate LINK trade) — refuse to start if another loop is fresh.
        lock = LT_DIR / "loop.lock"
        try:
            import time as _t
            if lock.exists() and (_t.time() - lock.stat().st_mtime) < 600:
                print(json.dumps({"error": "another llm_trader loop is active (loop.lock fresh)"}))
                raise SystemExit(1)
        except SystemExit:
            raise
        except Exception:
            pass
        PID_FILE.write_text(str(os.getpid()), encoding="ascii")
        while not STOP_FILE.exists():
            lock.write_text(str(os.getpid()), encoding="ascii")   # touch: proves liveness
            try: res = run_once()
            except Exception as exc: res = {"error": str(exc)[:200]}
            _hb(res)
            t = time.time() + a.interval_seconds
            while time.time() < t and not STOP_FILE.exists():
                time.sleep(1)
        _hb({}, status="stopped")
