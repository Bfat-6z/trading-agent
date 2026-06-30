"""
Autonomous futures watch loop — bot can LONG or SHORT based on agent consensus.

Differences from spot watch_mode.py:
- Scans Binance USDT-M perpetual futures (much bigger universe)
- Agent verdict mapping:
    - consensus=bullish + risk=proceed/reduce_size  -> OPEN LONG
    - consensus=bearish + risk=proceed/reduce_size  -> OPEN SHORT
    - risk=abort  -> NO TRADE
- Leverage scaled by risk persona:
    - aggressive view weight high  -> 8x
    - neutral                      -> 5x
    - conservative                 -> 3x
- SL/TP via STOP_MARKET / TAKE_PROFIT_MARKET reduce-only orders set on Binance side
"""
from __future__ import annotations

# PHASE00_LEGACY_DIRECT_RUN_GUARD
if __name__ == "__main__":
    from legacy_live_blocker import block_file_if_legacy as _phase00_block_file
    _phase00_block_file(__file__, "direct_exec")

import math
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

from binance.exceptions import BinanceAPIException

from tradingagents.binance.client import spot_client
from tradingagents.binance import futures as bf
from tradingagents.binance.data import fetch_binance_snapshot
from tradingagents.crypto import agents as ag

# ---------- Config ----------
INTERVAL_MIN = int(os.environ.get("FUTURES_WATCH_INTERVAL_MIN", "5"))   # longer interval = less Kiro hammering
MARGIN_USD = float(os.environ.get("FUTURES_MARGIN_USD", "1.5"))      # default desired margin
MIN_MARGIN_USD = float(os.environ.get("FUTURES_MIN_MARGIN_USD", "1.0"))  # minimum allowable
DEFAULT_LEVERAGE = int(os.environ.get("FUTURES_DEFAULT_LEVERAGE", "5"))
MAX_OPEN_POSITIONS = int(os.environ.get("FUTURES_MAX_POSITIONS", "2"))
DAILY_LOSS_LIMIT = float(os.environ.get("DAILY_LOSS_LIMIT_USD", "2.0"))
TOP_K = 60              # analyze ALL candidates from scan (was 12, now no cap)
MAX_UNIVERSE_SIZE = 60  # universe cap stays at 60
MIN_VOL_M = 5           # $M — much lower bar
SL_PCT = 5.0            # tight SL on futures (leverage amplifies)
TP1_PCT = 5.0
TP2_PCT = 10.0
MAX_24H_GAIN_PCT = 22
MAX_24H_LOSS_PCT = -25
PARALLEL_ANALYSIS = 1   # sequential — let Kiro breathe between candidates
WATCHLIST_TTL_MIN = 60      # cache lifetime
RECHECK_AFTER_MIN = 60      # force re-analyze cached entry if older than this (regardless of price)
PRICE_MOVE_RECHECK_PCT = 3.0   # also re-analyze if price moved this % since last analysis

# Entry gating v2 — layered design, see plans/entry_gating_v2.md
# Layer 2: conviction threshold (raised from 0.55 — borderline signals are coin flips)
CONVICTION_THRESHOLD = 0.65

# Layer 3: volatility-normalized momentum cap (ch24 / 1D_ATR%)
MOMENTUM_ATR_MULT_CAP = 3.0     # block if 24h move > 3x daily ATR

# Layer 5 (safety net): hard ch24 caps — kept as defense in depth
MAX_LONG_24H_GAIN_PCT = 12      # block LONG if 24h gain > +12%
MAX_SHORT_24H_LOSS_PCT = -15    # block SHORT if 24h loss > -15%

EXCLUDE_BASES = {
    # Stablecoins / fiat
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "EUR",
    # TradFi-Perps (require separate Binance agreement, error -4411)
    "XAU", "XAG",                                                       # metals
    "NVDA", "TSLA", "AAPL", "MSFT", "GOOGL", "GOOG", "META", "AMZN",   # mega-caps
    "NFLX", "INTC", "AMD", "INTU", "CRM", "ORCL", "DIS",                # tech/services
    "JPM", "BAC", "V", "MA", "KO", "PEP", "WMT", "MCD", "HD", "NKE",    # consumer/finance
    "BA", "GE", "F", "GM",                                              # industrial
    "SOXL", "SOXX", "QQQ", "SPY", "IWM", "INX", "TQQQ", "SQQQ", "UVXY", # ETFs/index
    "GLD", "SLV", "USO", "TLT",                                         # commodity ETFs
    "SNDK", "VVV", "MSTR", "COIN", "HOOD", "RIOT", "MARA", "SQ",        # crypto-adjacent stocks
}

# Runtime blacklist file — auto-populated when -4411 (TradFi agreement) hits
import json as _json
_TRADFI_BL_FILE = "state/tradfi_blacklist.json"
def _load_runtime_blacklist() -> set:
    try:
        with open(_TRADFI_BL_FILE, "r") as f:
            return set(_json.load(f))
    except Exception:
        return set()
def _save_runtime_blacklist(bl: set) -> None:
    try:
        import os as _os
        _os.makedirs("state", exist_ok=True)
        with open(_TRADFI_BL_FILE, "w") as f:
            _json.dump(sorted(bl), f, indent=2)
    except Exception:
        pass
EXCLUDE_BASES |= _load_runtime_blacklist()

# ---------- Watchlist cache ----------
# {symbol: {analyzed_at: timestamp, last_price, debate_consensus, risk_recommendation}}
_watchlist: dict = {}


def _watchlist_prune():
    """Remove stale entries older than WATCHLIST_TTL_MIN."""
    cutoff = time.time() - WATCHLIST_TTL_MIN * 60
    stale = [s for s, info in _watchlist.items() if info["analyzed_at"] < cutoff]
    for s in stale:
        del _watchlist[s]
    return len(stale)


def _watchlist_remember(symbol: str, snap, debate, risk, action=None, leverage=0,
                         ch24=None, atr_1d_pct=None, tv_snapshot=None):
    """Save analysis result + decided action to watchlist.
    tv_snapshot is the dict from _tv_snapshot_from_data (used by Layer 6 cached re-validation)."""
    _watchlist[symbol] = {
        "analyzed_at": time.time(),
        "last_price": snap.price_usd,
        "debate_consensus": debate.consensus,
        "debate_strength": debate.consensus_strength,
        "risk_recommendation": risk.recommendation,
        "risk_score": risk.risk_score,
        "action": action,         # "LONG" / "SHORT" / None
        "leverage": leverage,
        "ch24_at_analysis": ch24,
        "atr_1d_at_analysis": atr_1d_pct,
        "tv_state_at_analysis": tv_snapshot,
    }


def _find_executable_cached(current_tickers: dict) -> tuple | None:
    """Look through cache for a still-valid SHORT/LONG signal.
    Returns (symbol, action, leverage, current_price) or None.
    Valid means: cached <5min ago AND price hasn't moved against signal >2%."""
    candidates = []
    now = time.time()
    for sym, info in _watchlist.items():
        if info.get("action") not in ("LONG", "SHORT"):
            continue
        age_min = (now - info["analyzed_at"]) / 60
        if age_min > 5:
            continue
        # Price still in good zone?
        cur_price = current_tickers.get(sym)
        if not cur_price:
            continue
        last = info["last_price"]
        if last <= 0:
            continue
        move_pct = (cur_price - last) / last * 100
        # For SHORT signal: bad if price moved UP >2% (entry got worse)
        # For LONG signal: bad if price moved DOWN >2%
        if info["action"] == "SHORT" and move_pct > 2:
            continue
        if info["action"] == "LONG" and move_pct < -2:
            continue
        # Layer 5: hard ch24 safety net
        ch24 = info.get("ch24_at_analysis")
        if ch24 is not None:
            if info["action"] == "LONG" and ch24 > MAX_LONG_24H_GAIN_PCT:
                continue
            if info["action"] == "SHORT" and ch24 < MAX_SHORT_24H_LOSS_PCT:
                continue
        # Layer 6: re-validate cached TV state against current entry rules
        tv_state = info.get("tv_state_at_analysis")
        l1_ok, l1_reason = _tv_confirms_from_state(tv_state, info["action"])
        if not l1_ok:
            _audit_log_gate(sym, info["action"], {"cache": (False, f"L1_revalidate_{l1_reason}")}, "SKIPPED_CACHED")
            continue
        # Layer 3 re-check with cached ATR
        atr_1d = info.get("atr_1d_at_analysis")
        l3_ok, l3_reason = _momentum_normal_ok(ch24 or 0, atr_1d, info["action"])
        if not l3_ok:
            _audit_log_gate(sym, info["action"], {"cache": (False, f"L3_revalidate_{l3_reason}")}, "SKIPPED_CACHED")
            continue
        candidates.append((sym, info["action"], info["leverage"], cur_price,
                          info["debate_strength"], age_min, move_pct))

    if not candidates:
        return None
    # Pick highest strength
    candidates.sort(key=lambda x: x[4], reverse=True)
    top = candidates[0]
    return top  # (sym, action, lev, price, strength, age_min, move_pct)


# ============================================================
# Entry gating v2 helpers — see plans/entry_gating_v2.md
# ============================================================

def _tv_confirms_from_state(tv_state: dict | None, action: str) -> tuple[bool, str]:
    """Layer 1 logic applied to a TV state dict (live or cached snapshot).
    tv_state shape: {h1_rsi, h4_rsi, h4_ema_dist, h1_macd_hist, h4_macd_hist,
                     h1_adx, h1_di_plus, h1_di_minus}
    Returns (passes, reason). Fail-open if tv_state is None or missing keys."""
    if not tv_state:
        return True, "tv_unavailable_failopen"
    required = ("h1_rsi", "h4_rsi", "h4_ema_dist", "h1_macd_hist", "h4_macd_hist",
                "h1_adx", "h1_di_plus", "h1_di_minus")
    if any(tv_state.get(k) is None for k in required):
        return True, "tv_partial_failopen"
    if action == "LONG":
        if tv_state["h1_rsi"] > 75: return False, f"1h_rsi_{tv_state['h1_rsi']:.0f}>75"
        if tv_state["h4_rsi"] > 78: return False, f"4h_rsi_{tv_state['h4_rsi']:.0f}>78"
        if tv_state["h4_ema_dist"] > 10: return False, f"4h_ema_dist_{tv_state['h4_ema_dist']:.1f}>10"
        if tv_state["h4_macd_hist"] < 0 and tv_state["h1_macd_hist"] < 0:
            return False, "macd_bear_both_tfs"
        if tv_state["h1_adx"] > 50 and tv_state["h1_di_minus"] > tv_state["h1_di_plus"]:
            return False, "downtrend_developing"
    else:  # SHORT
        if tv_state["h1_rsi"] < 25: return False, f"1h_rsi_{tv_state['h1_rsi']:.0f}<25"
        if tv_state["h4_rsi"] < 22: return False, f"4h_rsi_{tv_state['h4_rsi']:.0f}<22"
        if tv_state["h4_ema_dist"] < -10: return False, f"4h_ema_dist_{tv_state['h4_ema_dist']:.1f}<-10"
        if tv_state["h4_macd_hist"] > 0 and tv_state["h1_macd_hist"] > 0:
            return False, "macd_bull_both_tfs"
        if tv_state["h1_adx"] > 50 and tv_state["h1_di_plus"] > tv_state["h1_di_minus"]:
            return False, "uptrend_developing"
    return True, "ok"


def _tv_snapshot_from_data(tv_data: dict | None) -> dict | None:
    """Extract the fields _tv_confirms_from_state needs from a fetch_tv_multi_tf result."""
    if not tv_data:
        return None
    tfs = tv_data.get("timeframes", {})
    h1 = tfs.get("1h", {})
    h4 = tfs.get("4h", {})
    if not (h1.get("available") and h4.get("available")):
        return None
    return {
        "h1_rsi": h1.get("rsi"),
        "h4_rsi": h4.get("rsi"),
        "h4_ema_dist": h4.get("price_vs_ema20_pct"),
        "h1_macd_hist": h1.get("macd_hist"),
        "h4_macd_hist": h4.get("macd_hist"),
        "h1_adx": h1.get("adx"),
        "h1_di_plus": h1.get("di_plus"),
        "h1_di_minus": h1.get("di_minus"),
    }


def _tv_confirms_live(symbol: str, action: str) -> tuple[bool, str, dict | None]:
    """Layer 1: pull live TV (60s cached) and apply rules.
    Returns (passes, reason, tv_state_snapshot_or_None)."""
    try:
        from tradingagents.crypto.tv_data import fetch_tv_multi_tf
        tv_data = fetch_tv_multi_tf(symbol)
    except Exception:
        return True, "tv_fetch_exc_failopen", None
    snapshot = _tv_snapshot_from_data(tv_data)
    if snapshot is None:
        return True, "tv_unavailable_failopen", None
    ok, reason = _tv_confirms_from_state(snapshot, action)
    return ok, reason, snapshot


def _momentum_normal_ok(ch24: float, atr_1d_pct: float | None, action: str) -> tuple[bool, str]:
    """Layer 3: ch24 normalized by daily ATR. Fail-open if ATR unavailable."""
    if not atr_1d_pct or atr_1d_pct <= 0:
        return True, "atr_missing_failopen"
    mult = ch24 / atr_1d_pct
    if action == "LONG" and mult > MOMENTUM_ATR_MULT_CAP:
        return False, f"momentum_{mult:.1f}x_atr>{MOMENTUM_ATR_MULT_CAP}"
    if action == "SHORT" and mult < -MOMENTUM_ATR_MULT_CAP:
        return False, f"momentum_{mult:.1f}x_atr<-{MOMENTUM_ATR_MULT_CAP}"
    return True, "ok"


def _audit_log_gate(symbol: str, action: str, layers: dict, final: str) -> None:
    """Append a single line to state/entry_gates.log.
    layers shape: {"L1": (ok, reason), "L2": (ok, reason), ...}"""
    try:
        gates_dir = ROOT / "state"
        gates_dir.mkdir(exist_ok=True)
        gates_path = gates_dir / "entry_gates.log"
        parts = [f"{n}={'ok' if ok else 'BLOCK'}({reason})"
                 for n, (ok, reason) in sorted(layers.items())]
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {symbol:<14} {action:<5} {' '.join(parts)} -> {final}\n"
        with gates_path.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _watchlist_needs_recheck(symbol: str, current_price: float) -> tuple[bool, str]:
    """Re-analyze if price moved significantly OR cache entry is stale (>60min).
    Returns (should_recheck, reason)."""
    info = _watchlist.get(symbol)
    if not info:
        return True, "NEW"
    age_min = (time.time() - info["analyzed_at"]) / 60
    if age_min >= RECHECK_AFTER_MIN:
        return True, f"STALE({age_min:.0f}min)"
    last = info["last_price"]
    if last <= 0:
        return True, "BAD_PRICE"
    move_pct = abs(current_price - last) / last * 100
    if move_pct >= PRICE_MOVE_RECHECK_PCT:
        return True, f"MOVED({move_pct:+.2f}%)"
    return False, f"STABLE({move_pct:+.2f}%)"


# ---------- Logging ----------
_LOG_FILE = ROOT / "state" / "futures_watch.log"
_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
_log_fh = open(_LOG_FILE, "a", encoding="utf-8", buffering=1)


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        _log_fh.write(line + "\n")
    except Exception:
        pass


# ---------- Helpers ----------

def kill_switch_active() -> bool:
    return (ROOT / "state" / "kill_switch").exists()


def get_futures_usdt() -> float:
    try:
        bal = bf.get_futures_balance()
        return bal["available"]
    except Exception:
        return 0.0


def scan_futures_movers(top_k: int) -> list[dict]:
    """Scan USDT-M perpetuals, rank by score."""
    c = spot_client()
    try:
        tickers = c.futures_ticker()
    except Exception as e:
        log(f"  futures_ticker error: {e}")
        return []

    candidates = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if base in EXCLUDE_BASES:
            continue
        try:
            vol_m = float(t.get("quoteVolume", 0)) / 1e6
            ch = float(t.get("priceChangePercent", 0))
            cnt = int(t.get("count", 0))
            if vol_m < MIN_VOL_M or cnt < 5000:
                continue
            # Reject overheated/falling-knife extremes
            if ch > MAX_24H_GAIN_PCT or ch < MAX_24H_LOSS_PCT:
                continue
            high = float(t["highPrice"])
            low = float(t["lowPrice"])
            price = float(t["lastPrice"])
            rng_pos = (price - low) / (high - low) if high > low else 0.5
            # Score: favor balanced setups risk debaters will approve
            # CAP volume score at $20M+ so mid-caps aren't penalized by liquidity bias.
            #   - oversold bounce (price -10 to -3%, recovered off low) -> LONG
            #   - mild momentum (+3 to +8%, mid-range, vol rising)       -> LONG
            #   - moderate distribution (+10 to +18%, top of range)     -> SHORT candidate
            vol_for_score = min(vol_m, 20.0)   # cap at $20M for scoring purposes
            log_v = math.log10(max(vol_for_score * 1e6, 1)) / 7.5
            # Layer 4 (entry_gating_v2): tightened bounds — see plans/entry_gating_v2.md
            # FIGHT loss 2026-05-21 happened because oversold_bounce_LONG had no rng_pos upper bound.
            regime = 0.4
            setup_label = "other"
            if -12 <= ch <= -3 and 0.45 < rng_pos < 0.85:        # upper bound 0.85 added
                regime, setup_label = 1.6, "oversold_bounce_LONG"
            elif 3 <= ch <= 8 and 0.3 < rng_pos < 0.65:          # tighter upper bound 0.65
                regime, setup_label = 1.5, "healthy_momentum_LONG"
            elif 8 < ch <= 14 and 0.5 < rng_pos < 0.75:          # new band
                regime, setup_label = 1.3, "momentum_continuation_LONG"
            elif 10 <= ch <= 18 and rng_pos > 0.75:
                regime, setup_label = 1.2, "mild_overbought_SHORT"
            elif 18 < ch <= 30 and rng_pos > 0.80:               # new band
                regime, setup_label = 1.4, "exhaustion_SHORT"
            elif -3 < ch < 3:
                regime, setup_label = 1.0, "consolidation"
            candidates.append({
                "symbol": sym, "base": base, "score": log_v * regime,
                "price": price, "ch24": ch, "vol_m": vol_m, "rng_pos": rng_pos,
                "setup": setup_label,
            })
        except Exception:
            continue

    # Stratified sampling by volume bucket to give mid/small caps representation
    LARGE_THRESHOLD = 100   # $M+ = large cap
    MID_THRESHOLD = 20      # $20-100M = mid cap
    # else = small ($5-20M)

    large = [c for c in candidates if c["vol_m"] >= LARGE_THRESHOLD]
    mid = [c for c in candidates if MID_THRESHOLD <= c["vol_m"] < LARGE_THRESHOLD]
    small = [c for c in candidates if c["vol_m"] < MID_THRESHOLD]

    for bucket in (large, mid, small):
        bucket.sort(key=lambda x: x["score"], reverse=True)

    # Allocate slots: 40% large / 40% mid / 20% small
    n_large = max(1, int(top_k * 0.4))
    n_mid = max(1, int(top_k * 0.4))
    n_small = max(1, top_k - n_large - n_mid)

    picked = large[:n_large] + mid[:n_mid] + small[:n_small]
    # If a bucket is short, fill from remaining sorted by score
    if len(picked) < top_k:
        seen = {c["symbol"] for c in picked}
        remaining = [c for c in candidates if c["symbol"] not in seen]
        remaining.sort(key=lambda x: x["score"], reverse=True)
        picked.extend(remaining[:top_k - len(picked)])

    # Final sort by score for readability
    picked.sort(key=lambda x: x["score"], reverse=True)
    return picked


def run_pipeline(symbol: str) -> dict | None:
    """Full 16-call agent pipeline."""
    try:
        snap = fetch_binance_snapshot(symbol)
    except Exception as e:
        log(f"  {symbol}: snapshot fail ({e})")
        return None

    analyst_fns = [
        ag.agent_market,
        ag.agent_onchain,
        lambda s: ag.agent_liquidity(s, MARGIN_USD * DEFAULT_LEVERAGE),
        ag.agent_sentiment,
        ag.agent_news,
        lambda s: ag.agent_tv_technicals(s, binance_symbol=symbol),
    ]
    analysts = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(fn, snap) for fn in analyst_fns]
        for f in futs:
            try:
                analysts.append(f.result(timeout=60))
            except Exception:
                continue
    if not analysts:
        return None

    debate = ag.debate_round(snap, analysts, num_rounds=2)
    # Risk debate (uses 0 open positions placeholder — we track separately for futures)
    open_count = 0
    daily_pnl = 0.0
    risk = ag.risk_debate(snap, debate, open_count, daily_pnl, MARGIN_USD)
    return {
        "symbol": symbol, "snap": snap, "debate": debate, "risk": risk,
        "analysts": analysts,
    }


def decide_action(debate, risk, symbol: str | None = None, ch24: float | None = None,
                   atr_1d_pct: float | None = None) -> tuple[str | None, int, dict, dict | None]:
    """Map agent verdict to futures action through layered gating.
    Returns (action, leverage, audit_layers, tv_snapshot).
    See plans/entry_gating_v2.md for layer design."""
    audit: dict[str, tuple[bool, str]] = {}
    tv_snapshot: dict | None = None

    if risk.recommendation == "abort":
        audit["L0"] = (False, "risk_abort")
        return None, 0, audit, None
    audit["L0"] = (True, f"risk_{risk.recommendation}")

    leverage = DEFAULT_LEVERAGE
    if risk.recommendation == "proceed":
        leverage = 7
    elif risk.recommendation == "reduce_size":
        leverage = 3

    # Layer 2: conviction threshold
    if debate.consensus == "bullish":
        action_proposed = "LONG"
    elif debate.consensus == "bearish":
        action_proposed = "SHORT"
    else:
        audit["L2"] = (False, f"neutral_consensus_{debate.consensus_strength:.2f}")
        return None, 0, audit, None
    if debate.consensus_strength < CONVICTION_THRESHOLD:
        audit["L2"] = (False, f"strength_{debate.consensus_strength:.2f}<{CONVICTION_THRESHOLD}")
        return None, 0, audit, None
    audit["L2"] = (True, f"strength_{debate.consensus_strength:.2f}")

    # Layer 1: TV multi-TF confirmation (live pull, 60s cached)
    if symbol:
        l1_ok, l1_reason, tv_snapshot = _tv_confirms_live(symbol, action_proposed)
        audit["L1"] = (l1_ok, l1_reason)
        if not l1_ok:
            return None, 0, audit, tv_snapshot
    else:
        audit["L1"] = (True, "no_symbol_skipped")

    # Layer 3: ATR-normalized momentum
    if ch24 is not None:
        l3_ok, l3_reason = _momentum_normal_ok(ch24, atr_1d_pct, action_proposed)
        audit["L3"] = (l3_ok, l3_reason)
        if not l3_ok:
            return None, 0, audit, tv_snapshot
    else:
        audit["L3"] = (True, "no_ch24_skipped")

    # Layer 5: hard ch24 safety net
    if ch24 is not None:
        if action_proposed == "LONG" and ch24 > MAX_LONG_24H_GAIN_PCT:
            audit["L5"] = (False, f"ch24_{ch24:.1f}>{MAX_LONG_24H_GAIN_PCT}")
            return None, 0, audit, tv_snapshot
        if action_proposed == "SHORT" and ch24 < MAX_SHORT_24H_LOSS_PCT:
            audit["L5"] = (False, f"ch24_{ch24:.1f}<{MAX_SHORT_24H_LOSS_PCT}")
            return None, 0, audit, tv_snapshot
    audit["L5"] = (True, "ok")

    return action_proposed, leverage, audit, tv_snapshot


def get_open_positions() -> list[dict]:
    """Query current Binance futures positions."""
    c = spot_client()
    try:
        positions = c.futures_position_information()
        return [p for p in positions if abs(float(p.get("positionAmt", 0))) > 0]
    except Exception as e:
        log(f"  position fetch error: {e}")
        return []


def open_position(symbol: str, action: str, leverage: int, mark_price: float) -> dict | None:
    """Open LONG or SHORT futures position with dynamic margin sizing."""
    # Dynamic margin: use MARGIN_USD if available, else available balance - buffer
    bal = get_futures_usdt()
    margin = min(MARGIN_USD, bal - 0.10)
    if margin < MIN_MARGIN_USD:
        log(f"  {symbol}: insufficient available ${bal:.2f} (need >= ${MIN_MARGIN_USD + 0.10})")
        return None
    # Bump leverage if margin smaller to maintain min $5 notional
    if margin * leverage < 5.1:
        leverage = max(leverage, int(5.5 / margin) + 1)
    log(f"  {symbol}: margin=${margin:.2f}  lev={leverage}x  notional=${margin * leverage:.2f}")
    try:
        if action == "LONG":
            res = bf.open_long(symbol, margin, leverage=leverage, isolated=True)
        else:
            res = bf.open_short(symbol, margin, leverage=leverage, isolated=True)

        log(f"  OPENED {action} {symbol}: qty={res.executed_qty} avgPrice=${res.avg_price}")

        # Set SL + TP via reduce-only orders
        entry = res.avg_price if res.avg_price > 0 else mark_price
        if action == "LONG":
            sl_price = entry * (1 - SL_PCT / 100)
            tp1_price = entry * (1 + TP1_PCT / 100)
            tp2_price = entry * (1 + TP2_PCT / 100)
        else:
            sl_price = entry * (1 + SL_PCT / 100)
            tp1_price = entry * (1 - TP1_PCT / 100)
            tp2_price = entry * (1 - TP2_PCT / 100)

        try:
            bf.place_stop_loss(symbol, sl_price, side_to_close=action)
            log(f"    SL @ ${sl_price:.6f}")
        except Exception as e:
            log(f"    SL setup failed: {e}")

        try:
            bf.place_take_profit(symbol, tp2_price, side_to_close=action)
            log(f"    TP2 @ ${tp2_price:.6f}")
        except Exception as e:
            log(f"    TP2 setup failed: {e}")

        return {"symbol": symbol, "action": action, "entry": entry,
                "sl": sl_price, "tp2": tp2_price}
    except Exception as e:
        err_str = str(e)
        log(f"  OPEN FAILED ({action} {symbol}): {err_str}")
        # Auto-blacklist TradFi-Perps that need agreement signing
        if "-4411" in err_str or "TradFi" in err_str:
            base = symbol[:-4] if symbol.endswith("USDT") else symbol
            EXCLUDE_BASES.add(base)
            bl = _load_runtime_blacklist()
            bl.add(base)
            _save_runtime_blacklist(bl)
            log(f"  → Auto-blacklisted {base} (TradFi-Perp, agreement not signed)")
        return None


def main_loop():
    log(f"Futures watch mode started.")
    log(f"  Interval: {INTERVAL_MIN}min  Margin: ${MARGIN_USD}  Default leverage: {DEFAULT_LEVERAGE}x")
    log(f"  Max positions: {MAX_OPEN_POSITIONS}  SL: {SL_PCT}%  TP1: {TP1_PCT}%  TP2: {TP2_PCT}%")
    log(f"Halt: trading-agent.bat kill")

    while True:
        if kill_switch_active():
            log("Kill switch active. Halting.")
            break

        try:
            bal = get_futures_usdt()
            open_pos = get_open_positions()
            log(f"Futures USDT avail: ${bal:.4f}  open positions: {len(open_pos)}")

            for p in open_pos:
                qty = float(p["positionAmt"])
                pnl = float(p.get("unRealizedProfit", 0))
                ep = float(p.get("entryPrice", 0))
                mp = float(p.get("markPrice", 0))
                side = "LONG" if qty > 0 else "SHORT"
                log(f"  {p['symbol']} {side} entry=${ep:.6f} mark=${mp:.6f} unPnL=${pnl:+.4f}")

            position_locked = len(open_pos) >= MAX_OPEN_POSITIONS

            # If slot is OPEN, check cache for any deferred signal still valid
            if not position_locked and bal >= MARGIN_USD + 0.05:
                # Get current prices for all watchlist symbols
                try:
                    all_tickers = {t["symbol"]: float(t["lastPrice"])
                                    for t in spot_client().futures_ticker()
                                    if t["symbol"] in _watchlist}
                except Exception:
                    all_tickers = {}
                cached_signal = _find_executable_cached(all_tickers)
                if cached_signal:
                    sym, action, lev, price, strength, age, move = cached_signal
                    log(f"  ⚡ EXECUTING CACHED signal: {action} {sym} (strength {strength:.2f}, age {age:.1f}min, drift {move:+.2f}%)")
                    opened = open_position(sym, action, lev or DEFAULT_LEVERAGE, price)
                    if opened:
                        # Refresh state, skip scan this cycle
                        log(f"  ✓ Position opened from cache. Skipping fresh scan.")
                        position_locked = True
                        # Remove this from cache so doesn't re-trigger
                        if sym in _watchlist:
                            _watchlist[sym]["action"] = None

            if position_locked:
                log(f"  Max positions ({MAX_OPEN_POSITIONS}) reached — still scanning + analyzing for cache (no execution)")
            if bal < MARGIN_USD + 0.05 and not position_locked:
                log(f"  Insufficient futures balance to open new position.")
            else:
                # Prune stale watchlist
                pruned = _watchlist_prune()
                if pruned:
                    log(f"  Pruned {pruned} watchlist entries older than {WATCHLIST_TTL_MIN}min")

                # Scan universe (capped + stratified by volume bucket)
                all_movers = scan_futures_movers(MAX_UNIVERSE_SIZE)
                # Log bucket distribution
                _l = sum(1 for c in all_movers if c["vol_m"] >= 100)
                _m = sum(1 for c in all_movers if 20 <= c["vol_m"] < 100)
                _s = sum(1 for c in all_movers if c["vol_m"] < 20)
                log(f"  Universe: {len(all_movers)} candidates (large={_l}, mid={_m}, small={_s})")

                # For each, decide: NEW (not cached) / RECHECK (cached but needs re-analysis) / SKIP
                queue = []   # what to actually analyze
                skip = []    # cached + stable + recent
                for c in all_movers:
                    needs, reason = _watchlist_needs_recheck(c["symbol"], c["price"])
                    c["_recheck_reason"] = reason
                    if needs:
                        queue.append(c)
                    else:
                        skip.append(c)

                # No TOP_K cap — analyze entire queue
                analysis_queue = queue[:TOP_K]

                log(f"  -> {len(analysis_queue)} to analyze | {len(skip)} skip (cached <60min, no price move)")
                if skip:
                    log(f"  Skipping ({len(skip)}): " + ", ".join(c["symbol"] for c in skip[:15]) + ("..." if len(skip) > 15 else ""))
                log(f"  Analysis queue order:")
                for i, c in enumerate(analysis_queue[:20], 1):
                    log(f"    {i:2d}. [{c['_recheck_reason']:<14}] {c['symbol']:<14} ch24={c['ch24']:+.2f}% vol={c['vol_m']:.0f}M score={c['score']:.2f}")
                if len(analysis_queue) > 20:
                    log(f"    ... + {len(analysis_queue) - 20} more")

                candidates = analysis_queue

                executed = False
                # Parallel batches — analyze PARALLEL_ANALYSIS at a time
                pending = list(candidates)
                while pending and not executed:
                    batch = pending[:PARALLEL_ANALYSIS]
                    pending = pending[PARALLEL_ANALYSIS:]
                    with ThreadPoolExecutor(max_workers=PARALLEL_ANALYSIS) as ex:
                        futs = {ex.submit(run_pipeline, c["symbol"]): c for c in batch}
                        results = []
                        for fut in as_completed(futs):
                            c = futs[fut]
                            try:
                                r = fut.result(timeout=180)
                            except Exception as e:
                                log(f"    {c['symbol']:<14} pipeline error: {type(e).__name__}: {e}")
                                continue
                            if not r:
                                continue
                            # Fetch atr_1d_pct for Layer 3 (shares TV cache with Layer 1 inside decide_action)
                            atr_1d = None
                            try:
                                from tradingagents.crypto.tv_data import fetch_tv_multi_tf
                                _tv = fetch_tv_multi_tf(c["symbol"])
                                if _tv:
                                    _tf1d = _tv["timeframes"].get("1D", {})
                                    if _tf1d.get("available"):
                                        atr_1d = _tf1d.get("atr_pct")
                            except Exception:
                                pass
                            action, lev, audit_layers, tv_snap = decide_action(
                                r["debate"], r["risk"],
                                symbol=c["symbol"], ch24=c.get("ch24"), atr_1d_pct=atr_1d)
                            blocked = ""
                            if action is None:
                                # Find which layer blocked, for human-readable log line
                                for k in ("L0", "L2", "L1", "L3", "L5"):
                                    if k in audit_layers and not audit_layers[k][0]:
                                        blocked = f" [{k}:{audit_layers[k][1]}]"
                                        break
                            log(f"    {c['symbol']:<14} debate={r['debate'].consensus} ({r['debate'].consensus_strength:.2f})  risk={r['risk'].recommendation} ({r['risk'].risk_score:.1f})  -> {action or 'NO_TRADE'}{blocked}")
                            _audit_log_gate(c["symbol"], action or "NO_TRADE", audit_layers, action or "NO_TRADE")
                            # Remember analysis result + action + TV snapshot in watchlist (Layer 6)
                            _watchlist_remember(c["symbol"], r["snap"], r["debate"], r["risk"],
                                                 action=action, leverage=lev,
                                                 ch24=c.get("ch24"),
                                                 atr_1d_pct=atr_1d,
                                                 tv_snapshot=tv_snap)
                            if action and not executed:
                                results.append((c, action, lev, r))

                        # If any candidate in this batch got a signal, execute the strongest
                        # UNLESS position_locked (then just cache it for later)
                        if results and not executed and not position_locked:
                            # Prefer higher debate strength
                            results.sort(key=lambda x: x[3]["debate"].consensus_strength, reverse=True)
                            c, action, lev, _r = results[0]
                            log(f"  >> Decision: {action} {c['symbol']} @ {lev}x leverage")
                            opened = open_position(c["symbol"], action, lev, c["price"])
                            if opened:
                                executed = True
                        elif results and position_locked:
                            # Log the would-be candidates for visibility
                            results.sort(key=lambda x: x[3]["debate"].consensus_strength, reverse=True)
                            for c, action, lev, _r in results[:3]:
                                log(f"  📋 cached candidate (locked): {action} {c['symbol']} (strength {_r['debate'].consensus_strength:.2f})")

                if not executed and not position_locked:
                    log("  No EXECUTE signal across all candidates. Holding cash.")

        except Exception as e:
            log(f"Loop error: {type(e).__name__}: {e}")
            traceback.print_exc(file=sys.stdout)

        log(f"Sleeping {INTERVAL_MIN}min until next cycle...\n")
        for _ in range(INTERVAL_MIN * 60 // 10):
            if kill_switch_active():
                log("Kill switch detected during sleep. Halting.")
                return
            time.sleep(10)


if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        log("Stopped by user.")
