"""SHADOW trigger evaluator — measures the RAW directional edge of each R1/R2 trigger path,
fast and WITHOUT risking the mission account (owner-approved option C, 2026-07-11).

Why: the live mission is ultra-selective (208 stage-2 rejects : 1 confirm) so per-path
verdicts would take weeks. This reads trigger_log.jsonl and, for EVERY matured trigger fire
(deduped to one sample per coin+path per 2h episode), simulates a paper entry in the trigger's
implied direction and a FIXED mechanical exit, then records the outcome tagged by path. Hundreds
of independent samples/day -> a real "does this path have edge" answer.

Design contract:
- READ-ONLY w.r.t. the mission: writes ONLY state/llm_trader/shadow_triggers.jsonl (+ heartbeat).
  Never touches positions/closed/account/pending.
- NO LOOKAHEAD: entry = close of the bar at the trigger's ts; exit simulated on LATER bars only.
- Fixed exit (fair cross-path comparison): SL 1.5xATR, TP 2.5xATR (R:R 1.67), timeout 24 bars.
  Pessimistic same-bar resolution (SL before TP), like resolve(). Costs (fee+slip by tier) charged.
- Metric = R-multiples (sizing-independent): actual_R, mfe_R, mae_R, win.
- Idempotent/resumable: dedup key sym|path|bucket(2h); already-recorded keys are skipped.
- Fail-soft: one bad trigger/kline fetch skips that sample, never crashes the run.
"""
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import orderflow_data as of
import paper_cost_model as pcm

ROOT = Path(__file__).resolve().parent
LT_DIR = ROOT / "state" / "llm_trader"
TRIGGER_LOG = LT_DIR / "trigger_log.jsonl"
SHADOW = LT_DIR / "shadow_triggers.jsonl"
HEARTBEAT = LT_DIR / "shadow_trigger_eval_heartbeat.json"

TF = "15m"
TF_MS = of._TF_MS[TF]
EPISODE_MS = 2 * 3600 * 1000        # one sample per coin+path per 2h window (independence)
MATURITY_MS = 7 * 3600 * 1000       # a trigger is evaluable once its 24-bar horizon has fully closed
SL_ATR = 1.5
TP_ATR = 2.5
MAX_HOLD = 24
ATR_LOOKBACK = 14
ATR_FLOOR = 0.003          # skip atr_pct < 0.3%: pegged/dead assets (gold, stables) where a
                           # 1.5xATR stop is dwarfed by fees -> nonsense R; not a real 15m setup
# Tokenized stocks / commodities / leveraged ETFs on Binance futures — a 15m crypto scalper has no
# business here (gaps, RTH-only moves, pegged). Same list the A+ scanner excludes, extended.
NON_CRYPTO = {
    "XAU", "XAG", "PAXG", "GLD", "SLV", "USO", "TLT", "NVDA", "TSLA", "AAPL", "MSFT", "GOOGL",
    "GOOG", "META", "AMZN", "NFLX", "INTC", "AMD", "INTU", "CRM", "ORCL", "DIS", "JPM", "BAC",
    "V", "MA", "KO", "PEP", "WMT", "MCD", "HD", "NKE", "BA", "GE", "F", "GM", "SOXL", "SOXX",
    "QQQ", "SPY", "IWM", "INX", "TQQQ", "SQQQ", "UVXY", "SNDK", "MSTR", "COIN", "HOOD",
    "RIOT", "MARA", "SQ", "SKHYNIX", "SAMSUNG", "MRVL", "EWY", "SPCX",
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "EUR",
}


def _base(sym: str) -> str:
    s = str(sym).upper()
    return s[:-4] if s.endswith("USDT") else s


def _num(x: Any) -> float | None:
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _direction(path: str, vals: dict[str, Any]) -> str | None:
    """Implied trade side for a trigger path. None = ambiguous -> skip."""
    v = vals.get(path) or {}
    if path == "chart_align":
        return "LONG" if v.get("dir") == "up" else "SHORT" if v.get("dir") == "down" else None
    if path in ("flush_no_oi", "flush_oi_dn"):
        return "LONG"                                   # capitulation-bounce hypothesis
    if path == "funding_extreme":
        r = _num(v.get("rate"))
        return None if r is None or r == 0 else ("SHORT" if r > 0 else "LONG")  # fade crowded funding
    if path == "whale":
        s = v.get("side")
        return s if s in ("LONG", "SHORT") else None
    if path == "news":
        return None                                     # news has no inherent direction here
    return None


def _load_done_keys() -> set[str]:
    keys: set[str] = set()
    if SHADOW.exists():
        for ln in SHADOW.read_text(encoding="utf-8").splitlines():
            try:
                keys.add(json.loads(ln)["key"])
            except Exception:
                continue
    return keys


NEGCACHE = LT_DIR / "shadow_triggers_negcache.json"


def _load_neg_keys() -> set[str]:
    try:
        return set(json.loads(NEGCACHE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_neg_keys(keys: set[str]) -> None:
    try:
        NEGCACHE.write_text(json.dumps(sorted(keys)[-5000:]), encoding="utf-8")   # bounded
    except Exception:
        pass


def _collect_episodes(now_ms: int, done: set[str]) -> list[dict[str, Any]]:
    """One earliest-fire sample per (sym, path, 2h-bucket), matured and not yet done."""
    if not TRIGGER_LOG.exists():
        return []
    best: dict[str, dict[str, Any]] = {}
    for ln in TRIGGER_LOG.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        ts = int(rec.get("ts_ms") or 0)
        if ts <= 0 or now_ms - ts < MATURITY_MS:
            continue
        for sym, hit in (rec.get("hits") or {}).items():
            if _base(sym) in NON_CRYPTO:                 # skip tokenized stocks/commodities/stables
                continue
            vals = hit.get("vals") or {}
            for path in (hit.get("paths") or []):
                side = _direction(path, vals)
                if not side:
                    continue
                key = f"{sym}|{path}|{ts // EPISODE_MS}"
                if key in done:
                    continue
                cur = best.get(key)
                if cur is None or ts < cur["ts"]:
                    best[key] = {"key": key, "sym": sym, "path": path, "side": side, "ts": ts}
    return sorted(best.values(), key=lambda e: e["ts"])


def _atr(bars: list[dict[str, Any]], upto: int) -> float | None:
    """Wilder-ish mean true range over the ATR_LOOKBACK bars BEFORE index `upto`."""
    if upto < ATR_LOOKBACK + 1:
        return None
    trs = []
    for i in range(upto - ATR_LOOKBACK, upto):
        try:
            h, l, pc = float(bars[i]["high"]), float(bars[i]["low"]), float(bars[i - 1]["close"])
        except Exception:
            return None
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else None


def _simulate(bars: list[dict[str, Any]], entry_idx: int, side: str) -> dict[str, Any] | None:
    """Enter at close[entry_idx]; SL/TP off ATR; pessimistic same-bar (SL before TP); timeout.
    Returns R-multiple metrics net of tier costs, or None if not simulable."""
    atr = _atr(bars, entry_idx)
    if atr is None or atr <= 0:
        return None
    try:
        entry = float(bars[entry_idx]["close"])
    except Exception:
        return None
    if entry <= 0 or atr / entry < ATR_FLOOR:            # dead/pegged asset -> not a real setup
        return None
    risk = SL_ATR * atr
    reward = TP_ATR * atr
    if side == "LONG":
        sl, tp = entry - risk, entry + reward
    else:
        sl, tp = entry + risk, entry - reward
    quote_vol = sum(float(b.get("quote_volume", 0.0) or 0.0) for b in bars[max(0, entry_idx - 96):entry_idx])
    tier = pcm.liquidity_tier(quote_vol)
    exit_px = reason = None
    mfe = mae = 0.0
    held = 0
    for j in range(entry_idx + 1, min(entry_idx + 1 + MAX_HOLD, len(bars))):
        try:
            hi, lo, cl = float(bars[j]["high"]), float(bars[j]["low"]), float(bars[j]["close"])
        except Exception:
            continue
        held += 1
        if side == "LONG":
            mfe = max(mfe, hi - entry); mae = max(mae, entry - lo)
            if lo <= sl: exit_px, reason = sl, "sl"
            elif hi >= tp: exit_px, reason = tp, "tp"
        else:
            mfe = max(mfe, entry - lo); mae = max(mae, hi - entry)
            if hi >= sl: exit_px, reason = sl, "sl"
            elif lo <= tp: exit_px, reason = tp, "tp"
        if exit_px is not None:
            break
    if exit_px is None:                                  # timeout
        try:
            exit_px = float(bars[min(entry_idx + MAX_HOLD, len(bars) - 1)]["close"])
        except Exception:
            return None
        reason = "timeout"
    # costs: taker in + (stop-slip if sl else taker) out, as price fraction
    slip_out = float(pcm.fill_bps(tier, is_stop=(reason == "sl"))) / 10000.0
    cost_px = entry * (2 * float(pcm.TAKER_FEE_RATE) + slip_out)
    gain_px = (exit_px - entry) if side == "LONG" else (entry - exit_px)
    net_gain = gain_px - cost_px
    return {"tier": tier, "reason": reason, "bars_held": held,
            "actual_R": round(net_gain / risk, 3), "gross_R": round(gain_px / risk, 3),
            "mfe_R": round(mfe / risk, 3), "mae_R": round(mae / risk, 3),
            "entry": round(entry, 8), "atr_pct": round(atr / entry * 100, 3)}


def evaluate_once(client: Any, now_ms: int, limit: int = 60) -> dict[str, Any]:
    # done = already-recorded outcomes UNION permanently-unfetchable keys (negative cache) — without
    # the latter, a dead/renamed alt in the oldest matured batch is retried every run and can stall
    # all newer episodes forever (Opus review MEDIUM-1).
    _neg = _load_neg_keys()
    done = _load_done_keys() | _neg
    episodes = _collect_episodes(now_ms, done)[:limit]
    n_ok = n_skip = 0
    for ep in episodes:
        try:
            end = ep["ts"] + MATURITY_MS + TF_MS
            bars = of.fetch_klines_with_flow(ep["sym"], TF, months=0.02, end_ms=end,
                                             client=client, sleep_between=0.02, with_deriv=False)
            if not bars:
                _neg.add(ep["key"]); n_skip += 1; continue
            # entry bar = last CLOSED bar at/just before the trigger ts. of.ts_ms is the bar's
            # CLOSE time (Opus review HIGH-1), so the condition is just close_time <= trigger_ts;
            # outcomes then start at entry_idx+1 (strictly future) = no lookahead.
            entry_idx = None
            for i, b in enumerate(bars):
                if int(b["ts_ms"]) <= ep["ts"]:
                    entry_idx = i
            if entry_idx is None or entry_idx + 2 >= len(bars):
                _neg.add(ep["key"])                          # can't place entry -> negative-cache
                n_skip += 1
                continue
            sim = _simulate(bars, entry_idx, ep["side"])
            if sim is None:
                _neg.add(ep["key"])
                n_skip += 1
                continue
            rec = {"key": ep["key"], "sym": ep["sym"], "path": ep["path"], "side": ep["side"],
                   "trigger_ts": ep["ts"], "eval_ts": now_ms, **sim}
            with SHADOW.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=True) + "\n")
            n_ok += 1
        except Exception:
            _neg.add(ep["key"])                              # fetch/parse error -> negative-cache
            n_skip += 1
            continue
    _save_neg_keys(_neg)
    return {"evaluated": n_ok, "skipped": n_skip, "pending_pool": len(episodes), "neg_cached": len(_neg)}


def report() -> dict[str, Any]:
    if not SHADOW.exists():
        return {"n": 0}
    rows = []
    for ln in SHADOW.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(ln))
        except Exception:
            continue
    def _mean(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 3) if xs else None

    by = defaultdict(list)
    for r in rows:
        by[r.get("path", "?")].append(r)
    out = {"n": len(rows), "by_path": {},
           "note": ("gross_R = raw directional edge, floored at -1 (fee-free); mean_R = net of tier "
                    "costs (micro slip is punishing). liq = mid+major only (where the bot actually "
                    "trades). Exit is PESSIMISTIC (SL-before-TP same-bar) so this is a LOWER BOUND — "
                    "positive here = strong; negative could be the pessimism, not proof of no-edge.")}
    for path, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        gross = [_num(r.get("gross_R")) for r in rs]
        liq = [r for r in rs if r.get("tier") in ("mid", "major")]
        liq_net = [_num(r.get("actual_R")) for r in liq]
        gw = sum(1 for x in gross if x is not None and x > 0)
        out["by_path"][path] = {
            "n": len(rs), "n_liq": len(liq),
            "gross_R": _mean(gross), "gross_win": round(gw / len(rs), 3) if rs else None,
            "net_R_all": _mean([_num(r.get("actual_R")) for r in rs]),
            "net_R_liq": _mean(liq_net),
            "tp": sum(1 for r in rs if r.get("reason") == "tp"),
            "sl": sum(1 for r in rs if r.get("reason") == "sl"),
            "timeout": sum(1 for r in rs if r.get("reason") == "timeout"),
            # verdict keys on GROSS (direction) at n>=25: pessimistic, so we only KILL on clearly
            # negative gross, and FLAG promising on positive gross.
            "verdict": ("PROMISING" if len(rs) >= 25 and (_mean(gross) or -9) > 0.05 else
                        "NO-EDGE (even gross)" if len(rs) >= 25 and (_mean(gross) or 9) < -0.10 else
                        "need n>=25" if len(rs) < 25 else "flat"),
        }
    return out


def _hb(last: dict[str, Any]) -> None:
    try:
        from timebase import utc_now
        import os as _os
        HEARTBEAT.write_text(json.dumps({"agent": "shadow_trigger_eval", "pid": _os.getpid(),
                                         "ts": utc_now(), "updated_at": utc_now(),
                                         "status": "running", "last_run": last}), encoding="utf-8")
    except Exception:
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Shadow trigger-path edge evaluator (paper, read-only)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--interval-seconds", type=float, default=900.0)
    a = ap.parse_args()
    if a.report:
        print(json.dumps(report(), indent=1))
        raise SystemExit(0)
    from tradingagents.binance.client import spot_client

    def _run():
        c = spot_client()
        res = evaluate_once(c, int(time.time() * 1000))
        res["report"] = report()
        _hb(res)
        return res

    if a.once:
        print(json.dumps(_run(), default=str))
    else:
        import os
        stop = LT_DIR / "STOP_SHADOW_TRIGGER_EVAL"
        while not stop.exists():
            try:
                _run()
            except Exception as exc:
                _hb({"error": str(exc)[:200]})
            t = time.time() + a.interval_seconds
            while time.time() < t and not stop.exists():
                time.sleep(2)
