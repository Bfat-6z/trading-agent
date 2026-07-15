"""TidalFi data adapter — 42-market OHLCV (incl. 22 TradFi-only perps) for the mission.

STANDALONE, NOT WIRED. Paper-only repo, live LOCKED. This module lets the
paper-trading mission later analyze the TidalFi-only TradFi perps
(NVDA/TSLA/META/AAPL/XAU/XAG/SPCX/OPENAI...) by emitting bars in the EXACT
shape the mission already consumes, so a ~5-line branch in build_context can
swap sources without touching any downstream code.

API (verified live 2026-07-15): base https://td.tidalfi.ai — public, no auth,
but a browser-ish User-Agent header is required.
  GET /api/market-data/symbols?status=TRADING
      -> {code,message,data:{symbols:[{symbol,baseAsset,pricePrecision,tickSize,
          stepSize,minNotional,contractType,underlyingType,underlyingSubTypes,...}]}}
      42 rows. underlyingType: COIN (20, all Binance-overlap), EQUITY (19),
      COMMODITY (2: XAU/XAG), PREMARKET (1: OPENAI). 19+2+1 = the 22 TidalFi-only.
  GET /api/tradingview/history?symbol=NVDAUSDT&resolution=15&from=UNIX_S&to=UNIX_S
      -> TradingView UDF {s:"ok",t:[...],o:[...],h:[...],l:[...],c:[...],v:[...]}
      t[] = bar OPEN time in unix SECONDS (t % tf == 0, verified). v[] = BASE
      quantity (verified by magnitude on BTCUSDT). The response INCLUDES the
      still-forming bar and may return MORE history than [from,to] asked for.
  GET /api/trading/orderbook?symbol=X&limit=5
      -> {data:{bids:[["px","qty"],...],asks:[...]}} (strings).

============================================================================
DISCOVERED BAR CONTRACT (what the mission consumes — do not drift from this)
============================================================================
Source of truth read on 2026-07-15:
  orderflow_data.py:96-160  fetch_klines_with_flow (producer)
  llm_trader.py:355-416     build_context           (consumer)
  backtest_chart_signal.py:70-94  _bars_to_df/compute_indicators (consumer)
  orderflow_data.py:163-186 compute_cvd_columns     (consumer)
  orderflow_data.py:215-244 enrich_indicator_df     (STRICT ts_ms join, fail-closed)

Each bar is a dict:
  open_time       str   ISO-8601 UTC, timespec="milliseconds" (e.g. "2026-07-15T04:00:00+00:00
                        style with .000 ms) — parsed by _bars_to_df.
  close_time      str   same format; pd.to_datetime(close_time) -> ts_ms MUST equal the
                        int ts_ms below EXACTLY or enrich_indicator_df raises (fail-closed).
  ts_ms           int   bar CLOSE time in ms, Binance convention: open_ms + tf_ms - 1
                        (i.e. ...999). VERIFIED: the mission uses close-time ts_ms —
                        fetch_klines_with_flow sets ts_ms = kline close_time and
                        build_context's closed-bar filter (llm_trader.py:371-372)
                        does `ts_ms + bar_ms <= now_ms` on top of it.
  open/high/low/close  float
  volume          float base quantity
  quote_volume    float quote (USDT) turnover — TidalFi UDF has no quote volume, we
                        approximate volume*close; feeds the _quote_vol_24h liquidity
                        tier (llm_trader.py:407), approximation errs small => tier
                        errs pessimistic => safe.
  taker_buy_base  float REQUIRED by compute_cvd_columns. TidalFi has NO taker split,
  taker_buy_quote float so we emit the NEUTRAL value volume/2 (resp. quote/2):
                        cvd_delta = 2*tbb - vol = 0, buy_frac = 0.5, cvd_norm = 0.0.
                        CVD is honestly ABSENT (zero), never fabricated.
  is_final        bool  always True — only closed bars are returned.
Ordering: chronological ascending, deduped, CLOSED bars only ([start,end) — the
forming bar, i.e. last t with t_ms + tf_ms > now_ms, is dropped).

============================================================================
INTEGRATION PLAN (for the later wiring change — NOT done here)
============================================================================
1. llm_trader.py:33 area — `import tidalfi_data as td` next to `import orderflow_data as of`.
2. llm_trader.py:367 in build_context(), branch per symbol (~5 lines):
       if sym in td.tidalfi_only_symbols():
           fb = td.fetch_klines(sym, TF, limit=300, end_ms=now_ms)
           sm = td.session_meta(sym, fb, TF, now_ms=now_ms)
           if not sm or not sm["fresh"] or sm["longest_gap_bars"] > 2: continue
           fund = []                                   # (3) below
       else:
           fb = of.fetch_klines_with_flow(...)         # unchanged
           fund = of.fetch_funding_series(...)         # unchanged (llm_trader.py:375)
   The existing closed-bar filter at llm_trader.py:371-372 stays and works
   unchanged on our bars (same ts_ms convention).
3. Funding: TidalFi has no funding-history endpoint here — pass fund=[] for
   TidalFi symbols; join_funding_point_in_time (orderflow_data.py:249-251)
   already emits funding_rate=0.0 for empty funding. Do NOT call
   of.fetch_funding_series for a TidalFi symbol (Binance errors on NVDAUSDT and
   build_context's except would silently skip the symbol).
4. Universe: universe_filter.py:28 NON_CRYPTO deliberately excludes these bases
   from the BINANCE universe — inject TidalFi symbols AFTER that filter (as an
   extra symbol list), never through it.
5. Session honesty: TidalFi TradFi perps print synthetic 24/7 contiguous bars
   (verified: NVDA weekend bars exist with volume) — so gap_count is usually 0
   and the real staleness signal is flat/zero-volume bars (OPENAIUSDT shows
   them). Callers should also check session_meta()["flat_bar_frac"].
"""
from __future__ import annotations

import datetime
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import requests

_BASE = "https://td.tidalfi.ai"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) tidalfi_data/1.0"}

ROOT = Path(__file__).resolve().parent
_STATE = ROOT / "state"
_META_CACHE_PATH = _STATE / "tidalfi_meta_cache.json"
_LOG_PATH = _STATE / "tidalfi_data.log"
_META_TTL_MS = 6 * 3600 * 1000

# tf -> ms, identical values to orderflow_data._TF_MS (kept local so this module
# stays import-independent; the tests assert parity).
_TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000,
          "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000}
# tf -> TradingView UDF resolution string.
_TF_RES = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "4h": "240",
           "1d": "1D", "1w": "1W"}

_KIND_MAP = {"COIN": "crypto", "COMMODITY": "commodity", "EQUITY": "equity",
             "PREMARKET": "equity"}   # pre-IPO (OPENAI) = equity bucket: gets session scrutiny

# The 20 COIN bases live on TidalFi 2026-07-15 — ALL are Binance USDT perps.
# Fallback overlap set when Binance exchangeInfo is unreachable.
_BINANCE_OVERLAP_FALLBACK = frozenset({
    "1000PEPE", "1000SHIB", "ADA", "ASTER", "AVAX", "BNB", "BTC", "DOGE", "DOT",
    "ETH", "HYPE", "LINK", "LTC", "NEAR", "SOL", "TRX", "UNI", "XLM", "XRP", "ZEC",
})


# ---------------------------------------------------------------------------
# infrastructure: logging + hang-proof HTTP
# ---------------------------------------------------------------------------
def _log(event: str, **kw: Any) -> None:
    """One jsonl line per failure/notable event. Never raises."""
    try:
        _STATE.mkdir(parents=True, exist_ok=True)
        row = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
               "event": event, **kw}
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _bounded_get_json(path: str, params: dict, hard_deadline: float = 8.0) -> Any:
    """GET that CANNOT hang the caller. Same pattern as orderflow_data._bounded_get
    (ck:debug 2026-07-08: on Windows the TLS handshake can block PAST the requests
    timeout) — run in a daemon thread, abandon it after hard_deadline. Returns the
    parsed JSON or None (fail-open)."""
    box: list = [None]

    def _run():
        try:
            r = requests.get(_BASE + path, params=params, headers=_HEADERS, timeout=(3, 6))
            box[0] = r.json()
        except Exception:
            box[0] = None

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(hard_deadline)
    return box[0]


def _iso_ms(ms: int) -> str:
    """MILLISECOND-precision ISO string — byte-identical to orderflow_data._iso_ms.
    compute_indicators re-derives ts_ms by parsing this string; any precision drift
    breaks the fail-closed enrich join (orderflow_data.py:215-244)."""
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# symbols metadata (6h cache: in-memory + state/tidalfi_meta_cache.json)
# ---------------------------------------------------------------------------
_mem_meta: dict[str, Any] = {"fetched_ms": 0, "symbols": None}


def fetch_symbols(force: bool = False) -> list[dict[str, Any]]:
    """TRADING symbol metadata rows, 6h-cached. [] on total failure (fail-open)."""
    now_ms = int(time.time() * 1000)
    if not force and _mem_meta["symbols"] and now_ms - _mem_meta["fetched_ms"] < _META_TTL_MS:
        return list(_mem_meta["symbols"])
    disk = None
    try:
        disk = json.loads(_META_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        disk = None
    if (not force and isinstance(disk, dict) and disk.get("symbols")
            and now_ms - int(disk.get("fetched_ms", 0)) < _META_TTL_MS):
        _mem_meta.update(fetched_ms=int(disk["fetched_ms"]), symbols=disk["symbols"])
        return list(disk["symbols"])
    j = _bounded_get_json("/api/market-data/symbols", {"status": "TRADING"})
    try:
        rows = j["data"]["symbols"]
        rows = [r for r in rows if r.get("status") == "TRADING" and r.get("symbol")]
        if not rows:
            raise ValueError("empty symbols list")
    except Exception as e:
        _log("symbols_fetch_fail", error=repr(e), got=type(j).__name__)
        # fail-open to STALE disk cache if present (better stale meta than none)
        if isinstance(disk, dict) and disk.get("symbols"):
            return list(disk["symbols"])
        return []
    _mem_meta.update(fetched_ms=now_ms, symbols=rows)
    try:
        _STATE.mkdir(parents=True, exist_ok=True)
        tmp = _META_CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"fetched_ms": now_ms, "symbols": rows}), encoding="utf-8")
        os.replace(tmp, _META_CACHE_PATH)
    except Exception as e:
        _log("meta_cache_write_fail", error=repr(e))
    return list(rows)


def _kind_for(symbol: str) -> str:
    """'crypto' | 'equity' | 'commodity'. Unknown/meta-unavailable -> 'equity'
    (conservative: the equity bucket is the one callers session-scrutinize)."""
    symbol = symbol.upper()
    for r in fetch_symbols():
        if r.get("symbol") == symbol:
            return _KIND_MAP.get(r.get("underlyingType"), "equity")
    return "equity"


# ---------------------------------------------------------------------------
# klines (no cache — fresh each call)
# ---------------------------------------------------------------------------
def _bars_from_udf(udf: Any, tf_ms: int, cutoff_ms: int) -> list[dict[str, Any]]:
    """PURE: TradingView UDF payload -> mission-contract bar dicts.
    - closed bars only: keep t where t_ms + tf_ms <= cutoff_ms (drops the forming
      bar; identical predicate to fetch_klines_with_flow's close_time < end_ms,
      since close_ms = t_ms + tf_ms - 1)
    - chronological, deduped by open time (last write wins)
    - never fabricates bars: gaps in t[] stay gaps."""
    try:
        if not isinstance(udf, dict) or udf.get("s") != "ok":
            return []
        t, o, h, l, c, v = (udf.get(k) for k in ("t", "o", "h", "l", "c", "v"))
        n = min(len(t), len(o), len(h), len(l), len(c), len(v))
        if n == 0:
            return []
    except Exception:
        return []
    seen: dict[int, dict[str, Any]] = {}
    for i in range(n):
        try:
            open_ms = int(t[i]) * 1000
            if open_ms + tf_ms > cutoff_ms:      # forming (or future) bar — drop
                continue
            close_ms = open_ms + tf_ms - 1       # Binance ...999 convention
            vol = float(v[i])
            close = float(c[i])
            quote = vol * close                  # approximation (UDF has no quote vol)
            seen[open_ms] = {
                "open_time": _iso_ms(open_ms), "close_time": _iso_ms(close_ms),
                "ts_ms": close_ms,
                "open": float(o[i]), "high": float(h[i]), "low": float(l[i]), "close": close,
                "volume": vol, "quote_volume": quote,
                # NEUTRAL taker split (no CVD data on TidalFi): cvd_delta == 0 downstream.
                "taker_buy_base": vol / 2.0, "taker_buy_quote": quote / 2.0,
                "is_final": True,
            }
        except Exception:
            continue
    return [seen[k] for k in sorted(seen)]


def fetch_klines(symbol: str, tf: str = "15m", limit: int = 300,
                 end_ms: int | None = None) -> list[dict[str, Any]]:
    """Closed-bar OHLCV in the mission's exact bar-dict shape (see module docstring).
    tf in {1m,5m,15m,1h,4h,1d,1w}. end_ms = optional cutoff ('now' for backfills).
    [] on any failure (fail-open, one log line)."""
    tf_ms, res = _TF_MS.get(tf), _TF_RES.get(tf)
    if not tf_ms or not res:
        _log("klines_bad_tf", symbol=symbol, tf=tf)
        return []
    cutoff_ms = int(end_ms) if end_ms else int(time.time() * 1000)
    frm = (cutoff_ms - (limit + 3) * tf_ms) // 1000    # +3 pad: forming bar + edge slop
    j = _bounded_get_json("/api/tradingview/history",
                          {"symbol": symbol.upper(), "resolution": res,
                           "from": frm, "to": cutoff_ms // 1000})
    if j is None:
        _log("klines_http_fail", symbol=symbol, tf=tf)
        return []
    bars = _bars_from_udf(j, tf_ms, cutoff_ms)
    if not bars:
        _log("klines_empty", symbol=symbol, tf=tf, status=(j.get("s") if isinstance(j, dict) else None))
        return []
    return bars[-limit:]


# ---------------------------------------------------------------------------
# session honesty
# ---------------------------------------------------------------------------
def session_meta(symbol: str, bars: list[dict[str, Any]], tf: str = "15m",
                 now_ms: int | None = None) -> dict[str, Any] | None:
    """Honesty check before analyzing a series — stock perps burned this repo before.
    Returns {kind, gap_count, longest_gap_bars, fresh, flat_bar_frac} or None on
    bad input (fail-open). Callers should SKIP a symbol this cycle when
    fresh is False or longest_gap_bars > 2 or flat_bar_frac is high (dead session).
    NOTE (verified live): TidalFi TradFi perps print contiguous synthetic bars 24/7,
    so gaps are rare — deadness shows up as flat (high==low) / zero-volume bars;
    that's what flat_bar_frac (last 96 bars) measures. Bars are NEVER fabricated
    here; we only measure what the venue returned."""
    tf_ms = _TF_MS.get(tf)
    if not tf_ms or not isinstance(bars, list):
        _log("session_meta_bad_input", symbol=symbol, tf=tf, n=len(bars) if isinstance(bars, list) else None)
        return None
    now_ms = int(now_ms) if now_ms else int(time.time() * 1000)
    gap_count, longest = 0, 0
    for a, b in zip(bars, bars[1:]):
        try:
            missing = round((int(b["ts_ms"]) - int(a["ts_ms"])) / tf_ms) - 1
        except Exception:
            continue
        if missing >= 1:
            gap_count += 1
            longest = max(longest, missing)
    fresh = bool(bars) and (now_ms - int(bars[-1]["ts_ms"])) <= 2 * tf_ms
    tail = bars[-96:]
    flat = sum(1 for b in tail
               if float(b.get("high", 0)) == float(b.get("low", 0)) or float(b.get("volume", 0)) <= 0)
    return {"kind": _kind_for(symbol), "gap_count": gap_count,
            "longest_gap_bars": longest, "fresh": fresh,
            "flat_bar_frac": round(flat / len(tail), 3) if tail else 1.0}


# ---------------------------------------------------------------------------
# TidalFi-only universe
# ---------------------------------------------------------------------------
_bnc_cache: dict[str, Any] = {"done": False, "bases": None}


def _binance_perp_bases() -> set[str] | None:
    """Base assets of Binance USDT perpetuals, fetched once per process via
    python-binance (public endpoint, no keys). None on failure -> caller uses
    the hardcoded overlap fallback."""
    if _bnc_cache["done"]:
        return _bnc_cache["bases"]
    _bnc_cache["done"] = True
    try:
        from binance.client import Client
        try:
            client = Client(requests_params={"timeout": 6}, ping=False)
        except TypeError:                     # older python-binance: no ping kwarg
            client = Client(requests_params={"timeout": 6})
        info = client.futures_exchange_info()
        bases = {s["baseAsset"] for s in info.get("symbols", [])
                 if s.get("quoteAsset") == "USDT" and s.get("contractType") == "PERPETUAL"
                 and s.get("status") == "TRADING"}
        _bnc_cache["bases"] = bases or None
    except Exception as e:
        _log("binance_bases_fail", error=repr(e))
        _bnc_cache["bases"] = None
    return _bnc_cache["bases"]


def tidalfi_only_symbols() -> set[str]:
    """TRADING TidalFi symbols NOT tradeable as a Binance USDT perp — i.e. the
    markets only this adapter can supply (equities + commodities + pre-IPO, plus
    any future COIN listing Binance lacks). Homonym guard: a non-COIN base that
    collides with a Binance crypto ticker (e.g. Visa 'V' vs a token named V) is
    still TidalFi-only — the Binance subtraction applies to COIN kinds only.
    Empty set on failure (fail-open)."""
    rows = fetch_symbols()
    if not rows:
        return set()
    bnc = _binance_perp_bases() or _BINANCE_OVERLAP_FALLBACK
    out: set[str] = set()
    for r in rows:
        kind = _KIND_MAP.get(r.get("underlyingType"), "equity")
        if kind == "crypto" and (r.get("baseAsset") or "") in bnc:
            continue
        out.add(r["symbol"])
    return out


# ---------------------------------------------------------------------------
# orderbook (spread/liquidity spot-check for later wiring)
# ---------------------------------------------------------------------------
def fetch_orderbook(symbol: str, limit: int = 5) -> dict[str, list[list[float]]] | None:
    """{'bids': [[px, qty], ...], 'asks': [...]} best-first, floats.
    None on any failure (fail-open, one log line)."""
    j = _bounded_get_json("/api/trading/orderbook", {"symbol": symbol.upper(), "limit": limit})
    try:
        d = j["data"]
        return {"bids": [[float(p), float(q)] for p, q in d["bids"]],
                "asks": [[float(p), float(q)] for p, q in d["asks"]]}
    except Exception as e:
        _log("orderbook_fail", symbol=symbol, error=repr(e))
        return None
