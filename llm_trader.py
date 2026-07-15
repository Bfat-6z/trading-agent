"""LLM-driven discretionary PAPER trader with context-conditioned self-learning.

The owner's design (not the mechanical prove-or-kill harness): plug a strong LLM
in as the decision brain. Each cycle it reads FULL market context per symbol
(price action, regime, funding, CVD, time-of-day) PLUS its own past trade outcomes
tagged by context, and decides LONG/SHORT/SKIP. It learns from mistakes
CONTEXTUALLY — a loss on one coin/regime/time doesn't blanket-ban the setup; the
same idea can win on another coin at another time. Markets are non-stationary; the
LLM weighs context rather than a static verdict.

RULES (owner, updated 2026-07-13 — "vốn/đòn bẩy tùy model tự cân nhắc"):
- position size & leverage = the MODEL's decision, based on current equity
- code keeps RUIN floors only: size<=40% margin, lev<=25, gap-to-liq veto
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

MODEL = os.environ.get("LLM_TRADER_MODEL", "cx/gpt-5.6-sol")   # owner 2026-07-13: switch to 5.6-sol (Tradebot X's model); verified text+vision OK on 9router
BASE_URL = os.environ.get("LLM_TRADER_BASE", "http://localhost:20128/v1")
# Squeeze the model: no artificial token ceiling + max reasoning effort. The
# endpoint accepts reasoning_effort=high (verified: it engages deeper reasoning)
# and large max_tokens, so let it THINK as long as it needs. These are ceilings,
# not forced usage; a slow deep cycle just runs less often (loop is sequential).
MAX_DECISION_TOKENS = int(os.environ.get("LLM_TRADER_MAX_TOKENS", "16000"))
REASONING_EFFORT = os.environ.get("LLM_TRADER_REASONING_EFFORT", "high")
LLM_TIMEOUT = float(os.environ.get("LLM_TRADER_LLM_TIMEOUT", "300"))
TF = "15m"
START_EQUITY = 100.0
MAX_HOLD_BARS = 32
# OWNER RULES updated 2026-07-13 ("vốn thì tùy model tự cân nhắc dựa trên số vốn hiện có và đòn
# bẩy cũng thế"): sizing & leverage belong to the MODEL now. Code keeps RUIN floors only —
# size<=40% margin (isolated: worst gap-to-liq loses the margin, one trade can't wipe the account),
# lev<=25 (liq ≈ 100/lev%; the gap veto scales with the chosen lev). Clamps live in _validate_decisions.
# (The old 5-10%/x5-x10 band still applies to the MECHANICAL paths, which have no model in the loop.)
# entries need strong participation: vol_ratio >= this (his method: EMA+VOL+price;
# research: breakout on sub-1.5x volume = fakeout). A+ capitulation needs >=1.8 anyway.
MIN_ENTRY_VOL = float(os.environ.get("LLM_TRADER_MIN_ENTRY_VOL", "1.5"))
# PROVEN-ONLY (the fix for the bleed, owner 2026-07-04 'fix cai bat on do di'):
# 77 measured trades say LLM discretionary entries are -EV (-$0.14/trade); the one
# measured +EV play is the lab survivor set. In this mode the bot trades ONLY when
# a survivor's mechanical condition fires on live bars — evaluated by the SAME
# method_lab code that backtested it (zero mapping drift). The lab keeps testing
# new methods 24/7; new survivors auto-arm, dropped survivors auto-disarm.
# 2026-07-09 (owner: "dùng model mạnh, đừng bảo thủ cứng nhắc"): default to DISCRETIONARY so gpt-5.5
# vision actually trades. PROVEN_ONLY was the "bleed fix" (the LLM path lost before), so this is a
# guarded re-try: the gap-tail ruin veto now applies to LLM decisions too (_apply_gap_veto), x5/x10 +
# size caps + kill-switch + live-LOCKED all stay. Watch the scorecard; if it bleeds, KILL (or set
# LLM_TRADER_PROVEN_ONLY=1 to revert). Paper only.
PROVEN_ONLY = os.environ.get("LLM_TRADER_PROVEN_ONLY", "0") == "1"
# R2 (plans/redesign_tin_va_chart_v1.md): information+chart redesign. OFF by default — flipping it on
# requires (a) trigger thresholds tuned on trigger_log data, (b) Opus adversarial review of the flip.
# When ON: discretionary candidates are GATED to trigger-hit coins, and every actionable decision must
# survive a STAGE-2 second look (fresh focused chart of the chosen TF) before it executes.
# Delivery mechanism: llm_trader does NOT load .env, and supervisor children inherit the SUPERVISOR's
# environment — an env-only flag would need a full fleet restart to flip. So the flag is env OR a
# marker file: flip = create state/llm_trader/redesign.flag + respawn just the mission (kill its
# launcher pair; supervisor respawns). Rollback = delete the file + respawn. Read ONCE at startup.
REDESIGN = (os.environ.get("LLM_TRADER_REDESIGN", "0") == "1") or (LT_DIR / "redesign.flag").exists()
STAGE2_MAX = int(os.environ.get("LLM_TRADER_STAGE2_MAX", "4"))   # focused re-look calls per cycle; =max_charts
                                                                 # so no actionable decision skips the look (review #2)
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
# Binance lists TOKENIZED STOCKS / COMMODITIES / leveraged-ETFs as USDT futures (NVDAUSDT, QQQUSDT,
# PAXGUSDT, XAUUSDT, MSTRUSDT...). A 15m crypto scalper has no business there (RTH-only moves, gaps,
# near-zero ATR on pegged gold) — the shadow eval 2026-07-11 caught the bot trading them. The list is
# now the shared canonical set in universe_filter.py (one source of truth; MU/DRAM/CRCL added 07-11).
from universe_filter import NON_CRYPTO as UNIVERSE_EXCLUDE_BASES
# Owner (2026-07-05): "danh may con vol 1h to thoi ... neu danh chart 1h thi quet
# 1h, 15m thi quet 15m, 4h thi quet 4h". So the universe is ranked by RECENT money
# flow on the timeframe we actually trade — not stale 24h volume (a coin can be big
# on 24h yet dead right now). The $50M/24h floor stays as an anti-trash liquidity
# base; among those, keep the hottest by recent SCAN_TF volume.
SCAN_TF = os.environ.get("LLM_TRADER_SCAN_TF", "1h")               # timeframe whose recent volume ranks the universe
SCAN_WINDOW_BARS = int(os.environ.get("LLM_TRADER_SCAN_WINDOW", "24"))   # sum this many recent SCAN_TF bars = "hot money flow now"
UNIVERSE_HOT_TOP = int(os.environ.get("LLM_TRADER_HOT_TOP", "60"))       # keep top-N liquid coins by recent SCAN_TF volume
# Boss trades TidalFi MANUALLY off this bot's signals; the mission must always SEE the
# prop-listed coins or it can structurally never signal them (2026-07-15: 8 closes/24h,
# only 1 on-prop -> the Telegram group starved a whole day). Crypto subset of the 42
# TidalFi perps that exist on Binance futures — appended to every universe build.
PROP_ALWAYS_SCAN = frozenset({
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "ZECUSDT", "HYPEUSDT", "XRPUSDT", "DOGEUSDT",
    "BNBUSDT", "NEARUSDT", "1000PEPEUSDT", "ADAUSDT", "LINKUSDT", "UNIUSDT",
    "AVAXUSDT", "XLMUSDT", "TRXUSDT", "LTCUSDT", "DOTUSDT", "ASTERUSDT", "1000SHIBUSDT",
})


def _with_prop_syms(selected: list) -> list:
    have = set(selected)
    extra = [s for s in sorted(PROP_ALWAYS_SCAN) if s not in have]
    try:                                   # + the 22 TidalFi-only TradFi perps (NVDA/XAU...)
        import tidalfi_data as td          # — injected AFTER the NON_CRYPTO universe filter,
        extra += [s for s in sorted(td.tidalfi_only_symbols())   # never through it
                  if s not in have and s not in extra]
    except Exception:
        pass
    return list(selected) + extra
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
PER_TRADE_NOTIONAL_PCT = 100.0    # Codex CRITICAL-1: cap per-trade notional (size*lev) at 100% equity
AGG_NOTIONAL_CAP_PCT = 400.0      # Codex CRITICAL-1: cap aggregate notional across open+new at 4x equity
MAX_FLUSH_MECH_OPEN = 3           # Codex CRITICAL(flush): cap CONCURRENT mech-flush positions (per-EVENT bound)
# Owner (2026-07-05): "bo cai phanh ngay do di" — DISABLE the daily-loss breaker.
# Owner accepts the risk (paper account). Set to "1" to re-arm. NOTE: with this OFF
# there is NO daily circuit-breaker; a run of losing capitulation fires on a hard
# down-day can compound past -15% with nothing halting new entries. The per-position
# sizing (mech_sizing), $50M liquidity floor and 95% total-margin cap are the only
# remaining guards. Live orders stay LOCKED regardless.
# 2026-07-09 (Codex review of the unshackle): default ON. Now that the discretionary model trades
# freely (choppy/wick gates removed), a -15%/day circuit-breaker is the SURVIVAL backstop that stops a
# bad day cascading into a blow-up — it never caps a single trade, so it is not the per-trade rigidity
# the owner objected to. Env can still disable (LLM_TRADER_DAILY_BREAKER=0).
DAILY_BREAKER_ON = os.environ.get("LLM_TRADER_DAILY_BREAKER", "1") != "0"
# DATA-ACCUMULATION mode (owner): trade B+ setups to build a measured track record
# fast, instead of sitting idle waiting for the rare A+. Lower confluence gate +
# an explicit "act, don't over-skip" directive. Honest tradeoff: more trades on a
# not-yet-proven strategy = faster data AND faster bleed; the scorecard is the judge.
MIN_CONFLUENCE = int(os.environ.get("LLM_TRADER_MIN_CONFLUENCE", "3"))   # playbook v2 (2026-07-16): was 2, contradicted the doc's ">=3 from distinct families"
EXPLORE_MODE = os.environ.get("LLM_TRADER_EXPLORE", "1") == "1"
# (wick/rút-râu is now an ADVISORY the model judges from wick_intensity in its context — no hard gate.)


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
    # fail-CLOSED (bughunt 2026-07-08): a present-but-corrupt account must NOT silently reset to
    # START_EQUITY — resolve() would then book PnL onto $100 and PERMANENTLY erase accumulated
    # equity. RAISE so the cycle fails loud (a transient mid-write heals next cycle; persistent
    # corruption needs a human, not a silent wipe). Only a genuinely-absent file starts fresh.
    if ACCOUNT.exists():
        try:
            return json.loads(ACCOUNT.read_text())
        except Exception as e:
            raise RuntimeError(f"account file exists but is unreadable ({e}) — refusing to reset equity") from e
    return {"equity": START_EQUITY, "realized": 0.0, "trades": 0, "wins": 0}


def save_account(a: dict[str, Any]) -> None:
    ACCOUNT.parent.mkdir(parents=True, exist_ok=True)
    tmp = ACCOUNT.with_name(f".{ACCOUNT.name}.{os.getpid()}.tmp")   # PID-suffixed (re-audit): a fixed
    tmp.write_text(json.dumps(a, indent=1, default=str), encoding="utf-8")  # account.tmp would be torn
    os.replace(tmp, ACCOUNT)                    # by a concurrent --once (which bypasses loop.lock)


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
_NEWS_PATH = ROOT / "state" / "agent_memory" / "news_latest.json"   # news_observer output (R1 triggers)


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


# Codex blocker (fail-OPEN): when venue metadata is unavailable the dynamic set is
# empty and every TradFi symbol slipped BACK into mech auto-fire + got mis-routed to
# Binance. Static fallback = the 22 known TidalFi-only markets; gates take the UNION.
TIDALFI_TRADFI_STATIC = frozenset({
    "XAUUSDT", "XAGUSDT", "MUUSDT", "SPCXUSDT", "INTCUSDT", "NVDAUSDT", "AMDUSDT",
    "METAUSDT", "TSLAUSDT", "ORCLUSDT", "TSMUSDT", "MSFTUSDT", "GOOGLUSDT", "AAPLUSDT",
    "AVGOUSDT", "AMZNUSDT", "OPENAIUSDT", "JPMUSDT", "CSCOUSDT", "WMTUSDT",
    "BRKBUSDT", "VUSDT",
})

_TD_ADAPTER_ERR_LOGGED: set = set()
_TD_BAR_CACHE: dict = {}
_DAILY_CACHE: dict = {}     # (sym, utc_day) -> daily bars; 1 fetch/coin/day (range-location + 1d chart)


def _fetch_bars_any(sym: str, tf: str, months: float, end_ms: int, client: Any,
                    **kw) -> list[dict]:
    """Venue-aware bar fetch: Binance for crypto, the TidalFi UDF adapter for the 22
    TradFi-only perps. CRITICAL for resolve/_resolve_pending — without this a TradFi
    position could OPEN but never CLOSE (the exact lane_farm zombie disease).
    Bar-boundary cache (review): the 90s cycle refetched identical 300-bar payloads
    ~10x per 15m bar (+12s/cycle median; 176s venue-degraded worst case) — now ONE
    fetch per (symbol, tf, bar boundary), the rest served from memory."""
    try:
        import tidalfi_data as td
        if sym in (TIDALFI_TRADFI_STATIC | set(td.tidalfi_only_symbols() or ())):
            _lim = min(1000, max(60, int(months * 30 * 86_400_000 / of._TF_MS[tf])))
            _ck = (sym, tf, int(end_ms) // of._TF_MS[tf], _lim)
            hit = _TD_BAR_CACHE.get(_ck)
            if hit is not None:
                return list(hit)
            bars = td.fetch_klines(sym, tf, limit=_lim, end_ms=end_ms)
            if bars:
                if len(_TD_BAR_CACHE) > 256:
                    _TD_BAR_CACHE.clear()          # tiny bound; repopulates in one cycle
                _TD_BAR_CACHE[_ck] = list(bars)
            return bars
    except Exception as _e:
        # review FIX-FIRST #3: this except used to be SILENT — an import/meta failure sent
        # a TidalFi symbol to Binance ("Invalid symbol") and resolve parked the position
        # FOREVER with zero trace. Log once per symbol per process.
        if sym not in _TD_ADAPTER_ERR_LOGGED:
            _TD_ADAPTER_ERR_LOGGED.add(sym)
            _append(LT_DIR / "governance.jsonl",
                    {"event": "tidalfi_adapter_error", "symbol": sym, "error": repr(_e)[:120]})
        if sym in TIDALFI_TRADFI_STATIC:
            return []      # fail-CLOSED: NEVER route a TradFi symbol to Binance
                           # ("Invalid symbol" -> silent park); [] parks WITH the log above
    return of.fetch_klines_with_flow(sym, tf, months=months, end_ms=end_ms, client=client, **kw)


def build_context(client: Any, symbols: list[str], now_ms: int) -> list[dict[str, Any]]:
    import backtest_data_fetcher as bf
    out = []
    whale = _whale_flow_map()   # per-symbol Telegram whale/liquidation pressure
    try:
        import tidalfi_data as td
        _tf_only = td.tidalfi_only_symbols()   # the 22 TradFi perps (NVDA/TSLA/XAU...) the
    except Exception:                          # boss can trade on TidalFi but Binance lacks
        td, _tf_only = None, set()
    for sym in symbols:
        try:
            # with_deriv=False on the live hot path (ck:debug 2026-07-08 root cause): the OI/LS
            # deriv fetch to fapi.binance.com/futures/data can HANG in the SSL handshake (requests
            # timeout doesn't cover it on Windows) — it froze run_once() for >70s, no heartbeat,
            # mission looked dead. Mission doesn't need deriv: no OI method is armed, and the
            # gap-veto's gap_risk_pct is computed from OHLC, not deriv. Re-enable only when an OI
            # method promotes AND fetch_deriv_series is proven hang-proof.
            if td is not None and sym in _tf_only:
                # TidalFi-only TradFi perp: bars from the venue's own UDF feed (adapter
                # returns the exact orderflow_data bar contract; 11/11 tests incl. the
                # fail-closed enrich join). Session gate: equities print synthetic 24/7
                # bars — skip when stale/flat (weekend, market closed) instead of feeding
                # the model dead candles (the stock-perp lane-loss lesson).
                fb = td.fetch_klines(sym, TF, limit=300, end_ms=now_ms)
                _sm = td.session_meta(sym, fb, TF, now_ms)
                if (not _sm.get("fresh") or (_sm.get("flat_bar_frac") or 0) > 0.3
                        or (_sm.get("longest_gap_bars") or 0) > 2):
                    continue
                fund = []                          # no funding on TidalFi TradFi perps ->
                                                   # enrich emits funding_rate=0.0 (verified)
            else:
                fb = of.fetch_klines_with_flow(sym, TF, months=0.12, end_ms=now_ms, client=client, sleep_between=0.02, with_deriv=False)
                fund = of.fetch_funding_series(sym, months=0.12, end_ms=now_ms, client=client)
            # CLOSED bars only (plan #13, VTL time-gating): drop the still-forming
            # candle so every decision input is immutable — its high/low/close and
            # derived indicators would otherwise repaint within the bar.
            bar_ms = of._TF_MS[TF]
            fb = [b for b in fb if int(b["ts_ms"]) + bar_ms <= now_ms]
            if len(fb) < 40:
                continue
            ind = cs.compute_indicators(fb)
            enr = of.enrich_indicator_df(ind, fb, fund)
            i = len(enr) - 1
            closes = [round(float(x), 4) for x in enr["close"].iloc[-8:].tolist()]
            reg = _regime(enr)
            feats = _features(enr, fb)
            # gap_risk_pct: worst single-bar range in the last 48 bars (matches method_lab's feature).
            # The gap-veto reads this on BOTH paths — without it on the discretionary ctx the LLM
            # gap-veto fail-closes and blocks every entry (the real "0 trades" trap).
            _gr_rngs = [(float(b["high"]) - float(b["low"])) / float(b["close"])
                        for b in fb[-48:] if float(b.get("close") or 0) > 0]
            gap_risk_pct = round(max(_gr_rngs) * 100, 3) if _gr_rngs else None
            # wick_intensity (owner 2026-07-09 "rút râu"): fraction of the last 12 bars DOMINATED by wick
            # (tail >=60% of the bar range, small body) = stop-hunt / rejection chop where 15m noise
            # clips stops — the measured 75%-SL death zone. High value => only high-conviction trades.
            _w12 = fb[-12:]; _wd = 0
            for _b in _w12:
                _h = float(_b["high"]); _l = float(_b["low"]); _c = float(_b["close"]); _o = float(_b.get("open") or _c)
                _rng = _h - _l
                if _rng > 0 and (_rng - abs(_c - _o)) / _rng >= 0.6:
                    _wd += 1
            wick_intensity = round(_wd / len(_w12), 2) if _w12 else None
            out.append({
                "symbol": sym, "price": round(float(enr["close"].iloc[i]), 4),
                "last8_closes": closes, "gap_risk_pct": gap_risk_pct, "wick_intensity": wick_intensity, **reg, **feats,
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


# REMOVED (second-brain P1, 2026-07-06): the _reflect()/_reflection_block() loop let
# the LLM free-write "directives" to self_reflection.json with NO evidence gate and
# then re-read them as authority ("follow your own conclusions") — a textbook
# memory-laundering loop (MemoryGraft arxiv.org/abs/2512.16962; the codebase's own
# data_trust.py classifies llm_generated text as non-promotable, and this loop
# bypassed it). The deterministic replacement already exists: _mistakes_block()
# (P&L-derived failure modes) + _proven_methods_block() (walk-forward evidence).


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


_DEF_MISSING_LOGGED: set = set()
_BRAIN_DEF_CACHE: dict = {}


def _method_def_from_brain(mid: str) -> dict | None:
    """Resurrect a method def from brain.db trials.dsl_canonical (cached — no sqlite
    hit per 90s cycle). Same fallback lane_promotion._def_for uses; without it an
    armed method whose def rotated out of methods_pool is silently never fired."""
    if mid in _BRAIN_DEF_CACHE:
        return _BRAIN_DEF_CACHE[mid]
    d = None
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{ROOT / 'state' / 'memory' / 'brain.db'}?mode=ro",
                              uri=True, timeout=10)   # ro: a READ path must never create
        try:                                          # a stub db file (audit#3 LOW)
            row = con.execute("SELECT dsl_canonical FROM trials WHERE method_id=? AND "
                              "dsl_canonical IS NOT NULL ORDER BY created_at DESC LIMIT 1",
                              (mid,)).fetchone()
        finally:
            con.close()
        if row and row[0]:
            cand = json.loads(row[0])
            if cand.get("when") or cand.get("conds"):
                d = cand
    except Exception:
        d = None
    _BRAIN_DEF_CACHE[mid] = d
    return d


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
            if not d and (s.get("when") or s.get("conds")):
                d = s        # armed row carries its def INLINE (lane_promoted resurrections
                             # outlive methods_pool rotation — Opus gate-v2 C1: joining back
                             # to the pool by id silently disarmed exactly those winners)
            if not d:
                d = _method_def_from_brain(s["id"])   # audit#2 F1: pool rotation had ALREADY
                                                      # silently disarmed hand-armed
                                                      # wr_flush_notknife (lockbox p=0.0002);
                                                      # brain.db dsl_canonical resurrects it
            if not d:
                if s["id"] not in _DEF_MISSING_LOGGED:   # loud, once per process (was silent)
                    _DEF_MISSING_LOGGED.add(s["id"])
                    _append(LT_DIR / "governance.jsonl",
                            {"event": "method_def_missing", "id": s["id"],
                             "ts_ms": int(time.time() * 1000)})
                continue
            m = {**d}
            if s.get("source"):
                m["source"] = s["source"]    # lane_promoted tag rides along for mode gating
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


# Reject a proven fire when liquidation is closer than this many ATRs (ruin control;
# see the gate below). 3.0 -> at x10 (~10% to liq) any coin with atr_pct > ~3.3% is
# refused, the volatility band where a single flush gaps the stop straight to liq.
GAP_LIQ_ATR_MULT = float(os.environ.get("MECH_GAP_LIQ_ATR_MULT", "3.0"))
# gap-tail veto (2026-07-08): close-to-close ATR let 2 mission trades gap PAST the stop to
# liquidation (−$15). gap_risk_pct = worst single-bar range in the last 48 bars — if that
# alone × this mult already reaches the liquidation distance, one more such bar can liquidate
# us, so refuse regardless of how calm the AVERAGE (atr) looks. 1.5 → veto at gap_risk > ~6.7%/x10.
GAP_RISK_MULT = float(os.environ.get("MECH_GAP_RISK_MULT", "1.5"))
# The mechanical path trades a single fixed leverage (owner: max conviction, x10).
# Bind the gate's liquidation distance AND the sizer to the SAME value so they can
# never drift apart — a hardcoded 10 in one place and a x5 sizer would wrong-sign
# the gate (Codex review point a). ~100/lev % is the naive liq distance at `lev`.
MECH_LEV = 10 if int(os.environ.get("MECH_LEV", "10") or 10) >= 10 else 5   # Codex: hard-clamp to {5,10} (env can't make mech x25)


def _mechanical_decisions(context: list[dict[str, Any]],
                          methods: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """PROVEN-ONLY decide: fire a trade ONLY where a lab-survivor method's DSL
    condition holds on the coin's live CLOSED bars — evaluated with method_lab's
    own feature_frame/method_fires (the exact code that proved it). Execution is
    faithful to what was backtested: the method's own sl/tp %, x10 (owner wants
    size at max conviction), no structure override, no trailing, 16-bar timeout.
    `methods` overrides the armed set (REDESIGN fires only lane_promoted rows)."""
    import method_lab as ml
    import mech_sizing as msz
    methods = _survivor_methods() if methods is None else methods
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
                # GAP-RISK GATE (ruin control, 2026-07-06 HMSTR liquidation -$14.33 =
                # the entire day's loss). Sizing's Kelly is fit on backtest net% that
                # ASSUME the SL fills at its nominal % — it never saw the gap-through-
                # to-liquidation tail. On a high-ATR coin, liquidation (~100/lev % away)
                # is only a couple of noise-bars away and a single flush candle blows
                # past the 1% stop straight to liq. Refuse the fire when liquidation is
                # closer than GAP_LIQ_ATR_MULT ATRs. atr_pct is a feature_frame column.
                _atr = row.get("atr_pct")
                _gaprisk = row.get("gap_risk_pct")
                _liq_dist = 100.0 / max(1, MECH_LEV)          # x10 -> ~10% to liquidation
                # fail-CLOSED (Codex): missing/degenerate atr means we can't size the
                # liquidation risk -> refuse, don't fire blind. Same rule the lanes use.
                # PLUS gap-tail veto: the worst recent single bar (gap_risk) reaching the
                # liq distance = ruin risk ATR's average hides (2026-07-08 sizing fix).
                # gap_risk fail-CLOSED too (Codex #4): a risk gate must refuse when it can't
                # assess the risk. `_x != _x` is a no-import NaN test. Both risk metrics block.
                if (_atr is None or _atr != _atr or float(_atr) <= 0 or float(_atr) * GAP_LIQ_ATR_MULT > _liq_dist
                        or _gaprisk is None or _gaprisk != _gaprisk
                        or float(_gaprisk) * GAP_RISK_MULT > _liq_dist):
                    _append(LT_DIR / "governance.jsonl",
                            {"event": "gate_block_gap_risk", "symbol": c["symbol"],
                             "method": m["id"], "atr_pct": _atr, "gap_risk_pct": _gaprisk,
                             "liq_dist_pct": _liq_dist, "mult": GAP_LIQ_ATR_MULT})
                    continue
                # LESSON GATE (second brain P4, Codex ship-gate): tiered.
                # 'active' (eff_n>=12 clusters + mission cohort negative) = HARD
                # veto; 'advisory' (pooled-negative only) = logged, NOT blocked —
                # so evidence keeps accumulating and a noise-mined rule can't
                # starve the already-rare fires. Rows logged verbatim.
                blocked = False
                try:
                    import brain
                    for les in brain.lesson_hits(row, m.get("side", "LONG"), m["id"]):
                        _append(LT_DIR / "governance.jsonl",
                                {"event": "lesson_block" if les["status"] == "active" else "lesson_advisory",
                                 "symbol": c["symbol"], "method": m["id"],
                                 "lesson": les.get("lesson_id"), "n": les.get("n"),
                                 "eff_n": les.get("eff_n"), "mission_n": les.get("mission_n"),
                                 "avg_r": les.get("avg_r"), "label": les.get("label")})
                        if les["status"] == "active":
                            blocked = True
                except Exception as _lge:
                    # fail-OPEN by design (a gate bug must not kill the bot) but never
                    # SILENTLY (Codex file-review #3): an active veto that stops
                    # applying is a risk change the owner must be able to see.
                    blocked = False
                    _append(LT_DIR / "governance.jsonl",
                            {"event": "lesson_gate_error", "symbol": c["symbol"],
                             "method": m["id"], "error": repr(_lge)[:160]})
                if blocked:
                    continue
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
    sized = {(o["coin"], o["method"]): o for o in msz.size_fires(fires, dists, lev=MECH_LEV)}
    _lane_src = {m["id"]: m.get("source") for m in methods}
    out = []
    for (sym, mid) in fires:
        o = sized.get((sym, mid))
        if not o and _lane_src.get(mid) == "lane_promoted" and mid not in dists:
            # audit#2 F2: a brain.db-resurrected lane method has NO survivor_distribution
            # by construction (only full_scale_validation writes that file) -> Kelly sizer
            # skips it forever and the funnel is inert at the LAST hop. Fixed conservative
            # size instead: 5% margin (half PER_POS_CAP), same ruin bounds as every path.
            o = {"coin": sym, "method": mid, "margin_pct": 5.0}
            _append(LT_DIR / "governance.jsonl",
                    {"event": "lane_promoted_fixed_size", "symbol": sym, "method": mid,
                     "margin_pct": 5.0})
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
        try:
            from brain import LESSON_FEATS
            _feats = {k: row.get(k) for k in LESSON_FEATS if row.get(k) is not None}
        except Exception:
            _feats = None
        out.append({**c, "action": m.get("side", "LONG"), "leverage": MECH_LEV,
                    "size_pct": o["margin_pct"], "sl_pct": _slp, "tp_pct": _tpp, "entry_px": None,
                    "_mech": True, "_max_hold": int(m.get("timeout") or 16), "_chart_b64": chart,
                    "_mech_method": m["id"], "_entry_feats": _feats,
                    "rationale": f"PROVEN {mid} (win {m.get('oos_win_rate')}, sized {o['margin_pct']}% margin, "
                                 f"cluster={len(fires)}): {m.get('desc','')[:60]}"})
        _append(LT_DIR / "governance.jsonl",
                {"event": "proven_fire", "symbol": sym, "method": mid, "margin_pct": o["margin_pct"],
                 "cluster_size": len(fires), "rsi": row.get("rsi14"), "vol": row.get("vol_ratio")})
    return out


FLUSH_DISARM_MIN_N = 15        # need this many mission flush closes before a disarm can trigger
FLUSH_DISARM_FLOOR = -0.10     # mean r-on-margin below this over the window = edge decayed on LIVE money
FLUSH_REPROBE_MS = 12 * 3600 * 1000   # after a disarm, re-arm for a fresh probe every 12h
FLUSH_DISARM_FILE = LT_DIR / "flush_disarm.json"   # persisted latch state (Opus review: log-once + latch)


def _flush_armed(now_ms: int) -> bool:
    """FLUSH EDGE-DECAY MONITOR (gap #6): the mech flush path auto-fires x10 on the FROZEN shadow
    'CONFIRMED' verdict — it must not run forever if the edge decays on the mission's own money.
    Once there are >=15 flush closes with a finite r, if the rolling mean r-on-margin turns clearly
    negative (<=-0.10) the path is DISARMED (a LATCH — no new closes accrue while disarmed, so it
    can't self-recover from its own frozen window; Opus review). It re-arms via a TIME re-probe: 12h
    after a disarm the path fires again so fresh closes can accrue; if it re-decays it disarms again
    (the 12h cooldown = hysteresis, no cycle flapping). State persists in flush_disarm.json and is
    logged ONLY on a transition (not every cycle). Fail-OPEN on error (keep armed, logged)."""
    import math
    try:
        rows = [r for r in _dedupe_closed(_load(CLOSED))
                if str(r.get("mech_method") or "") in ("flush_no_oi_mech", "flush_oi_dn_mech")][-40:]
        vals = []                        # Codex: a single non-numeric r ("bad") raised -> outer catch
        for r in rows:                    # -> fail-OPEN kept a decayed edge firing. Parse per row.
            v = r.get("r")
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue                  # missing / non-numeric -> skip, don't poison the mean
            if math.isfinite(fv):
                vals.append(fv)
        try:
            st = json.loads(FLUSH_DISARM_FILE.read_text(encoding="utf-8"))
        except Exception:
            st = {"disarmed": False, "since_ms": 0}

        def _flip(disarmed: bool, event: str, extra: dict) -> None:
            FLUSH_DISARM_FILE.write_text(json.dumps({"disarmed": disarmed, "since_ms": now_ms}),
                                         encoding="utf-8")
            _append(LT_DIR / "governance.jsonl", {"ts_ms": now_ms, "event": event, **extra})

        if st.get("disarmed"):
            if now_ms - int(st.get("since_ms") or 0) >= FLUSH_REPROBE_MS:   # cooldown over -> re-probe
                _flip(False, "flush_mech_REARM_probe", {})
                return True
            return False                                                    # still latched
        # armed: check for decay
        if len(vals) >= FLUSH_DISARM_MIN_N:
            mr = sum(vals) / len(vals)
            if mr <= FLUSH_DISARM_FLOOR:
                _flip(True, "flush_mech_DISARMED", {"mean_r": round(mr, 3), "n": len(vals)})
                return False
        return True
    except Exception as _fae:
        _append(LT_DIR / "governance.jsonl",
                {"ts_ms": now_ms, "event": "flush_armed_error", "error": repr(_fae)[:120]})
        return True


def _non_tidalfi_ctx(ctx: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mech auto-fire is validated on CRYPTO only (review FIX-FIRST #2): the flush path's
    OI probe returns None for TidalFi symbols, which classifies EVERY equity flush as
    'confirmed no-OI' BY CONSTRUCTION — x10 auto-fires on earnings knives, mirrored to the
    boss's real prop account. TradFi symbols stay on the model+stage-2 discretionary path
    (the stated Phase-2 goal) until per-venue expectancy exists."""
    try:
        import tidalfi_data as td
        _tf = TIDALFI_TRADFI_STATIC | set(td.tidalfi_only_symbols() or ())
    except Exception:
        _tf = TIDALFI_TRADFI_STATIC    # Codex blocker: fail-CLOSED — a metadata outage must
    return [c for c in ctx if c.get("symbol") not in _tf]   # never re-arm mech on TradFi


def _flush_mech_decisions(ctx: list[dict[str, Any]], trig_map: dict[str, Any],
                          already: list[dict[str, Any]], now_ms: int) -> list[dict[str, Any]]:
    """MECHANICAL executor for the measured-positive flush paths (owner 2026-07-13: "m sợ quá
    nhiều thứ" — stop double-gating a CONFIRMED signal). Shadow live-forward: flush_no_oi
    CONFIRMED positive (n>=30), flush_oi_dn positive accruing. The model+stage-2 pipeline was
    capturing ~25% of those fires; this path takes them ALL, deterministically — the same
    bracket as the flush_bounce_exec lane (sl 2.5% / tp 4% / 24 bars ≈ the shadow ATR bracket)
    so mission-vs-lane stays directly comparable. No model, no stage-2 (the signal is code-
    confirmed; a second LLM opinion on it was fear, not discipline). SAFETY KEPT: caller runs
    _apply_gap_veto (ruin control), LAW sizing (x10, 5-10% margin), 1-per-symbol in
    open_positions, live LOCKED. One trade per flush EPISODE (2h window, mirrors the shadow
    dedup) so a trigger that stays true across 90s cycles can't churn re-entries."""
    if not _flush_armed(now_ms):      # edge-decay auto-disarm (gap #6): stop auto-firing if the
        return []                      # mission's own flush closes turned clearly -EV
    hits = []
    for sym, h in (trig_map or {}).items():
        paths = set((h or {}).get("paths") or [])
        if "flush_no_oi" in paths or "flush_oi_dn" in paths:
            hits.append((sym, "flush_no_oi" if "flush_no_oi" in paths else "flush_oi_dn"))
    if not hits:
        return []
    by_sym = {c.get("symbol"): c for c in ctx}
    taken = {d.get("symbol") for d in (already or [])}
    _open_rows = _load(POSITIONS)
    open_syms = ({p.get("symbol") for p in _open_rows}
                 | {p.get("symbol") for p in _load(PENDING)})
    # PER-EVENT cap (Codex CRITICAL): the 3/cycle cap did NOT bound one flush event — a trigger
    # persisting across 90s cycles opened 3+3+3... to the margin cap. Cap CONCURRENT mech-flush
    # positions instead: once MAX_FLUSH_MECH_OPEN are live, no more fire until they resolve. Also
    # count the model's OWN flush-trigger picks this cycle against the budget (correlated exposure).
    n_flush_open = sum(1 for p in _open_rows if str(p.get("mech_method") or "").startswith("flush"))
    n_flush_model = sum(1 for d in (already or [])
                        if any(pp in ("flush_no_oi", "flush_oi_dn") for pp in (d.get("_trigger_paths") or [])))
    flush_budget = max(0, MAX_FLUSH_MECH_OPEN - n_flush_open - n_flush_model)
    # episode dedup: any flush-mech trade on this symbol entered/closed in the last 2h = same flush
    recent = set()
    try:
        for r in _load(CLOSED)[-150:]:
            if (r.get("mech_method") or "").startswith("flush") and \
               now_ms - int(r.get("closed_ts") or 0) < 2 * 3600 * 1000:
                recent.add(r.get("symbol"))
    except Exception:
        pass
    out = []
    # correlated-cluster cap (Opus review A): a market-wide capitulation fires MANY coins at
    # once — all LONG, maximally correlated; 9×10%×x10 into a flash-crash approaches wipeout
    # (the one fear that's earned: ruin). Cap 3 new fires/cycle, CONFIRMED path first; a real
    # multi-coin flush still builds across cycles, just not in one all-in candle.
    hits.sort(key=lambda sp: 0 if sp[1] == "flush_no_oi" else 1)
    for sym, path in hits:
        if len(out) >= flush_budget:      # concurrent cap: total live flush-mech <= MAX_FLUSH_MECH_OPEN
            _append(LT_DIR / "governance.jsonl",
                    {"ts_ms": now_ms, "event": "flush_mech_concurrent_cap", "skipped": sym,
                     "open": n_flush_open, "model": n_flush_model, "budget": flush_budget})
            continue
        c = by_sym.get(sym)
        if not c or sym in taken or sym in open_syms or sym in recent:
            continue
        feats = None
        try:
            import method_lab as ml
            from brain import LESSON_FEATS
            row = ml.feature_frame(c.get("_bars") or [], funding=c.get("_funding"))[-1]
            feats = {k: row.get(k) for k in LESSON_FEATS if row.get(k) is not None}
        except Exception:
            pass
        size = 10.0 if path == "flush_no_oi" else 5.0     # LAW band 5-10%: full size on the CONFIRMED path
        out.append({**c, "action": "LONG", "leverage": MECH_LEV, "size_pct": size,
                    "sl_pct": 2.5, "tp_pct": 4.0, "entry_px": None,
                    "_mech": True, "_max_hold": 24, "_mech_method": f"{path}_mech",
                    "_entry_feats": feats,
                    "rationale": f"MECH FLUSH {path} (shadow-confirmed capitulation bounce; "
                                 f"lane-parity bracket 2.5/4.0/24)"})
        _append(LT_DIR / "governance.jsonl",
                {"ts_ms": now_ms, "event": "flush_mech_fire", "symbol": sym, "path": path,
                 "size_pct": size})
    return out


def _mistakes_block() -> str:
    """Surface the measured failure-mode lessons PROMINENTLY in the system prompt
    (not buried in the memory JSON) so the model actually corrects them — the
    'learn from your own mistakes' channel. Computed from realized P&L each cycle.

    Re-wired 2026-07-15 (loop-forensic: the LLM had NO mistake feedback at all) with
    fixes for the P1-2026-07-09 degeneracy that got it removed, hardened per the
    Codex FIX-FIRST review (era-fallback contamination + blanket-pair suppression):
      1. DISCRETIONARY ONLY: mech-fired rows never teach the LLM (they fire without
         it — their outcomes say nothing about LLM decision quality), in BOTH the
         era window and the fallback.
      2. ERA-WINDOWED: rows stamped with the CURRENT model once >=8 exist; until
         then the last-30 discretionary window, honestly LABELLED as the previous
         model's record (system-level cautions, not 'your' sins).
      3. CAP 2 + max ONE blanket line: structural leaks (noise-stop, inverted R:R)
         outrank blanket bans; STAND-ASIDE / AVOID-side / OVER-TRADING compete for
         a single slot — the all-four self-cancelling storm cannot re-form."""
    try:
        rows = _dedupe_closed(_load(CLOSED))
        disc = [r for r in rows if not r.get("mech_method")]
        era = [r for r in disc if r.get("model") == MODEL]
        prior_era = len(era) < 8
        ms = ltm.mistake_lessons(disc[-30:] if prior_era else era[-30:])   # recent window either way
                                                                           # (unbounded era re-converges
                                                                           #  to stale lifetime scolding)
    except Exception:
        ms = []
    if not ms:
        return ""
    def _rank(line: str) -> int:                      # actionable first, blanket last
        for i, pfx in enumerate(("THESIS WRONG", "NOISE-STOPPED", "WEAK R:R", "STAND ASIDE",
                                 "AVOID", "OVER-TRADING")):    # prefixes: llm_trader_memory lockstep
            if line.startswith(pfx):
                return i
        return 9
    _BLANKET = ("STAND ASIDE", "AVOID", "OVER-TRADING")
    picked: list[str] = []
    for m in sorted(ms, key=_rank):                   # <=2 lines, <=1 blanket-suppression
        if m.startswith(_BLANKET) and any(p.startswith(_BLANKET) for p in picked):
            continue
        picked.append(m)
        if len(picked) == 2:
            break
    lines = "\n".join(f"- {m}" for m in picked)
    hdr = ("=== YOUR MEASURED MISTAKES (from your OWN recent losing trades — actively correct "
           "these; do NOT repeat them) ===\n")
    if prior_era:
        hdr = ("=== MEASURED MISTAKES IN THIS SYSTEM'S RECENT DISCRETIONARY TRADES (mostly the "
               "PREVIOUS model's record — your own is still accumulating; treat as system-level "
               "cautions, not your personal stats) ===\n")
    return hdr + lines + "\n=== END MISTAKES ===\n\n"


def memory_context() -> dict[str, Any]:
    """Distilled learning context injected into decide()'s prompt each cycle.

    Aggregates ALL closed trades (not the last-8 raw rows) into grouped stats,
    data-phrased lessons and rationale-vs-outcome recents via the pure
    llm_trader_memory module — plan 260702 checklist #10/#11. Reads CLOSED
    (canonical append-only log written by resolve); llm_trader_memory
    guarantees malformed rows are skipped, so this can't kill the loop.
    model=MODEL era-windows the stats (P1 #11) — same policy as
    _mistakes_block, so 5.6-sol isn't taught with 5.5's record."""
    ctx = ltm.build_memory_context(_dedupe_closed(_load(CLOSED)), model=MODEL)
    try:
        # the calibration report was BUILT (P1 2026-07-10) as the honest feedback channel
        # replacing the degenerate mistakes block — and then never wired into any prompt
        # (loop-forensic). The model now sees its measured noise-vs-thesis split + hint.
        import llm_trader_learning as ltl
        _rows = _dedupe_closed(_load(CLOSED))
        _disc = [r for r in _rows if isinstance(r, dict) and not r.get("mech_method")]
        _era = [r for r in _disc if r.get("model") == MODEL
                and r.get("thesis_wrong") is not None]
        # Codex review: calibration must follow the SAME era policy the memory block
        # claims — own-era once >=8 instrumented rows exist, else the recent mixed
        # window HONESTLY LABELLED as prior-era system history.
        _own = len(_era) >= 8
        cal = ltl.calibration_report(_era if _own else _disc[-40:], window=40)
        ctx["calibration"] = {k: cal.get(k) for k in
                              ("n", "win_rate", "mean_actual_R", "noise_stop_rate",
                               "thesis_wrong_rate", "over_optimism_R", "verdict_hint")}
        ctx["calibration"]["scope"] = ("your OWN record (current model)" if _own else
                                       "mostly PREVIOUS model era — system history, not your record")
    except Exception:
        pass
    return ctx


# ---------------------------------------------------------------------------
# LLM decision (9router, OpenAI-compatible)
# ---------------------------------------------------------------------------
def _llm(system: str, user: str, max_tokens: int | None = None,
         effort: str | None = None) -> str | None:
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
                       "reasoning_effort": effort or REASONING_EFFORT,
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
            if action in ("LONG", "SHORT") and sym:
                # funnel audit SILENT-DROP (a): the prompt shows a 20-coin market_overview,
                # so the model CAN pick a coin outside its charted shortlist — that decision
                # vanished with no trace. Still dropped (no ctx = no price/bars to book
                # against), but now it's VISIBLE, and its frequency tells us whether the
                # overview->shortlist gap is starving real intent.
                _append(LT_DIR / "governance.jsonl",
                        {"ts_ms": int(__import__("time").time()*1000), "event": "llm_pick_outside_shortlist", "symbol": sym, "action": action})
            continue
        try:                                                              # bughunt LLM#2 + Codex#3: a
            import math as _math                                          # malformed LLM field
            _sz = float(dec.get("size_pct", 5) or 5)                      # ("10x"/NaN/Inf/[..]) must skip
            _sl = float(dec.get("sl_pct", 2) or 2)                        # only THIS decision, not crash
            _tp = float(dec.get("tp_pct", 3) or 3)                        # the batch — AND NaN/Inf must not
            if not (_math.isfinite(_sz) and _math.isfinite(_sl) and _math.isfinite(_tp)):
                raise ValueError("non-finite numeric")                    # slip the clamp as a boundary value
            # Owner 2026-07-13: "vốn thì tùy model tự cân nhắc dựa trên số vốn hiện có, đòn bẩy cũng
            # thế" — sizing & leverage are the MODEL's call now. Code keeps RUIN floors only:
            # lev<=25 (liq ≈ 100/lev%; the gap-veto scales with the chosen lev), size<=40% margin
            # (isolated: worst gap-to-liq loses the margin -> one trade can never wipe the account).
            lev = max(1, min(25, int(dec.get("leverage", 10) or 10)))
            # PER-TRADE NOTIONAL CAP (Codex review CRITICAL-1): the model owns size+lev, but
            # size*lev = notional must not blow the account. Cap notional at PER_TRADE_NOTIONAL_PCT
            # of equity, so size AUTOMATICALLY shrinks as leverage rises (x25 -> <=4% margin;
            # x5 -> <=20%). Replaces the removed x5/x10 band's implicit ceiling.
            size_pct = max(1.0, min(40.0, _sz))
            size_pct = min(size_pct, PER_TRADE_NOTIONAL_PCT / lev)
            sl_pct = max(0.3, min(8.0, _sl))
            tp_pct = max(0.3, min(15.0, _tp))
        except Exception:
            _append(LT_DIR / "governance.jsonl", {"event": "llm_decision_coercion_skip", "symbol": sym})
            continue
        # LIMIT entry FIRST: keep entry_px only if it's a FAVORABLE pullback limit
        # within ~5% (LONG below price / SHORT above); else treat as market.
        entry_px = None
        try:
            ep = float(dec.get("entry_px") or 0)
            px = float(ctx["price"])
            if ep > 0 and abs(ep / px - 1) <= 0.05 and (
                    (action == "LONG" and ep < px) or (action == "SHORT" and ep > px)):
                entry_px = round(ep, 8)
            elif ep > 0:
                # funnel audit SILENT-DROP (b): a limit >5% away / wrong side was silently
                # converted to a MARKET fill — doctrine inversion (the model wanted a much
                # better price; code filled it NOW). Behavior kept for now, but logged loud.
                _append(LT_DIR / "governance.jsonl",
                        {"ts_ms": int(__import__("time").time()*1000), "event": "llm_limit_coerced_to_market", "symbol": sym,
                         "entry_px": ep, "spot": px})
        except Exception:
            entry_px = None
        # FULL TRUST (owner 2026-07-09: "the brain is gpt-5.5"): the chase gate (LONG RSI>=65 extended)
        # and the vol gate (vol_ratio>=1.5) are NO LONGER hard code-blocks. The model SEES rsi14,
        # px_vs_ema20_pct and vol_ratio in its context and is told in the prompt that chasing extended
        # RSI is its #1 measured loss and that its method wants strong volume — it judges these itself now.
        _tfb = str(dec.get("tf_basis", "15m")).lower()    # which TF the model based the setup on
        if _tfb not in ("15m", "1h", "4h"):
            _tfb = "15m"
        out.append({**ctx, "action": action, "leverage": lev, "size_pct": size_pct,
                    "sl_pct": sl_pct, "tp_pct": tp_pct, "entry_px": entry_px, "tf_basis": _tfb,
                    "rationale": str(dec.get("rationale", ""))[:240]})
    return out


_MEMORY_RULE = ("Learn from your MEMORY block CONTEXTUALLY: the counts are evidence to weigh, not bans — a past "
                "loss does NOT blanket-ban a setup; the same idea can win on another coin/regime/time (markets are "
                "non-stationary). Pick only the BEST setups; SKIP is common and fine — no forced trades. "
                "Owner LAW (updated 2026-07-13): position SIZE and LEVERAGE are YOUR decisions — size each trade "
                "from your CURRENT equity, open exposure, setup quality and stop distance. Know the physics: "
                "liquidation sits ~100/leverage % away and a gap-risk veto blocks entries whose liquidation is "
                "within ~3 ATR; code only clamps insanity (size<=40% margin, lev<=25). Bet bigger on A+ setups, "
                "smaller on B — that judgment is the job. Your EMA+VOLUME+price method strongly PREFERS strong participation — quiet-bar "
                "entries (vol_ratio<1.5) are your measured losing pattern, so weigh volume heavily and prefer "
                "vol_ratio>=1.5 — but that is now YOUR call, not a code block; never enter the volume/ignition bar itself; wait for the level to HOLD "
                "(close-back-through + follow-through, or a sweep-and-reclaim) — a volume spike confirms "
                "PARTICIPATION not DIRECTION (your lab proved ignition candles carry no directional edge).")
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
    "current extended price. Market-enter (omit entry_px) ONLY on a sweep-and-reclaim that already HELD, NEVER on a "
    "fresh breakout — fresh breaks get faded (your measured #1 loss). A limit is allowed only at a pre-validated "
    "level or a zone you expect price to pull back INTO, never resting into the ignition bar. A limit "
    "waits for price to come to you and cancels if it runs away — this is how a disciplined trader enters.\n"
    "===DECISIONS===\nThen a JSON ARRAY (may be empty) of ONLY coins that PASSED the gate: "
    "[{\"symbol\":\"BTCUSDT\",\"action\":\"LONG|SHORT|SKIP\",\"leverage\":<YOUR call, 1-25>,\"size_pct\":<YOUR call, % of equity, 1-40>,"
    "\"entry_px\":<limit price, or omit for market>,\"sl_pct\":0.5-5,\"tp_pct\":0.5-10,"
    "\"tf_basis\":\"15m|1h|4h\",\"rationale\":\"cite levels + how many confluences\"}]")


_THINK_PATH = LT_DIR / "thinking_latest.json"


def _symbol_history() -> dict[str, dict[str, Any]]:
    """Per-symbol dossier of the model's OWN record (data-flow v2, owner 2026-07-13: the model
    was re-proposing KORU 31 times with no memory that it had already been rejected/burned
    there). {sym: {n, net, last_reasons}} from the closed ledger — cheap, computed per cycle."""
    out: dict[str, dict[str, Any]] = {}
    try:
        for r in _dedupe_closed(_load(CLOSED))[-200:]:
            s = r.get("symbol")
            if not s:
                continue
            d = out.setdefault(s, {"n": 0, "net": 0.0, "last_reasons": []})
            d["n"] += 1
            d["net"] = round(d["net"] + float(r.get("net") or 0), 3)
            d["last_reasons"] = (d["last_reasons"] + [r.get("reason")])[-3:]
    except Exception:
        pass
    return out


def _recent_rejections(hours: float = 12.0) -> dict[str, dict[str, Any]]:
    """{sym: {n, last_reason}} from recent stage-2 rejections — stage-1 had NO memory of them,
    so it re-proposed the same idea every cycle (TAO 29x in 8h, each a burned vision call).
    Injected into the board + coins_txt so the model stops knocking on a door it already
    closed — unless the structure actually CHANGED (its call, told in the prompt)."""
    out: dict[str, dict[str, Any]] = {}
    try:
        import time as _t
        cutoff = _t.time() * 1000 - hours * 3600 * 1000
        for l in (LT_DIR / "governance.jsonl").read_text(encoding="utf-8", errors="replace").splitlines()[-500:]:
            try:
                g = json.loads(l)
            except Exception:
                continue
            if g.get("event") == "stage2_reject" and float(g.get("ts_ms") or 0) >= cutoff:
                d = out.setdefault(str(g.get("symbol")), {"n": 0, "last_reason": ""})
                d["n"] += 1
                d["last_reason"] = str(g.get("reason") or "")[:90]
    except Exception:
        pass
    return out


def _btc_context_chart(client: Any, now_ms: int) -> tuple[str, str] | None:
    """BTC 1h chart — the market-regime context every alt decision should see (data-flow v2:
    the model was trading alts blind to what BTC is doing RIGHT NOW). Best-effort."""
    try:
        fb = of.fetch_klines_with_flow("BTCUSDT", "1h", months=0.5, end_ms=now_ms,
                                       client=client, sleep_between=0.02, with_deriv=False)
        fb = [b for b in fb if int(b["ts_ms"]) + of._TF_MS["1h"] <= now_ms]
        if len(fb) < 40:
            return None
        try:                                        # numeric tide (sếp 2026-07-13: "thị trường thay
            closes = [float(b["close"]) for b in fb]   # đổi long hay short nó phải theo") — the chart
            r24 = round((closes[-1] / closes[-25] - 1) * 100, 2) if len(closes) > 25 else None
            e50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else None
            _btc_context_chart._tide = {            # is an image; the model also gets it as NUMBERS.
                "btc_ret_24h_pct": r24,
                "btc_vs_ema50_1h": round((closes[-1] / e50 - 1) * 100, 2) if e50 else None,
                "tide": ("dumping" if (r24 or 0) < -1.5 else "pumping" if (r24 or 0) > 1.5 else "flat")}
        except Exception:
            _btc_context_chart._tide = None
        b64 = ltc.render_chart("BTCUSDT", fb[-160:], tf="1h", title_suffix=" · MARKET CONTEXT")
        return ("BTCUSDT 1h — MARKET CONTEXT (the tide all alts swim in)", b64) if b64 else None
    except Exception:
        return None


def _board_pass(context: list[dict[str, Any]], status: dict[str, Any] | None,
                hist: dict[str, dict[str, Any]]) -> list[str] | None:
    """DATA-FLOW v2 (owner: "flow chảy của dữ liệu"): the MODEL drives its own attention.
    One cheap TEXT call: the full numeric board (every hot coin, both directions) + its open
    positions + its per-symbol record -> it returns the symbols worth deep chart analysis
    this cycle. Replaces the code-side activity heuristic as the chooser; falls back to the
    activity ranking on any failure (fail-open, never blocks the cycle)."""
    try:
        _rej = _recent_rejections()
        rows = []
        for c in context:
            rows.append({k: c.get(k) for k in
                         ("symbol", "price", "ret5_pct", "ret20_pct", "rsi14", "vol_ratio",
                          "atr_pct", "regime", "funding_rate", "wick_intensity") if c.get(k) is not None}
                        | ({"trigger_edge": list((c.get("trigger_info") or {}).get("_edge", {}))}
                           if isinstance(c.get("trigger_info"), dict) else {})
                        | ({"your_record_here": hist[c["symbol"]]} if c.get("symbol") in hist else {})
                        | ({"rejected_by_your_2nd_look": _rej[c["symbol"]]}
                           if c.get("symbol") in _rej else {}))
        opens = [{"symbol": p.get("symbol"), "side": p.get("side")} for p in _load(POSITIONS)]
        sys_p = ("You are a discretionary futures trader scanning the FULL board to allocate your "
                 "attention. Below: numeric state of every hot coin (both LONG and SHORT are yours), "
                 "your open positions, and your own past record per symbol (respect it — re-proposing "
                 "a setup you were burned on repeatedly is your measured mistake). NOTE: capitulation "
                 "flushes are auto-traded by a mechanical path — your edge is everything ELSE: trends, "
                 "breakdowns, shorts, structure plays. DIRECTION FOLLOWS THE TAPE (owner): market_tide "
                 "dumping -> hunt SHORT setups first; pumping -> longs first; flip when it flips. "
                 "Pick the coins genuinely worth a deep chart "
                 "look RIGHT NOW (fewer is fine; empty if nothing is interesting). Reply STRICT JSON: "
                 '{"investigate":["SYMBOL1",...max 6],"why":"<=120 chars"}')
        txt = _llm(sys_p, json.dumps({"board": rows, "your_open_positions": opens,
                                      "market_tide": getattr(_btc_context_chart, "_tide", None),
                                      "capacity": (status or {}).get("capacity")}, default=str),
                   max_tokens=4000, effort="medium")   # triage call, not the deep decision (Opus F2:
                                                       # high-effort at 2k tokens could truncate to a no-op)
        d = _extract_json(txt) if txt else None
        # _extract_json prefers the FIRST [...] span, so on {"investigate":[...]} it returns the
        # INNER ARRAY, not the dict (silent-None bug caught 07-14: board_pass never fired once).
        picks = None
        if isinstance(d, list) and all(isinstance(s, str) for s in d):
            picks = [str(s).upper() for s in d][:6]
        elif isinstance(d, dict) and isinstance(d.get("investigate"), list):
            picks = [str(s).upper() for s in d["investigate"] if isinstance(s, str)][:6]
        if picks is not None:
            _append(LT_DIR / "governance.jsonl",
                    {"event": "board_pass", "picks": picks,
                     "why": (str(d.get("why", ""))[:120] if isinstance(d, dict) else "")})
            return picks
        _append(LT_DIR / "governance.jsonl",     # NEVER fail silently again
                {"event": "board_pass_fail", "reply_head": (txt or "")[:120]})
    except Exception as _bpe:
        try:
            _append(LT_DIR / "governance.jsonl",
                    {"event": "board_pass_fail", "error": repr(_bpe)[:120]})
        except Exception:
            pass
    return None


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
           + _proven_methods_block() + _mistakes_block() + _MEMORY_RULE + " " + _DECISION_SCHEMA)
           # _mistakes_block RE-WIRED 2026-07-15: era-windowed + capped-2 + de-conflicted — the
           # P1-2026-07-09 degeneracy (all-four storm on lifetime 17% WR) can no longer fire.
    usr = json.dumps({"equity": round(equity, 2), "your_status": status or {},
                      "memory": memory_context(), "coins": payload}, default=str)
    return _validate_decisions(_split_thinking(_llm(sys, usr)), by_sym)


def _activity_score(c: dict[str, Any]) -> float:
    """Cheap 'is this coin interesting right now' rank — |recent move| +
    volume surge + order-flow imbalance. Picks which coins get a chart."""
    return (abs(float(c.get("ret20_pct", 0) or 0))
            + 4.0 * abs(float(c.get("vol_ratio", 1) or 1) - 1.0)
            + 3.0 * abs(float(c.get("cvd_norm", 0) or 0)))


def _range_location(dbars: list[dict[str, Any]], price: float) -> dict[str, Any] | None:
    """WHERE price sits in the multi-week range (audit 2026-07-16: the #1 thesis-wrong
    driver was buying breakouts at range highs the model could not see — its sight
    horizon was ~10 days, no daily/range context). PURE, from daily bars. None on
    insufficient/degenerate input (fail-open: the field is simply absent)."""
    try:
        px = float(price)
        highs = [float(b["high"]) for b in dbars if float(b.get("high") or 0) > 0]
        lows = [float(b["low"]) for b in dbars if float(b.get("low") or 0) > 0]
        if len(highs) < 5 or len(lows) < 5 or px <= 0:
            return None

        def _rng(n: int) -> dict[str, Any]:
            h = max(highs[-n:]); l = min(lows[-n:]); span = h - l
            pos = (px - l) / span * 100 if span > 0 else 50.0
            return {"hi": round(h, 6), "lo": round(l, 6),
                    "pos_pct": round(max(0.0, min(100.0, pos)), 1),   # 0=at low, 100=at high
                    "to_hi_pct": round((h - px) / px * 100, 2),
                    "to_lo_pct": round((px - l) / px * 100, 2)}

        out: dict[str, Any] = {"d30": _rng(min(30, len(highs))), "d7": _rng(min(7, len(highs)))}
        if len(dbars) >= 2:    # last daily bar may be today (partial); prev-day = last CLOSED day
            pd = dbars[-2]
            out["prev_day"] = {"hi": round(float(pd["high"]), 6), "lo": round(float(pd["low"]), 6)}
        return out
    except Exception:
        return None


def decide(context: list[dict[str, Any]], equity: float,
           status: dict[str, Any] | None = None, *, max_charts: int = 6,   # owner 2026-07-13 "vắt kiệt model": see MORE coins/cycle (4->6)
           client: Any = None, now_ms: int | None = None) -> list[dict[str, Any]]:
    """CHART-BASED decision (owner request A+B): scan all coins numerically, pick
    the most active ones, RENDER their candlestick+EMA+volume charts and let
    gpt-5.5 SEE them (vision) before deciding — the way a discretionary trader
    scans a watchlist then opens charts. Falls back to a numeric-only decision on
    the same shortlist if the vision call fails, so it never stalls."""
    if not context:
        return []
    # shortlist: DATA-FLOW v2 — the MODEL scans the full numeric board and chooses what to
    # investigate (its attention, its call). None = board-pass failed -> activity fallback;
    # [] = model says nothing is interesting -> respect it (mech/proven paths still run).
    ranked = sorted(context, key=_activity_score, reverse=True)
    hist = _symbol_history()
    picks = _board_pass(context, status, hist)
    try:      # mid-cycle heartbeat (Opus F1): the extra LLM call must not eat the supervisor's
        _hb({"phase": "board_pass", "picks": len(picks) if picks else 0})   # 1200s stale margin
    except Exception:
        pass
    if picks is not None:
        _by = {c["symbol"]: c for c in context}
        shortlist = [_by[s] for s in picks if s in _by][:max_charts]
    else:
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
    # FLUSH OVERRIDE (shadow live-forward CONFIRMED 2026-07-12): flush_no_oi is the one path measured
    # live-positive, but the activity re-sort above was silently dropping low-activity flush coins —
    # the exact candidates the R2 flush-first gate exists to surface (Opus review finding). Same
    # mechanics as the A+ override: a flush-triggered coin gets charted regardless of activity rank.
    flush = [c for c in ranked if isinstance(c.get("trigger_info"), dict)
             and any(p in (c["trigger_info"].get("_edge") or {}) for p in ("flush_no_oi", "flush_oi_dn"))]
    for c in flush:
        if c not in shortlist:
            shortlist = [c] + shortlist[:max_charts - 1]
    by_sym = {c["symbol"]: c for c in shortlist}
    import time as _t2
    _nowms = int(now_ms if now_ms is not None else _t2.time() * 1000)
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
            images.append((c["symbol"] + " 15m", b64)); charted.append(c)  # LLM saw is persisted
            # MULTI-TF (owner 2026-07-09: "đánh cả ba khung, khung nào đánh thì nhìn chart khung đó lại"):
            # render the SAME coin on REAL 1h and 4h bars so the model confirms the setup on the actual
            # higher-TF chart, not a resampled number. Best-effort — skip a TF if fetch/render fails.
            if client is not None:
                for _tf, _mo in (("1h", 0.5), ("4h", 2.0)):
                    try:
                        _tfb = _fetch_bars_any(c["symbol"], _tf, _mo, _nowms, client,
                                               sleep_between=0.02, with_deriv=False)
                        _tfb = [b for b in _tfb if int(b["ts_ms"]) + of._TF_MS[_tf] <= _nowms]
                        if len(_tfb) >= 30:
                            _b64tf = ltc.render_chart(c["symbol"], _tfb[-160:], tf=_tf)
                            if _b64tf:
                                images.append((c["symbol"] + " " + _tf, _b64tf))
                    except Exception:
                        pass
                # DAILY + RANGE-LOCATION (audit 2026-07-16: the model's sight horizon was
                # ~10 days and it could not see WHERE price sat in the multi-week range —
                # the measured #1 thesis-wrong driver, buying breakouts at range highs).
                # Daily bars raw (a daily chart's last partial candle is legit context);
                # 90-bar 1d chart = 3-month horizon. Additive + best-effort (fail-open).
                try:
                    _dk = (c["symbol"], _nowms // 86_400_000)   # daily bars change once/UTC-day
                    _db = _DAILY_CACHE.get(_dk)                  # -> fetch 1x/coin/day, not 1x/cycle
                    if _db is None:
                        _db = _fetch_bars_any(c["symbol"], "1d", 3.0, _nowms, client,
                                              sleep_between=0.02, with_deriv=False)
                        if _db:
                            if len(_DAILY_CACHE) > 128:
                                _DAILY_CACHE.clear()
                            _DAILY_CACHE[_dk] = _db
                    if _db and len(_db) >= 5:
                        _rl = _range_location(_db, c.get("price"))
                        if _rl:
                            c["range_ctx"] = _rl
                        _b64d = ltc.render_chart(c["symbol"], _db[-90:], tf="1d")
                        if _b64d:
                            images.append((c["symbol"] + " 1d", _b64d))
                except Exception:
                    pass
    if not images:
        return _decide_numeric(shortlist, equity, status)   # nothing rendered -> numeric
    _btc = _btc_context_chart(client, _nowms) if client is not None else None
    if _btc:
        images.append(_btc)          # market tide context rides with every alt decision
    # compact numeric + SMC read to accompany each chart (no raw bars in text)
    _rej = _recent_rejections()
    coins_txt = [{"symbol": c["symbol"], "smc": c.get("_smc", {}),
                  "your_record_here": hist.get(c["symbol"]),   # its OWN past on this symbol
                  "your_recent_rejections_here": _rej.get(c["symbol"]),   # stage-2 already said no N times
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
           "MULTI-TIMEFRAME (owner rule): each coin is shown on THREE real charts — labeled '<SYM> 15m', "
           "'<SYM> 1h', '<SYM> 4h'. READ ALL THREE before deciding: the 4h and 1h define the DOMINANT trend "
           "and the major support/resistance; the 15m is the entry trigger/timing. Trade the timeframe where "
           "the setup is CLEANEST, and base your SL/TP on THAT timeframe's structure — a 4h-based trade has a "
           "WIDER 4h-structure stop (it must not sit inside 15m noise), a 15m scalp a tight one. A setup that "
           "looks good on 15m but fights the 1h/4h trend is a TRAP (this is your 75%-noise-stop leak) — require "
           "the higher timeframe to at least NOT oppose you. Report the timeframe you based the trade on as "
           "\"tf_basis\": one of \"15m\"|\"1h\"|\"4h\".\n\n"
           "wick_intensity (0.0-1.0) = fraction of the last 12 bars dominated by WICK (long tails, small "
           "body) = a stop-hunt 'rút râu' chop where tight stops get clipped by noise (your measured 75%-"
           "SL leak). It is NOT a hard block anymore — YOU judge it: when it's high (~0.5+), either stand "
           "aside or take only a high-conviction setup with a WIDE structure stop that clears the wicks. "
           "Likewise 'regime':'choppy' has been your worst zone (7% win) — trade it only with a genuine "
           "edge, not a marginal one. These are YOUR calls now, not code gates.\n\n"
           "REJECTION MEMORY: 'your_recent_rejections_here' = how many times YOUR OWN second look already "
           "rejected this coin recently and why. Do NOT re-propose the same idea unless the chart shows the "
           "exact thing the rejection said was missing (e.g. the retest/confirmation has now printed). "
           "Re-knocking on a closed door burns calls and is your measured over-trading pattern.\n\n"
           "DIRECTION IS ADAPTIVE (owner directive): LONG and SHORT are equal citizens — your side must "
           "FOLLOW the current tape, never a habit. Check 'market_tide' + the BTCUSDT MARKET CONTEXT "
           "chart FIRST: when the market is DUMPING, breakdown continuations and bounce-fade SHORTS are "
           "the high-probability side and longs need exceptional justification; when it is PUMPING, the "
           "reverse. When the tide FLIPS, you flip with it. (A mechanical path already buys capitulation "
           "flushes automatically — do not duplicate it; your job is the directional judgment it lacks.)\n\n"
           "RANGE LOCATION + TIME (2026-07-16 — your measured #1 leak was buying breakouts where they "
           "FADE): each coin carries 'range_ctx' = where price sits in its 7d and 30d range (pos_pct: "
           "0=range LOW, 100=range HIGH) with distance to the range hi/lo and the previous day's hi/lo, "
           "plus a '<SYM> 1d' daily chart (≈3-month view). USE IT: a LONG 'breakout' when pos_pct is "
           "already ~85-100 (pinned to the 30d high) is buying the exact spot longs get trapped — prefer "
           "LONGs in the LOWER half of the range off support, SHORTS in the UPPER half into resistance; a "
           "with-trend entry at a range extreme needs exceptional justification. 'now' gives UTC time, "
           "weekday and session — the 22 tokenized-stock perps (NVDA/TSLA/JPM/META/XAU/...) only have REAL "
           "liquidity when us_equity_open is true; outside cash hours and on WEEKENDS their candles are "
           "thin/synthetic, so do NOT trade a 'breakout' on a TradFi perp outside cash hours.\n\n"
           + (_playbook() and ("=== TRADING PLAYBOOK (apply this) ===\n" + _playbook() + "\n=== END PLAYBOOK ===\n\n"))
           + _proven_methods_block() + _mistakes_block() + _MEMORY_RULE + " " + _DECISION_SCHEMA)
           # _mistakes_block RE-WIRED 2026-07-15: era-windowed + capped-2 + de-conflicted — the
           # P1-2026-07-09 degeneracy (all-four storm on lifetime 17% WR) can no longer fire.
    import time as _tt
    _utc = _tt.gmtime(_nowms / 1000)
    _hr = _utc.tm_hour; _wknd = _utc.tm_wday >= 5
    _now_ctx = {"utc": _tt.strftime("%Y-%m-%d %H:%M", _utc), "weekday": _tt.strftime("%a", _utc),
                "session": ("WEEKEND" if _wknd else "Asia" if _hr < 7 else "EU" if _hr < 13
                            else "US-cash" if _hr < 20 else "US-late"),
                "us_equity_open": bool(not _wknd and 13 <= _hr < 20)}   # NYSE ~13:30-20:00 UTC
    text = json.dumps({"equity": round(equity, 2), "your_status": status or {},
                       "now": _now_ctx,                                  # clock/session (audit #3)
                       "market_tide": getattr(_btc_context_chart, "_tide", None),   # numeric tide (sếp: side must follow the tape)
                       "memory": mem, "charted_coins": coins_txt,
                       "market_overview": market_overview}, default=str)
    out = _validate_decisions(_split_thinking(_llm_vision(sys, text, images)), by_sym)
    if out:
        return out
    return _decide_numeric(charted, equity, status)   # vision failed/empty -> numeric fallback


def _stage2_confirm(decisions: list[dict[str, Any]], client: Any, now_ms: int) -> list[dict[str, Any]]:
    """R2 SECOND LOOK (owner: 'chọn đánh khung nào thì nhìn lại khung đó rồi quyết định'). For each
    actionable stage-1 decision (bounded STAGE2_MAX), re-render ONLY the chosen TF fresh (more bars +
    S/R zones) and ask the model to CONFIRM or REJECT its own proposal. Model REJECT -> the trade is
    dropped (that is the point of the second look). TECHNICAL failure (fetch/render/LLM error) ->
    pass-through tagged 'error_passthrough': an outage must not silently zero all trading (the
    '0 trades' trap) — it is logged and visible, not a silent block. Never raises."""
    if not REDESIGN or not decisions:
        return decisions
    # funnel audit: ZBT was re-proposed + re-rejected 25x/16h (~60 wasted vision calls/day).
    # A (symbol, side) stage-2 REJECT is binding for 2h in CODE — the prompt-side rejection
    # memory has proven the model ignores it. Fail-open on a corrupt file.
    _rej_f = LT_DIR / "stage2_rejects.json"
    try:
        _rej = json.loads(_rej_f.read_text(encoding="utf-8"))
        _rej = _rej if isinstance(_rej, dict) else {}
    except Exception:
        _rej = {}
    out: list[dict[str, Any]] = []
    looked = 0
    for d in decisions:
        _rk = f"{d.get('symbol')}|{str(d.get('action')).upper()}|{d.get('tf_basis') or '15m'}"
        if now_ms - int(_rej.get(_rk) or 0) < 2 * 3600 * 1000:
            _append(LT_DIR / "governance.jsonl",
                    {"ts_ms": now_ms, "event": "stage2_dedup_skip", "symbol": d.get("symbol"),
                     "side": d.get("action")})
            continue                           # rejected <2h ago -> no new vision call
        if looked >= STAGE2_MAX:               # over budget: pass through untagged-looked
            d["_stage2"] = "skipped_budget"
            out.append(d)
            continue
        looked += 1
        sym = d.get("symbol"); tf = str(d.get("tf_basis") or "15m")
        try:      # mid-cycle heartbeat (review FIX-BEFORE-FLIP #1): 3 stage-2 vision calls can push
            _hb({"phase": "stage2", "symbol": sym, "looked": looked})   # run_once past the supervisor's
        except Exception:                                               # 1200s stale bound — touching the
            pass                                                        # hb per look caps the gap at ~1 call
        try:
            if tf == "15m":
                bars = list(d.get("_bars") or [])
            else:
                _mo = 0.5 if tf == "1h" else 2.0
                bars = _fetch_bars_any(sym, tf, _mo, now_ms, client,   # review FIX-FIRST #1:
                                       sleep_between=0.02, with_deriv=False)   # Binance-only fetch
                bars = [b for b in bars if int(b["ts_ms"]) + of._TF_MS[tf] <= now_ms]
                # made stage-2 error_passthrough BYPASS the confirm gate for every TidalFi
                # symbol on a 1h/4h basis — the newest asset class skipped review entirely.
            if len(bars) < 30:
                raise ValueError("not enough bars for stage-2")
            sm = smc.smc_summary(bars, sym, tf)
            b64 = ltc.render_chart(sym, bars[-260:], tf=tf, hlines=(sm.get("hlines") or None))
            if not b64:
                raise ValueError("stage-2 render failed")
            # funnel audit: judging "entry RIGHT NOW" killed ~1/3 of proposals for being
            # limits — "await retest / entry missed" IS what a resting limit is for. The
            # judge must evaluate the PROPOSED entry price, not the current spot.
            _entry_frame = ("does structure on THIS timeframe still support the entry RIGHT NOW? "
                            if not d.get("entry_px") else
                            "this is a RESTING LIMIT at entry_px=" + str(d.get("entry_px")) +
                            " (NOT a market entry now): judge whether structure supports the plan "
                            "IF price pulls back to that level — 'entry missed'/'await retest' are "
                            "NOT valid reject reasons for a limit; reject only if the LEVEL or the "
                            "thesis is wrong (zone broken, trend flipped, level on the wrong side). ")
            sys2 = ("SECOND LOOK — you are the same trader. On your broad 3-timeframe scan you proposed "
                    "the trade below and chose the " + tf + " timeframe as its basis. This is that SAME "
                    "coin redrawn FRESH on " + tf + " only, with more bars and S/R zones. Decide FINAL: "
                    + _entry_frame +
                    "Be strict — a "
                    "marginal setup on the second look is a REJECT (rejecting is free, a bad entry is not). "
                    "Reply STRICT JSON only: {\"confirm\": true|false, \"reason\": \"<=120 chars\", "
                    "\"sl_pct\": <number or null>, \"tp_pct\": <number or null>} — set sl/tp to THIS "
                    "timeframe's structure if the old ones don't fit (sl 0.3-8, tp 0.3-15; null keeps old).")
            body = json.dumps({"proposal": {k: d.get(k) for k in
                                            ("symbol", "action", "leverage", "size_pct", "sl_pct",
                                             "tp_pct", "entry_px", "tf_basis", "rationale")},
                               "trigger_info": d.get("trigger_info"),
                               "numbers": {k: d.get(k) for k in
                                           ("price", "atr_pct", "rsi14", "vol_ratio", "funding_rate",
                                            "regime", "wick_intensity")},
                               "smc_" + tf: sm.get("summary") or {}}, default=str)
            v = _split_thinking(_llm_vision(sys2, body, [(str(sym) + " " + tf + " (second look)", b64)]))
            if isinstance(v, list):
                v = v[0] if v and isinstance(v[0], dict) else None
            if not isinstance(v, dict):
                raise ValueError("stage-2 reply not a dict")
            if not bool(v.get("confirm")):
                _append(LT_DIR / "governance.jsonl",
                        {"ts_ms": now_ms, "event": "stage2_reject", "symbol": sym, "tf": tf,
                         "reason": str(v.get("reason", ""))[:140]})
                try:                            # 2h code-side dedupe (see head of function)
                    _rej[f"{sym}|{str(d.get('action')).upper()}|{d.get('tf_basis') or '15m'}"] = now_ms
                    _rej = {k: t for k, t in sorted(_rej.items(), key=lambda kv: -kv[1])[:80]}
                    _tmpf = _rej_f.with_suffix(".tmp")
                    _tmpf.write_text(json.dumps(_rej), encoding="utf-8")
                    os.replace(_tmpf, _rej_f)
                except Exception:
                    pass
                continue                        # model rejected its own idea on the focused look -> drop
            import math as _m
            for k, lo, hi in (("sl_pct", 0.3, 8.0), ("tp_pct", 0.3, 15.0)):
                try:
                    nv = float(v.get(k))
                    if _m.isfinite(nv):
                        d[k] = max(lo, min(hi, nv))   # same clamps as _validate_decisions
                except (TypeError, ValueError):
                    pass                             # null/absent -> keep stage-1 value
            d["_stage2"] = "confirmed"
            d["rationale"] = (str(d.get("rationale") or "") + " | s2:" + str(v.get("reason", ""))[:100])[:340]
            out.append(d)
        except Exception as _s2e:
            d["_stage2"] = "error_passthrough"
            _append(LT_DIR / "governance.jsonl",
                    {"ts_ms": now_ms, "event": "stage2_error_passthrough", "symbol": sym, "tf": tf,
                     "error": repr(_s2e)[:140]})
            out.append(d)
    return out


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


def _apply_gap_veto(decisions: list[dict[str, Any]], ctx: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The ONLY hard filter on the DISCRETIONARY (LLM) path — the gap-tail RUIN veto. 2026-07-09 (owner:
    "rules too rigid / conservative"): the choppy-regime and rút-râu/wick quality gates were REMOVED.
    Stacking quality gates on a -EV base doesn't create edge — it just stops the strong model trading and
    cages its own judgment, so we can never learn whether the MODEL has an edge. The model still SEES
    regime + wick_intensity + the real 15m/1h/4h charts + its measured mistake-lessons in context and
    decides for itself. What survives here is SURVIVAL, not conservatism: a high-gap coin can gap THROUGH
    the stop straight to liquidation (the -$14 HMSTR ruin). Fail-CLOSED (None/NaN/degenerate -> block)."""
    by_sym = {c.get("symbol"): c for c in ctx}
    import time as _t
    now_ms = int(_t.time() * 1000)
    out = []
    for d in decisions:
        row = by_sym.get(d.get("symbol")) or {}
        _atr = row.get("atr_pct"); _gr = row.get("gap_risk_pct")
        _liq = 100.0 / max(1, int(d.get("leverage") or MECH_LEV))
        if (_atr is None or _atr != _atr or float(_atr) <= 0 or float(_atr) * GAP_LIQ_ATR_MULT > _liq
                or _gr is None or _gr != _gr or float(_gr) * GAP_RISK_MULT > _liq):
            _append(LT_DIR / "governance.jsonl",
                    {"ts_ms": now_ms, "event": "gate_block_gap_risk_llm", "symbol": d.get("symbol"),
                     "atr_pct": _atr, "gap_risk_pct": _gr, "liq_dist_pct": _liq,   # audit: numbers
                     "lev": d.get("leverage")})                                     # were unlogged
            continue
        out.append(d)
    return out


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
                                  "tf_basis": d.get("tf_basis", "15m"),
                                  "trigger_paths": d.get("_trigger_paths"),   # R1 measurement tag
                                  "stage2": d.get("_stage2"),                 # R2 second-look outcome
                                  "model": d.get("_model"), "pipeline_mode": d.get("_pipeline_mode"),
                                  "tide_at_entry": d.get("_tide_at_entry"), "tide_aligned": d.get("_tide_aligned"),
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
        # AGGREGATE NOTIONAL CAP (Codex CRITICAL-1): total margin cap alone doesn't bound risk
        # when leverage is free — 95% margin at x25 = 23x notional. Cap SUM of open notional
        # (margin*lev) + this new one at AGG_NOTIONAL_CAP_PCT of equity. Bounds correlated ruin.
        _open_notional = sum(float(p.get("margin") or 0) * int(p.get("leverage") or 1)
                             for p in _load(POSITIONS))   # reflects opens already made THIS cycle
        if equity > 0 and (_open_notional + notional) / equity * 100 > AGG_NOTIONAL_CAP_PCT:
            _append(LT_DIR / "governance.jsonl",
                    {"ts_ms": now_ms, "event": "agg_notional_cap_block", "symbol": d["symbol"],
                     "open_notional_pct": round(_open_notional / equity * 100), "add_pct": round(notional / equity * 100)})
            continue
        qty = notional / entry if entry > 0 else 0.0
        # FULL TRUST (owner 2026-07-09: "the brain is gpt-5.5, let it think on the indicators + balance").
        # Use the model's OWN sl/tp % — it set them reading the multi-TF charts and structure itself. No
        # code structure-override, and no R:R-skip that silently dropped the model's trades. (Proven/mech
        # methods use the same math on their backtested sl/tp.)
        sl = entry * (1 - d["sl_pct"] / 100) if side == "LONG" else entry * (1 + d["sl_pct"] / 100)
        tp = entry * (1 + d["tp_pct"] / 100) if side == "LONG" else entry * (1 - d["tp_pct"] / 100)
        # Forced-liquidation price (plan item #1): stored at open so resolve()
        # can rank liq ahead of SL pessimistically on every bar.
        mmr = lr.mmr_for(d["symbol"])
        liq_px = lr.liquidation_price(entry, lev, side, mmr)
        chart_rel = _save_entry_chart(d.get("_chart_b64"), d["symbol"], d["_ts"])
        open_pos.append({"symbol": d["symbol"], "side": side, "entry": entry, "qty": qty,
                         "margin": round(margin, 4), "leverage": lev, "sl": sl, "tp": tp,
                         "liq_px": liq_px, "mmr": mmr, "quote_vol_24h": quote_vol, "tier": tier,
                         "entry_ts": d["_ts"], "opened_at": now_iso, "regime": d["regime"],
                         "tf_basis": d.get("tf_basis", "15m"),   # which TF the model based the trade on
                         "fill_bar_ts": d.get("_fill_bar_ts"),
                         "mech": bool(d.get("_mech")), "max_hold": (int(d.get("_max_hold") or 16) if d.get("_mech") else None),
                         "mech_method": d.get("_mech_method"), "entry_feats": d.get("_entry_feats"),
                         "trigger_paths": d.get("_trigger_paths"),   # R1: which selection paths fired (measurement)
                         "stage2": d.get("_stage2"),   # R2: confirmed | error_passthrough | skipped_budget | None
                         "model": d.get("_model"), "pipeline_mode": d.get("_pipeline_mode"),   # provenance (gap #5)
                         "tide_at_entry": d.get("_tide_at_entry"), "tide_aligned": d.get("_tide_aligned"),
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
            fb = _fetch_bars_any(sym, TF, 0.05, now_ms, client, sleep_between=0.02)
            fut = [b for b in fb if int(b["ts_ms"]) > int(po["placed_ms"]) and int(b["ts_ms"]) + bar_ms <= now_ms]
        except Exception:
            still.append(po); continue
        fill_ts = None
        for b in fut:
            if (side == "LONG" and float(b["low"]) <= limit) or (side == "SHORT" and float(b["high"]) >= limit):
                fill_ts = int(b["ts_ms"]); break
        if fill_ts is not None:                       # FILLED -> open at the limit price
            # Codex #2: RE-VETO gap risk at FILL. The gap-veto ran when the limit was PLACED, but the
            # coin may have turned volatile while it rested (a flush candle after placement). Re-check the
            # gap-tail against fresh bars before opening — same fail-closed rule as entry.
            _rngs = [(float(b["high"]) - float(b["low"])) / float(b["close"])
                     for b in fb[-48:] if float(b.get("close") or 0) > 0]
            _grisk = (max(_rngs) * 100) if _rngs else None
            _liqd = 100.0 / max(1, int(po.get("leverage") or MECH_LEV))
            if _grisk is None or _grisk != _grisk or _grisk * GAP_RISK_MULT > _liqd:
                _append(LT_DIR / "pending_events.jsonl",
                        {"symbol": sym, "side": side, "event": "fill_blocked_gap_risk",
                         "gap_risk_pct": _grisk, "ts": now_ms})
                continue
            d = {"symbol": sym, "action": side, "price": limit, "leverage": po["leverage"],
                 "size_pct": po["size_pct"], "sl_pct": po["sl_pct"], "tp_pct": po["tp_pct"], "entry_px": None,
                 "_smc": po.get("smc") or {}, "atr": po.get("atr", 0), "_quote_vol_24h": po.get("quote_vol_24h", 0),
                 "vol_ratio": po.get("vol"), "_maker": True,
                 "regime": po.get("regime"), "_chart_b64": po.get("chart_b64"),
                 "_ts": fill_ts - 1, "_fill_bar_ts": fill_ts,
                 "tf_basis": po.get("tf_basis", "15m"),
                 "_trigger_paths": po.get("trigger_paths"),   # R1 tag flows through the limit path too
                 "_stage2": po.get("stage2"),                 # R2 tag flows through the limit path too
                 "_model": po.get("model"),                   # provenance flows through the limit path too
                 "_pipeline_mode": po.get("pipeline_mode"),   # (Opus IMPORTANT-2: limit fills closed with
                 "_tide_at_entry": po.get("tide_at_entry"),   #  model=None forever -> era gate starved +
                 "_tide_aligned": po.get("tide_aligned"),     #  selection bias against limit entries)
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


MANAGE_INTERVAL_MS = int(float(os.environ.get("LLM_TRADER_MANAGE_SEC", "240")) * 1000)


def _manage_positions_llm(client: Any, now_ms: int) -> int:
    """VẮT KIỆT MODEL (owner 2026-07-13: "model nó linh hoạt hơn rule rất nhiều"): the model
    MANAGES its own open discretionary positions. The entry bracket is its opening opinion,
    not a prison — every ~4min it sees a FRESH chart + live state per position and may HOLD,
    CLOSE (market, next bar), or move SL/TP to structure. Mech positions are excluded (their
    fixed bracket IS the experiment being measured).

    ANTI-LOOKAHEAD (the exact trap the lane-ratchet review caught): resolve() replays all
    bars since entry each cycle, so a stop moved on TODAY's knowledge must never apply to
    YESTERDAY's bars. Adjustments are appended to p["mgmt"] with a timestamp and resolve()
    activates each entry only for bars that OPEN at/after that timestamp.

    RUIN FLOORS ONLY: SL must stay outside a 2% liquidation buffer and on the correct side
    of the mark; TP on the correct side; CLOSE always allowed (risk-reducing). Widening the
    stop is ALLOWED (owner: trust the model — max loss is still capped by the margin)."""
    open_pos = _load(POSITIONS)
    cand = [p for p in open_pos if not p.get("mech")]
    if not cand:
        return 0
    stf = LT_DIR / "manage_state.json"
    cursor = 0
    try:
        _mst = json.loads(stf.read_text())
        if now_ms - int(_mst.get("last_ms") or 0) < MANAGE_INTERVAL_MS:
            return 0
        cursor = int(_mst.get("cursor") or 0)
    except Exception:
        pass
    # ROTATE (Codex: only first 4 of N were ever managed) — window advances each pass so every
    # position gets reviewed. EFFECTIVE sl/tp (Codex CRITICAL: the model saw the STALE entry sl,
    # not its own prior adjustments in p['mgmt'] -> it could "tighten" and actually WIDEN).
    if len(cand) > 4:
        cursor = cursor % len(cand)
        cand = (cand + cand)[cursor:cursor + 4]
    try:
        # stamp the throttle NOW (Opus F1): a failed pass (Binance ban -> no states; LLM down ->
        # no reply) must still cost the full cooldown, else every 90s cycle re-fires 4 klines
        # fetches + a vision call exactly when we need back-off (IP-ban history).
        stf.write_text(json.dumps({"last_ms": now_ms, "cursor": cursor}), encoding="utf-8")
    except Exception:
        pass
    images, states = [], []
    bar_ms = of._TF_MS[TF]
    for p in cand[:4]:
        try:
            fb = _fetch_bars_any(p["symbol"], TF, 0.03, now_ms,
                                           client=client, sleep_between=0.02)
            bars = [b for b in fb if int(b["ts_ms"]) + bar_ms <= now_ms]
            if len(bars) < 30:
                continue
            mark = float(bars[-1]["close"])
            entry = float(p["entry"]); side = p["side"]
            up_pct = ((mark / entry - 1) if side == "LONG" else (1 - mark / entry)) * 100 * int(p["leverage"])
            b64 = ltc.render_chart(p["symbol"], bars, tf=TF, title_suffix=f" · OPEN {side}")
            if not b64:
                continue
            images.append((f"{p['symbol']} (your open {side})", b64))
            _esl, _etp = float(p["sl"]), float(p["tp"])      # EFFECTIVE sl/tp = base + latest mgmt override
            for _m in (p.get("mgmt") or []):
                if _m.get("sl") is not None: _esl = float(_m["sl"])
                if _m.get("tp") is not None: _etp = float(_m["tp"])
            states.append({"symbol": p["symbol"], "side": side, "leverage": p["leverage"],
                           "entry": entry, "mark": mark, "sl": _esl, "tp": _etp,
                           "liq_px": p.get("liq_px"), "upnl_pct_on_margin": round(up_pct, 2),
                           "bars_held": int((now_ms - int(p["entry_ts"])) / bar_ms),
                           "your_entry_rationale": (p.get("rationale") or "")[:160]})
        except Exception:
            continue
    if not states:
        return 0
    _btc = _btc_context_chart(client, now_ms)
    if _btc:
        images.append(_btc)          # manage with the market tide in view, not just own chart
    sys_p = ("You are managing YOUR OWN open futures positions (paper). For each position you see its "
             "fresh chart and live state. You have FULL authority: HOLD, CLOSE (market out — cutting a "
             "broken thesis early is a skill), or ADJUST sl_px/tp_px to structure (trail a winner, "
             "protect breakeven, give a thesis room — your call; know liquidation sits at liq_px). "
             "Reply STRICT JSON array only: "
             '[{"symbol":"...","action":"HOLD|CLOSE|ADJUST","sl_px":<number|null>,"tp_px":<number|null>,'
             '"reason":"<=100 chars"}]')
    txt = _llm_vision(sys_p, json.dumps({"positions": states}, default=str), images)
    arr = _split_thinking(txt) if txt else None    # returns the parsed decisions JSON
    if isinstance(arr, dict):
        arr = [arr]
    if not isinstance(arr, list):
        return 0
    by_sym = {p["symbol"]: p for p in open_pos}
    applied = 0
    for a in arr:
        if not isinstance(a, dict):
            continue
        p = by_sym.get(str(a.get("symbol")))
        act = str(a.get("action", "HOLD")).upper()
        if not p or p.get("mech") or act == "HOLD":
            continue
        st_row = next((s for s in states if s["symbol"] == p["symbol"]), None)
        if st_row is None:
            continue
        if act == "CLOSE":
            p["close_req_ts"] = now_ms
            applied += 1
            _append(LT_DIR / "governance.jsonl",
                    {"ts_ms": now_ms, "event": "llm_manage_close", "symbol": p["symbol"],
                     "reason": str(a.get("reason", ""))[:120]})
            continue
        if act != "ADJUST":
            continue
        mark = st_row["mark"]; side = p["side"]
        liq = float(p.get("liq_px") or 0)
        new_sl = new_tp = None
        try:
            import math as _math
            if a.get("sl_px") is not None:
                v = float(a["sl_px"])
                ok = _math.isfinite(v) and v > 0 and (   # Opus F2: -inf slipped the comparisons
                    (v < mark and (liq <= 0 or v > liq * 1.02)) if side == "LONG" else
                    (v > mark and (liq <= 0 or v < liq * 0.98)))
                if ok:
                    new_sl = v
            if a.get("tp_px") is not None:
                v = float(a["tp_px"])
                if _math.isfinite(v) and v > 0 and (
                        (side == "LONG" and v > mark) or (side == "SHORT" and v < mark)):
                    new_tp = v
        except Exception:
            pass
        if new_sl is None and new_tp is None:
            _append(LT_DIR / "governance.jsonl",
                    {"ts_ms": now_ms, "event": "llm_manage_rejected", "symbol": p["symbol"],
                     "sl_px": a.get("sl_px"), "tp_px": a.get("tp_px")})
            continue
        p.setdefault("mgmt", []).append({"ts": now_ms, "sl": new_sl, "tp": new_tp,
                                         "why": str(a.get("reason", ""))[:120]})
        applied += 1
        _append(LT_DIR / "governance.jsonl",
                {"ts_ms": now_ms, "event": "llm_manage_adjust", "symbol": p["symbol"],
                 "sl": new_sl, "tp": new_tp, "reason": str(a.get("reason", ""))[:120]})
    if applied:
        _rewrite(POSITIONS, open_pos)
    try:
        stf.write_text(json.dumps({"last_ms": now_ms, "cursor": cursor + 4}), encoding="utf-8")
    except Exception:
        pass
    return applied


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
            fb = _fetch_bars_any(p["symbol"], TF, 0.06, now_ms, client, sleep_between=0.02)
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
        mfe_px = 0.0; mae_px = 0.0   # P0 learning-metric: max favorable / adverse price excursion while held
                                     # (noise-stop vs thesis-wrong). Measurement only — never affects exits.
        # TRUE breakeven must cover round-trip fees + the STOP slippage resolve()
        # itself charges by tier (audit: 12bps flat made every 'BE' exit a
        # guaranteed loss — micro tier slips 150bps). Skip the BE ratchet entirely
        # when the buffer eats most of 1R (placing a fake-BE stop is worse).
        _stop_slip = float(pcm.fill_bps(tier, is_stop=True)) / 10000.0
        BE_BUF = 2 * float(pcm.TAKER_FEE_RATE) + _stop_slip + 0.0002
        fb_ts = int(p.get("fill_bar_ts") or -1)
        is_mech = bool(p.get("mech"))
        hold_cap = int(p.get("max_hold") or MAX_HOLD_BARS)   # proven methods: 16 bars, as backtested
        # model management (VẮT KIỆT MODEL 2026-07-13): timestamped SL/TP adjustments apply only to
        # bars that OPEN at/after the decision — current-knowledge stops never rewrite past bars
        # (the lane-ratchet lookahead lesson). Each entry applies ONCE at its activation bar so the
        # mechanical BE/trail ratchet below can keep tightening on top of it. Deterministic replay.
        _mgmt = sorted((p.get("mgmt") or []), key=lambda m: int(m.get("ts") or 0)) if not is_mech else []
        _mi = 0
        _crq = int(p.get("close_req_ts") or 0) if not is_mech else 0
        for k, b in enumerate(fut):
            while _mi < len(_mgmt) and int(_mgmt[_mi].get("ts") or 0) <= int(b["ts_ms"]):
                if _mgmt[_mi].get("sl") is not None: sl = float(_mgmt[_mi]["sl"])
                if _mgmt[_mi].get("tp") is not None: tp = float(_mgmt[_mi]["tp"])
                _mi += 1
            if _crq and int(b["ts_ms"]) >= _crq:
                # model requested a market close: fill at the OPEN of the first bar starting after
                # the request. Codex: if that bar GAPPED past liquidation, the position was
                # liquidated BEFORE the voluntary close could fill — book liquidation, not llm_close.
                _bo = float(b.get("open") or b["close"])
                if (side == "LONG" and _bo <= liq_px) or (side == "SHORT" and _bo >= liq_px):
                    exit_px, reason = liq_px, "liquidation"
                else:
                    exit_px, reason = _bo, "llm_close"
                exit_ts = int(b["ts_ms"]); break
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
                try:
                    hit = lr.exit_check(b, side, liq_px, sl, tp)  # pessimistic: liq -> sl -> tp
                except Exception:
                    # data-flow audit 2026-07-08: a corrupt (NaN) liq_px/sl/tp on a legacy position
                    # makes exit_check raise, and this call is OUTSIDE resolve's fetch-try -> it would
                    # crash the whole batch (every open position unresolved that tick). Skip the bar;
                    # the position falls through to the timeout close instead of killing the loop.
                    hit = None
            if hit is not None:
                exit_px, reason = hit
                # a stop that has ratcheted to/above breakeven is a managed exit
                if reason == "sl" and ((side == "LONG" and sl >= entry * (1 + BE_BUF))
                                        or (side == "SHORT" and sl <= entry * (1 - BE_BUF))):
                    reason = "trail"
                exit_ts = int(b["ts_ms"]); break
            if is_mech and k + 1 >= hold_cap:
                # ONLY proven/mechanical methods time out — they were BACKTESTED to exit at N bars, so the
                # hold is part of their measured edge. A DISCRETIONARY trade must NOT close on a timer
                # (owner, repeatedly): a live futures trade rides to its SL or TP, it is not killed mid-move
                # by a clock. Non-mech positions simply stay open and are re-checked next cycle until SL/TP.
                exit_px, reason = float(b["close"]), "timeout"
                exit_ts = int(b["ts_ms"]); break
            if int(b["ts_ms"]) == fb_ts:
                # fill bar's HIGH/LOW may pre-date our fill — feeding it into the
                # trailing peak would arm a breakeven stop off a move we may never
                # have held through (optimistic leak, twin of the TP-off-fill-bar
                # bug). The ratchet starts from the NEXT bar.
                continue
            # P0 MFE/MAE — fill bar already skipped above (no-lookahead), so this bar's high/low is a
            # move we actually held through. Pure measurement; tracked for ALL positions before the
            # mech 'no-trailing' skip. try/except (Opus xhigh review): a malformed bar must NOT crash
            # the resolve() batch — this line is OUTSIDE the fetch-try, and for mech positions there is
            # no other OHLC deref, so an unguarded raise here would reopen the 2026-07-08 corrupt-bar hole.
            try:
                if side == "LONG":
                    mfe_px = max(mfe_px, float(b["high"]) - entry); mae_px = max(mae_px, entry - float(b["low"]))
                else:
                    mfe_px = max(mfe_px, entry - float(b["low"])); mae_px = max(mae_px, float(b["high"]) - entry)
            except (KeyError, TypeError, ValueError):
                pass
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
        elif reason in ("timeout", "llm_close"):     # plain market orders
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
        r = net / margin if margin > 0 else 0.0  # R vs margin risked (leverage-scaled; NOT the R:R multiple)
        # P0 LEARNING METRICS (measurement only — do not affect the account/scorecard). actual_R/predicted_R
        # are true R:R multiples (price move ÷ stop distance), the right units for calibration — unlike
        # `r` above which is return-on-margin. noise_stop vs thesis_wrong disambiguates the 75% stop-out rate.
        gain_px = (exit_px - entry) if side == "LONG" else (entry - exit_px)
        actual_R = round(gain_px / risk, 3) if risk > 0 else 0.0
        predicted_R = round(abs(tp - entry) / risk, 3) if risk > 0 else 0.0
        mfe_R = round(mfe_px / risk, 3) if risk > 0 else 0.0
        mae_R = round(mae_px / risk, 3) if risk > 0 else 0.0
        noise_stop = bool(net < 0 and mfe_R >= 1.0)    # offered >=1R profit, then wiggled to the stop
        thesis_wrong = bool(net < 0 and mfe_R < 0.5)   # went ~straight against the entry
        bars_held = int((exit_ts - int(p["entry_ts"])) / bar_ms) if bar_ms > 0 else 0
        acct["equity"] = round(float(acct["equity"]) + net, 4)
        acct["realized"] = round(float(acct["realized"]) + net, 4)
        acct["trades"] = int(acct["trades"]) + 1
        acct["wins"] = int(acct["wins"]) + (1 if net > 0 else 0)
        rec = {"symbol": p["symbol"], "side": side, "regime": p.get("regime"), "hour_utc": p.get("hour_utc"),
               "entry": entry, "exit": exit_px, "reason": reason, "net": round(net, 4), "r": round(r, 3),
               "fee": round(fee, 4), "funding": round(funding, 4), "liq_px": round(liq_px, 6), "tier": tier,
               "leverage": lev, "margin": round(margin, 4), "vol": p.get("vol"),
               "predicted_R": predicted_R, "actual_R": actual_R, "mfe_R": mfe_R, "mae_R": mae_R,  # P0 learning
               "noise_stop": noise_stop, "thesis_wrong": thesis_wrong, "bars_held": bars_held,
               "mech_method": p.get("mech_method"), "entry_feats": p.get("entry_feats"),
               "trigger_paths": p.get("trigger_paths"),   # R1: per-path expectancy is judged on THIS field
               "stage2": p.get("stage2"),                 # R2: second-look outcome rides to the ledger
               "model": p.get("model"), "pipeline_mode": p.get("pipeline_mode"),   # provenance (gap #5)
               "tide_at_entry": p.get("tide_at_entry"), "tide_aligned": p.get("tide_aligned"),
               "rationale": p.get("rationale"), "chart": p.get("chart"), "closed_ts": now_ms}
        try:    # owner feature: closed-trade chart with BUY/SELL markers
            b64 = ltc.render_trade_chart(p["symbol"], fb, side=side,
                                         entry_ts=int(p["entry_ts"]), entry_px=entry,
                                         exit_ts=exit_ts, exit_px=exit_px, reason=reason, tf=TF)
            if b64:
                import base64 as _b64
                cdir = LT_DIR / "charts_closed"
                cdir.mkdir(parents=True, exist_ok=True)
                fn = f"{p['symbol']}_{exit_ts}.png"
                (cdir / fn).write_bytes(_b64.b64decode(b64))
                rec["chart_exit"] = f"charts_closed/{fn}"
        except Exception as _ce:
            _append(LT_DIR / "governance.jsonl",
                    {"event": "chart_error", "symbol": p.get("symbol"), "error": repr(_ce)[:120]})
        _append(CLOSED, rec)
        _append(MEMORY, rec)   # self-learning: outcome tagged by context
        try:                    # second brain: mission closes feed lesson mining too
            import brain
            brain.record_mission_close({**rec, "entry_ts_ms": int(p.get("entry_ts") or 0),
                                        "closed_ts_ms": now_ms})
        except Exception as _bre:
            # never silent (Codex file-review #4): a lost autopsy row is lost
            # lesson evidence — surface it in governance so gaps are visible.
            _append(LT_DIR / "governance.jsonl",
                    {"event": "second_brain_error", "where": "record_mission_close",
                     "symbol": rec.get("symbol"), "error": repr(_bre)[:160]})
        closed_n += 1
    save_account(acct)
    _rewrite(POSITIONS, still)
    if closed_n:
        try:                     # lessons recompute on new MISSION evidence too —
            import brain         # without this, mining only ran on shadow closes
            brain.mine_lessons() # and mission-only days left lesson stats stale.
        except Exception:
            pass
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
                return _with_prop_syms(c["selected"])
    except Exception:
        pass
    ticks = client.futures_ticker()
    pool = sorted(
        [(t["symbol"], float(t.get("quoteVolume", 0) or 0)) for t in ticks
         if t.get("symbol", "").endswith("USDT") and "_" not in t["symbol"]
         and t["symbol"][:-4] not in UNIVERSE_EXCLUDE_BASES   # no tokenized stocks/commodities/stables
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
    return _with_prop_syms(selected)


def _write_daily_progress(now_ms: int) -> None:
    """P1: once per UTC day, snapshot the calibration KPIs to progress.jsonl. The TREND across these rows
    is the 'is the model actually learning' success metric. Best-effort — must NEVER affect trading."""
    import datetime
    prog = LT_DIR / "progress.jsonl"
    today = datetime.datetime.fromtimestamp(now_ms / 1000, datetime.timezone.utc).strftime("%Y-%m-%d")
    try:
        rows = _load(prog)
        if rows and rows[-1].get("date") == today:
            return                                  # already snapshotted today
    except Exception:
        pass
    try:
        import llm_trader_learning as ltl
        rep = ltl.calibration_report(_dedupe_closed(_load(CLOSED)))
        _append(prog, {"date": today, "ts": now_ms, "n": rep.get("n"), "win_rate": rep.get("win_rate"),
                       "mean_actual_R": rep.get("mean_actual_R"), "noise_stop_rate": rep.get("noise_stop_rate"),
                       "thesis_wrong_rate": rep.get("thesis_wrong_rate"), "over_optimism_R": rep.get("over_optimism_R"),
                       "verdict_hint": rep.get("verdict_hint")})
    except Exception as _pe:
        _append(LT_DIR / "governance.jsonl", {"event": "daily_progress_error", "error": repr(_pe)[:150]})


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
    try:
        # VẮT KIỆT MODEL: model manages its own open positions (HOLD/CLOSE/move SL-TP) every ~4min.
        _manage_positions_llm(client, now_ms)
    except Exception as _mge:
        _append(LT_DIR / "governance.jsonl",
                {"ts_ms": now_ms, "event": "llm_manage_error", "error": repr(_mge)[:160]})
    try:
        _hb({"phase": "post_manage"})     # mid-cycle heartbeat (Opus F1): manage adds a vision call
    except Exception:
        pass
    if pend_filled:
        acct = load_account(); equity = float(acct["equity"])
    card = refresh_scorecard(client)
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
        selected = _with_prop_syms(
            us.select_universe(client, end_ms=now_ms, months=1.0, timeframe="1h",
                               min_daily_quote_volume=UNIVERSE_MIN_QVOL,
                               max_symbols=UNIVERSE_MAX)["selected"])
    ctx = build_context(client, selected, now_ms)
    # R1 trigger engine — DARK measurement ONLY (plans/redesign_tin_va_chart_v1.md §5-R1): evaluates
    # which candidate-selection paths (news/whale/funding_oi/chart_align) each coin hits, logs the
    # cycle, and tags opened trades. Changes NO behavior — the per-path expectancy it accumulates on
    # live closes is what green-lights (or kills) the R2 gate. Fail-soft: errors are logged, never raised.
    trig_map: dict[str, Any] = {}
    try:
        import llm_trader_triggers as ltt
        trig_map = ltt.evaluate(ctx, ltt.read_news(_NEWS_PATH, now_ms),
                                oi_probe=lambda _s: ltt.probe_oi_slope(_s, now_ms))
        ltt.log_cycle(LT_DIR / "trigger_log.jsonl", now_ms, trig_map, len(ctx))
    except Exception as _te:
        _append(LT_DIR / "governance.jsonl",
                {"ts_ms": now_ms, "event": "trigger_engine_error", "error": repr(_te)[:150]})
    if PROVEN_ONLY:
        # the bleed fix: no discretionary entries — only lab-proven survivors fire.
        decisions = _mechanical_decisions(_non_tidalfi_ctx(ctx))
    elif REDESIGN:
        # R2 (owner-approved redesign): candidates are GATED to trigger-hit coins — no trigger, the
        # model never sees the coin as a trade option this cycle (selection in CODE, not prompt: the
        # model has proven it ignores prompt rules). trigger_info rides into the prompt so the model
        # knows WHY each coin is on the list. Then every actionable decision must survive the
        # STAGE-2 second look on its chosen TF. Mechanical/proven path is NOT gated (own governance).
        #
        # FLUSH-FIRST (shadow live-forward verdict 2026-07-12): per-path measured edge ranks the gate.
        #   flush_no_oi CONFIRMED live-positive (n_live>=30) > flush_oi_dn (positive, accruing) >
        #   chart_align (flat, unproven) > news/whale (rare, unmeasured).
        #   funding_extreme = NO-EDGE (n=50) -> DEAD path: no longer a reason to surface a coin.
        # A coin whose ONLY hit is a dead path is not a candidate; ranking puts measured-edge coins
        # first so the model and the STAGE2_MAX cap spend attention on them. Measurement is UNCUT:
        # log_cycle above already logged every path (incl. funding) before this gate.
        _PATH_RANK = {"flush_no_oi": 0, "flush_oi_dn": 1, "chart_align": 2, "news": 3, "whale": 3}
        _DEAD_PATHS = {"funding_extreme"}
        _EDGE_NOTE = {"flush_no_oi": "CONFIRMED live-positive edge: LONG the capitulation bounce (n_live>=30)",
                      "flush_oi_dn": "positive so far, live sample still accruing (n<25)",
                      "chart_align": "measured ~flat so far - no proven edge yet",
                      "news": "unmeasured (rare)", "whale": "unmeasured (rare)"}
        _ranked = []
        for c in ctx:
            _hit = trig_map.get(c.get("symbol")) or {}
            _live = [p for p in (_hit.get("paths") or []) if p not in _DEAD_PATHS]
            if not _live:
                continue                    # untriggered, or funding-only (dead path) -> not a candidate
            _info = dict(_hit.get("vals") or {})
            _info["_edge"] = {p: _EDGE_NOTE.get(p, "") for p in _live}
            c["trigger_info"] = _info
            _ranked.append((min(_PATH_RANK.get(p, 9) for p in _live), c))
        _ranked.sort(key=lambda rc: rc[0])          # stable: equal rank keeps ctx (hot-volume) order
        ctx_gated = [c for _r, c in _ranked]
        if ctx_gated:
            decisions = _apply_gap_veto(
                decide(ctx_gated, equity, status=status, client=client, now_ms=now_ms), ctx_gated)
            decisions = _stage2_confirm(decisions, client, now_ms)
        else:
            decisions = []   # no coin triggered -> no discretionary trades this cycle (by design)
        # MECHANICAL FLUSH (2026-07-13): the CONFIRMED path fires without asking the model or
        # stage-2 — runs regardless of what the model decided (dedup inside; gap-veto applied).
        try:
            _raw_flush = _flush_mech_decisions(_non_tidalfi_ctx(ctx), trig_map, decisions, now_ms)
            mech_flush = _apply_gap_veto(_raw_flush, ctx)
            # ADAPTIVE DE-RISK (2026-07-15 funnel forensic): capitulation candles are BY
            # DEFINITION huge bars, so the gap-veto at x10 blocked 34/43 flush fires in 16h
            # and starved the ONLY confirmed +edge path. Within the owner's mech band {5,10}:
            # a fire vetoed at x10 retries at x5 (liq distance 10%->20%, margin unchanged =>
            # notional HALVES — strictly safer). Still blocked at x5 = genuinely too wild.
            _kept = {d.get("symbol") for d in mech_flush}
            for _d in _raw_flush:
                if _d.get("symbol") in _kept or int(_d.get("leverage") or 0) <= 5:
                    continue
                _d5 = {**_d, "leverage": 5}
                if _apply_gap_veto([_d5], ctx):
                    _append(LT_DIR / "governance.jsonl",
                            {"ts_ms": now_ms, "event": "flush_mech_derisk_x5",
                             "symbol": _d.get("symbol")})
                    mech_flush.append(_d5)
            decisions = decisions + mech_flush
        except Exception as _fme:
            _append(LT_DIR / "governance.jsonl",
                    {"ts_ms": now_ms, "event": "flush_mech_error", "error": repr(_fme)[:160]})
        # LANE-PROMOTED methods fire mechanically in REDESIGN too (gate-v2 funnel output).
        # Without this the promotion file was WRITE-ONLY in this mode (Opus C1 — the exact
        # "wired but inert" disease the funnel redesign exists to cure). Scope: ONLY rows
        # tagged source=lane_promoted — hand-armed behavior in REDESIGN is unchanged (off).
        try:
            # funnel audit: the repo's STRONGEST validated method (wr_flush_notknife,
            # lockbox p=0.0002, mean_r 1.156) could never fire in REDESIGN — the active
            # mode since 07-10. It now rides the same guarded mech path as lane_promoted
            # (gap-veto, episode memo 2h, concurrent cap 3, symbol dedup).
            # capitulation_long stays DARK: owner culled its lane (18.8% win, n=16) and
            # the flush-mech path already owns the capitulation-LONG side.
            _lp = [m for m in _survivor_methods()
                   if m.get("source") == "lane_promoted" or m.get("id") == "wr_flush_notknife"]
            if _lp:
                # churn guards (audit#2 F3 — parity with flush-mech, which got these from a
                # Codex CRITICAL): a still-true condition re-fires every 90s cycle after a
                # 1-bar SL, serially bleeding sl x lev. Dedup vs open+pending, 2h episode
                # memo per (symbol, method), and a concurrent-open cap of 3.
                _have = {d.get("symbol") for d in decisions}
                _have |= {p.get("symbol") for p in _load(POSITIONS)}
                _have |= {q.get("symbol") for q in _load(PENDING)}
                _ep_f = LT_DIR / "lane_mech_episodes.json"
                try:
                    _eps = json.loads(_ep_f.read_text(encoding="utf-8"))
                    _eps = _eps if isinstance(_eps, dict) else {}
                except Exception:
                    _eps = {}
                _lp_ids = {m["id"] for m in _lp}
                _n_open = sum(1 for p in _load(POSITIONS) if p.get("mech_method") in _lp_ids)
                mech_lane = []
                for d in _apply_gap_veto(_mechanical_decisions(_non_tidalfi_ctx(ctx), methods=_lp), ctx):
                    _k = f"{d.get('symbol')}|{d.get('_mech_method') or ''}"
                    if (d.get("symbol") in _have or _n_open + len(mech_lane) >= 3
                            or now_ms - int(_eps.get(_k) or 0) < 2 * 3600 * 1000):
                        continue
                    _eps[_k] = now_ms
                    mech_lane.append(d)
                if mech_lane:
                    _eps = dict(sorted(_eps.items(), key=lambda kv: -kv[1])[:200])
                    _tmp = _ep_f.with_suffix(".tmp")
                    _tmp.write_text(json.dumps(_eps), encoding="utf-8")
                    os.replace(_tmp, _ep_f)
                    _append(LT_DIR / "governance.jsonl",
                            {"ts_ms": now_ms, "event": "lane_promoted_fire",
                             "symbols": [d.get("symbol") for d in mech_lane]})
                decisions = decisions + mech_lane
        except Exception as _lpe:
            _append(LT_DIR / "governance.jsonl",
                    {"ts_ms": now_ms, "event": "lane_promoted_mech_error", "error": repr(_lpe)[:160]})
    else:
        # DISCRETIONARY: gpt-5.5 vision reads the charts and decides. Loosened from PROVEN_ONLY so the
        # strong model actually trades — but the gap-tail RUIN veto still applies (bughunt LLM#1),
        # because "don't get liquidated on a gap" is safety, not rigidity.
        decisions = _apply_gap_veto(decide(ctx, equity, status=status, client=client, now_ms=now_ms), ctx)
    # PROVENANCE STAMP (gap #5): every decision carries WHICH brain + mode + tide made it, so any
    # future "does it have edge?" analysis can split cleanly by model era / pipeline / tide-alignment
    # instead of confounding 3 model generations. A few strings now unlocks retroactive A/B forever.
    _tide = getattr(_btc_context_chart, "_tide", None) if not PROVEN_ONLY else None
    _mode = "PROVEN_ONLY" if PROVEN_ONLY else ("REDESIGN" if REDESIGN else "DISCRETIONARY")
    for _d in decisions:   # tag which trigger paths the coin hit THIS cycle (measurement metadata)
        try:
            _d["_trigger_paths"] = (trig_map.get(_d.get("symbol")) or {}).get("paths")
            _d["_model"] = MODEL
            _d["_pipeline_mode"] = _mode
            _d["_tide_at_entry"] = (_tide or {}).get("tide") if isinstance(_tide, dict) else None
            _side = str(_d.get("action") or "").upper()
            _t = (_tide or {}).get("tide") if isinstance(_tide, dict) else None
            _d["_tide_aligned"] = (None if _t in (None, "flat") else
                                   (_side == "LONG") == (_t == "pumping"))
        except Exception:
            pass
    opened = open_positions(decisions, equity, utc_now(), now_ms=now_ms)
    try:
        # CÁ VOI TẬP SỰ: entry signals + exit notifications + /status command, one tick per cycle
        # (dedup + prop-safe sizing + daily-cap warning inside). Dark until token configured; a
        # Telegram error must NEVER touch the trading loop.
        import whale_signal

        def _ws_status():
            import time as _t
            _rows = _dedupe_closed(_load(CLOSED))
            est, w, l = whale_signal._prop_day_est(_rows[-120:], _t.time())
            _wr = whale_signal._winrates()
            _op = _load(POSITIONS)
            _ol = "\n".join(f"  • {str(p.get('symbol') or '').replace('USDT','')} {p.get('side')}"
                            f" ({p.get('mech_method') or 'model'})" for p in _op) or "  (không có)"
            return ("🐋 <b>CÁ VOI TẬP SỰ — STATUS</b>\n"
                    f"💼 Paper: <b>${acct['equity']:.2f}</b> · {acct['trades']} lệnh\n"
                    f"📅 Prop hôm nay ước tính: <b>{'+' if est >= 0 else ''}{est:,.0f}$</b>"
                    f" ({w}W/{l}L) · trần −$200\n"
                    f"📈 win flush {_wr.get('flush') or '—'} · 30 lệnh gần {_wr.get('recent') or '—'}\n"
                    f"📌 Đang mở:\n{_ol}")

        whale_signal.tick(_load(POSITIONS), _dedupe_closed(_load(CLOSED))[-120:],
                          _load(PENDING), _ws_status)
    except Exception as _wse:
        _append(LT_DIR / "governance.jsonl", {"ts_ms": now_ms, "event": "whale_signal_error", "error": repr(_wse)[:150]})
    try:
        _write_daily_progress(now_ms)   # P1 KPI snapshot; best-effort, never blocks the cycle
    except Exception:
        pass
    wr = round(acct["wins"] / acct["trades"], 3) if acct["trades"] else None
    return {"equity": acct["equity"], "trades": acct["trades"], "win_rate": wr,
            "opened": opened, "resolved": resolved, "open": len(_load(POSITIONS)),
            "considered": len(ctx), "acted": len(decisions), "model": MODEL,
            "mode": "PROVEN_ONLY" if PROVEN_ONLY else ("REDESIGN" if REDESIGN else "DISCRETIONARY"),
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
                # BUG FIX 2026-07-08: mtime-only 'fresh' check blocked for 600s after a dead
                # pythonw spawn left a recent-mtime lock — the supervisor re-spawned every cycle,
                # refreshing it -> PERMANENT self-block (mission couldn't start for hours). Only
                # refuse if the lock-owner PID is actually ALIVE (Windows-safe OpenProcess, since
                # os.kill(pid,0) TERMINATES on Windows). A dead owner's lock is stale -> proceed.
                _owner_alive = True
                try:
                    _opid = int((lock.read_text(encoding="ascii") or "0").strip())
                    if _opid > 0:
                        import ctypes as _ct
                        _h = _ct.windll.kernel32.OpenProcess(0x1000, False, _opid)  # QUERY_LIMITED
                        if _h:
                            # Opus review: OpenProcess success alone treats a REUSED pid as
                            # "owner alive" -> new loop exits 1 each cycle -> circuit breaker
                            # -> 6h mission quarantine. Require STILL_ACTIVE + a python image.
                            _code = _ct.c_ulong(0)
                            _ok = _ct.windll.kernel32.GetExitCodeProcess(_h, _ct.byref(_code))
                            _alive = bool(_ok) and _code.value == 259          # STILL_ACTIVE
                            if _alive:
                                _buf = _ct.create_unicode_buffer(512)
                                _sz = _ct.c_ulong(512)
                                if _ct.windll.kernel32.QueryFullProcessImageNameW(
                                        _h, 0, _buf, _ct.byref(_sz)):
                                    _alive = "python" in _buf.value.lower()    # reused pid != our loop
                            _ct.windll.kernel32.CloseHandle(_h)
                            _owner_alive = _alive
                        else:
                            _owner_alive = False        # PID not found -> dead -> stale lock
                    else:
                        _owner_alive = False
                except Exception:
                    _owner_alive = True                 # can't tell -> fail-safe: assume alive
                if _owner_alive:
                    print(json.dumps({"error": "another llm_trader loop is active (loop.lock fresh, pid alive)"}))
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
