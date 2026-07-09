"""R1 trigger engine — DARK MEASUREMENT ONLY (plans/redesign_tin_va_chart_v1.md §2a, §5-R1).

Evaluates, per cycle, which candidate-selection paths each coin hits:
    news        — a fresh news event names the coin (or a strong macro event -> majors)
    whale       — Telegram whale/liquidation pressure on the coin (data-trust contract is
                  `shadow_only`, so this path is measurement/context, never a sole gate)
    funding_extreme — |8h funding| at an extreme (crowded positioning to fade)
    flush_oi_dn — capitulation-flush WITH open interest declining (the ONE +EV lab setup,
                  clf_oi_dn lineage). OI probed hang-proof (orderflow_data._bounded_get daemon
                  thread) ONLY on flush hits, bounded OI_PROBE_MAX/cycle.
    flush_no_oi — flush without OI confirmation (OI rising/unavailable). Split kept separate
                  from flush_oi_dn AND funding_extreme (Opus review L1: one hypothesis per bucket)
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
T_OI_DECL = float(os.environ.get("LLM_TRADER_TRIG_OI_DECL", "-1.0"))        # OI slope <= -1% over ~4-8h
OI_PROBE_MAX = int(os.environ.get("LLM_TRADER_TRIG_OI_MAX", "3"))           # OI lookups per cycle (bound)
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


def probe_oi_slope(sym: str, now_ms: int) -> float | None:
    """% change of open interest over the last ~8h (1h points). Negative = OI declining
    (longs being flushed OUT, not added — the +EV clf_oi_dn precondition). Hang-proof by
    construction: orderflow_data._bounded_get runs each HTTP call in a daemon thread with a
    hard deadline, so this can never freeze the cycle. Fail-soft None on any problem."""
    try:
        import orderflow_data as of   # lazy: keep this module import-light for tests
        series = of.fetch_deriv_series(sym, "1h", start_ms=now_ms - 8 * 3_600_000, end_ms=now_ms)
        ois = [v["oi"] for _, v in sorted(series.items()) if isinstance(v, dict) and _num(v.get("oi"))]
        if len(ois) < 3 or ois[0] <= 0:
            return None
        return round((ois[-1] - ois[0]) / ois[0] * 100, 2)
    except Exception:
        return None


def evaluate(ctx_rows: list[dict[str, Any]], news: dict[str, Any],
             oi_probe: Any = None) -> dict[str, dict[str, Any]]:
    """{symbol: {"paths": [...], "vals": {...}}} for coins hitting >=1 path. Never raises.
    `oi_probe`: optional callable(sym) -> oi_slope_pct|None, consulted ONLY on flush hits
    (bounded OI_PROBE_MAX/cycle) to split flush_oi_dn (the +EV lab setup) from flush_no_oi."""
    out: dict[str, dict[str, Any]] = {}
    news = news if isinstance(news, dict) else {}
    events = news.get("events") or []
    oi_probed = 0
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
                vals["whale"] = {"side": wside, "score": wscore,
                                 "events": _num(w.get("events"))}   # score is ~binary; event count is
                                                                    # the real tune discriminator

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
                # flush hit -> probe OI (bounded) to split the +EV setup (capitulation WITH
                # open interest declining = longs flushed out) from the plain flush proxy.
                slope = None
                if callable(oi_probe) and oi_probed < OI_PROBE_MAX:
                    oi_probed += 1
                    try:
                        slope = _num(oi_probe(sym))
                    except Exception:
                        slope = None
                if slope is not None and slope <= T_OI_DECL:
                    paths.append("flush_oi_dn")
                    vals["flush_oi_dn"] = {"ret5_pct": ret5, "vol_ratio": volr, "oi_slope_pct": slope}
                else:
                    paths.append("flush_no_oi")
                    vals["flush_no_oi"] = {"ret5_pct": ret5, "vol_ratio": volr, "oi_slope_pct": slope}

            # -- chart_align: 15m + 1h + 4h all agree AND the EMA stack confirms
            t15 = str(c.get("trend") or "")            # up/down (EMA fast vs slow, 15m)
            t1h = str(c.get("htf_1h_trend") or "")
            t4h = str(c.get("htf_4h_trend") or "")
            stack = str(c.get("ema_stack") or "")
            _dir = ("up" if (t15 == t1h == t4h == "up" and stack == "bull_stack") else
                    "down" if (t15 == t1h == t4h == "down" and stack == "bear_stack") else None)
            if _dir:
                paths.append("chart_align")
                # log the candidate DISCRIMINATORS (adx/efficiency/overextension) alongside the hit —
                # the R2 tune must tighten this path (it admits ~half the universe) and can only do so
                # with evidence if the dark window RECORDED what a stricter gate would have keyed on.
                vals["chart_align"] = {"dir": _dir,
                                       "adx": _num(c.get("adx")),
                                       "eff": _num(c.get("efficiency")),
                                       "px_e20": _num(c.get("px_vs_ema20_pct")),
                                       "ret20": _num(c.get("ret20_pct"))}

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
