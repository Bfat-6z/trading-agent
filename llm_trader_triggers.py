"""R1 trigger engine — DARK MEASUREMENT ONLY (plans/redesign_tin_va_chart_v1.md §2a, §5-R1).

Evaluates, per cycle, which candidate-selection paths each coin hits:
    news        — a fresh news event names the coin (or a strong macro event -> majors)
    whale       — Telegram whale/liquidation pressure on the coin (data-trust contract is
                  `shadow_only`, so this path is measurement/context, never a sole gate)
    funding_extreme — |8h funding| at an extreme (crowded positioning to fade)
    flush_no_oi — capitulation-flush proxy (OI-declining leg DEFERRED until the deriv fetch
                  is re-enabled hang-proof; renamed from the spec's umbrella `funding_oi` so the
                  two hypotheses are measured separately — Opus review L1)
    chart_align — the owner's "3 khung cùng hướng": 15m trend + 1h trend + 4h trend agree
                  AND the EMA stack confirms the same direction

R1 does NOT gate anything: results are logged (trigger_log.jsonl) and tagged onto trades
(`trigger_paths`) so per-path expectancy accumulates from LIVE data before any behavior
change (R2 flips gating on). Pure + fail-soft: a malformed input must never raise into the
trading loop — every public function returns an empty/neutral result on bad input.

Thresholds are set ONCE here (env-overridable), logged with every cycle, and must be tuned
only on trigger_log data BEFORE R2 — never on the same closes that judge the paths (Šidák
lesson, bughunt 2026-07-08).
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import Any

# --- thresholds (provisional defaults; tune from trigger_log before R2, then freeze) ---
T_NEWS_SYM = float(os.environ.get("LLM_TRADER_TRIG_NEWS_SYM", "0.3"))     # per-event catalyst, symbol-matched
T_NEWS_MACRO = float(os.environ.get("LLM_TRADER_TRIG_NEWS_MACRO", "0.7"))  # macro event -> majors only
T_WHALE_SCORE = float(os.environ.get("LLM_TRADER_TRIG_WHALE", "0.5"))
T_FUND = float(os.environ.get("LLM_TRADER_TRIG_FUND", "0.0005"))          # |8h funding| >= 0.05%
T_FLUSH_RET5 = float(os.environ.get("LLM_TRADER_TRIG_FLUSH_RET5", "-3.0"))  # 5-bar return <= -3%
T_FLUSH_VOL = float(os.environ.get("LLM_TRADER_TRIG_FLUSH_VOL", "2.0"))     # volume surge >= 2x
NEWS_MAX_AGE_MIN = float(os.environ.get("LLM_TRADER_TRIG_NEWS_AGE", "90"))
MAJORS = ("BTCUSDT", "ETHUSDT")

_THRESHOLDS = {"news_sym": T_NEWS_SYM, "news_macro": T_NEWS_MACRO, "whale": T_WHALE_SCORE,
               "fund": T_FUND, "flush_ret5": T_FLUSH_RET5, "flush_vol": T_FLUSH_VOL}


def _num(x: Any) -> float | None:
    import math
    try:
        v = float(x)
        return v if math.isfinite(v) else None   # reject NaN AND +/-inf (Opus review L3)
    except (TypeError, ValueError):
        return None


def read_news(path: Path, now_ms: int, max_age_min: float = NEWS_MAX_AGE_MIN) -> dict[str, Any]:
    """Load news_latest.json (news_observer output). Returns a neutral dict when the file is
    missing/stale/corrupt — the trading loop must keep working with zero news, silently.
    Only events with clean sanitize_flags are kept (data_trust: tainted text never steers)."""
    empty = {"fresh": False, "catalyst": 0.0, "macro_risk": None, "events": []}
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        ts = datetime.datetime.fromisoformat(str(d.get("ts")).replace("Z", "+00:00"))
        if ts.tzinfo is None:   # Opus review M1: a tz-naive ts would be read in LOCAL tz (UTC+7 here),
            ts = ts.replace(tzinfo=datetime.timezone.utc)   # silently zeroing the news path forever
        age_min = (now_ms / 1000 - ts.timestamp()) / 60.0
        if age_min > max_age_min or age_min < -5:
            return empty
        events = []
        for e in (d.get("top_events") or []):
            if not isinstance(e, dict) or e.get("sanitize_flags"):
                continue
            events.append({"title": str(e.get("title") or "")[:160],
                           "symbols": [str(s).upper() for s in (e.get("symbols") or [])],
                           "catalyst": _num(e.get("catalyst")) or 0.0,
                           "freshness": _num(e.get("freshness")) or 0.0,
                           "reasons": list(e.get("reasons") or [])[:6]})
        return {"fresh": True, "catalyst": _num(d.get("catalyst_score")) or 0.0,
                "macro_risk": _num(d.get("macro_risk_score")), "events": events}
    except Exception:
        return empty


def _base(sym: str) -> str:
    """BTCUSDT -> BTC (1000BONKUSDT -> 1000BONK; matching is exact-on-base, best-effort)."""
    s = str(sym).upper()
    return s[:-4] if s.endswith("USDT") else s


def evaluate(ctx_rows: list[dict[str, Any]], news: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{symbol: {"paths": [...], "vals": {...}}} for coins hitting >=1 path. Never raises."""
    out: dict[str, dict[str, Any]] = {}
    news = news if isinstance(news, dict) else {}
    events = news.get("events") or []
    for c in ctx_rows if isinstance(ctx_rows, list) else []:
        try:
            sym = str(c.get("symbol") or "")
            if not sym:
                continue
            paths: list[str] = []
            vals: dict[str, Any] = {}

            # -- news: a fresh clean event names this coin, or a strong macro event -> majors
            if news.get("fresh"):
                base = _base(sym)
                hit = [e for e in events if base in e.get("symbols", []) and e.get("catalyst", 0) >= T_NEWS_SYM]
                if hit:
                    paths.append("news")
                    vals["news"] = {"n": len(hit), "catalyst": max(e["catalyst"] for e in hit),
                                    "title": hit[0]["title"][:80]}
                elif sym in MAJORS:
                    macro = [e for e in events if not e.get("symbols") and e.get("catalyst", 0) >= T_NEWS_MACRO]
                    if macro:
                        paths.append("news")
                        vals["news"] = {"n": len(macro), "catalyst": max(e["catalyst"] for e in macro),
                                        "title": macro[0]["title"][:80], "macro": True}

            # -- whale: pressure from whale_flow_observer (already per-symbol in ctx)
            w = c.get("whale") or {}
            wscore = _num(w.get("score")) or 0.0
            wside = str(w.get("side") or "")
            if wside in ("LONG", "SHORT") and wscore >= T_WHALE_SCORE:
                paths.append("whale")
                vals["whale"] = {"side": wside, "score": wscore}

            # -- funding/flush: TWO separately-measured sub-paths (Opus review L1 — they are two
            # different hypotheses: fading crowded funding != buying a capitulation flush; pooling
            # them under one expectancy bucket would muddy the R2 verdict). OI leg still deferred.
            fr = _num(c.get("funding_rate")) or 0.0
            ret5 = _num(c.get("ret5_pct"))
            volr = _num(c.get("vol_ratio")) or 0.0
            if abs(fr) >= T_FUND:
                paths.append("funding_extreme")
                vals["funding_extreme"] = {"rate": fr}
            if ret5 is not None and ret5 <= T_FLUSH_RET5 and volr >= T_FLUSH_VOL:
                paths.append("flush_no_oi")
                vals["flush_no_oi"] = {"ret5_pct": ret5, "vol_ratio": volr}

            # -- chart_align: 15m + 1h + 4h all agree AND the EMA stack confirms
            t15 = str(c.get("trend") or "")            # up/down (EMA fast vs slow, 15m)
            t1h = str(c.get("htf_1h_trend") or "")
            t4h = str(c.get("htf_4h_trend") or "")
            stack = str(c.get("ema_stack") or "")
            if t15 == t1h == t4h == "up" and stack == "bull_stack":
                paths.append("chart_align")
                vals["chart_align"] = {"dir": "up"}
            elif t15 == t1h == t4h == "down" and stack == "bear_stack":
                paths.append("chart_align")
                vals["chart_align"] = {"dir": "down"}

            if paths:
                out[sym] = {"paths": paths, "vals": vals}
        except Exception:
            continue   # one bad row must never kill the cycle's trigger evaluation
    return out


def log_cycle(path: Path, now_ms: int, trig_map: dict[str, dict[str, Any]], n_ctx: int) -> None:
    """One JSONL line per cycle: what fired and with which values (the R2 tuning dataset).
    Best-effort — logging failure must never affect trading. Rotates at ~15MB (Opus review M2):
    current -> .1 (previous .1 overwritten) so the log never grows unbounded."""
    try:
        p = Path(path)
        try:
            if p.exists() and p.stat().st_size > 15_000_000:
                p.replace(p.with_suffix(p.suffix + ".1"))
        except Exception:
            pass
        rec = {"ts_ms": now_ms, "n_ctx": n_ctx, "n_hit": len(trig_map),
               "thresholds": _THRESHOLDS, "hits": trig_map}
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=True) + "\n")
    except Exception:
        pass
