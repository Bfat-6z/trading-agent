"""Forward-paper the candidate lead the meta-loop surfaced (donchian committed
breakout + Kaufman efficiency regime + volume, WIDE stops, 1h) — REAL-TIME
out-of-sample validation.

Why forward-paper, not more backtest: after ~4000 cumulative trials the in-sample
DSR bar is saturated; a persistent-but-faint lead (+0.09-0.12R/component that held
under 3-4x more sample) cannot be confirmed by more in-sample search (grinding only
raises the bar + risks overfit). The honest test is out-of-sample TIME: detect the
signal on the just-CLOSED bar going forward, open a PAPER position (no live order),
resolve it on FUTURE closed bars with the real tiered cost model, and accrue
expectancy over weeks. A lead is NOT a confirmed edge — never live from this.

Paper-only; imports NO live-order path; ALLOW_LIVE_ORDERS never set.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import backtest_chart_signal as cs
import paper_cost_model as pcm
import strategy_compiler as sc
import universe_selector as us

ROOT = Path(__file__).resolve().parent
FS_DIR = ROOT / "state" / "forward_strategy"
POSITIONS = FS_DIR / "positions.jsonl"       # currently-open paper positions
CLOSED = FS_DIR / "closed.jsonl"             # resolved paper trades (append-only)
PID_FILE = FS_DIR / "forward_strategy_paper.pid"
STOP_FILE = FS_DIR / "forward_strategy_paper.stop"
HEARTBEAT_PATH = FS_DIR / "forward_strategy_paper_heartbeat.json"

TF = "1h"
MAX_HOLD_BARS = 48
MIN_SAMPLE = 200          # out-of-sample trades needed before any read-out
CANDLE_LIMIT = 320        # bars per fetch (enough for indicators + resolution)

# FROZEN candidate spec (the meta-loop lead). Not tuned further — forward-test is
# a fixed, pre-registered hypothesis so results are honest out-of-sample.
FROZEN_SPEC = {
    "name": "lead_donchian_committed_kaufman_vol",
    "direction": "LONG",   # evaluated both ways below
    "entry": {"all": [
        {"block": "donchian_breakout_committed", "params": {"n": 20, "k": 0.25}},
        {"block": "kaufman_efficiency_regime", "params": {"er_window": 20, "er_min": 0.35}},
        {"block": "volume_min_ratio", "params": {"min_ratio": 1.3}},
    ]},
    "exit": {"sl_atr": 2.5, "tp_atr": 5.0, "min_rr": 1.5, "regime_exit": True, "max_hold_bars": MAX_HOLD_BARS},
}
DIRECTIONS = ["LONG", "SHORT"]


CANDIDATE_FILE = ROOT / "state" / "agent_memory" / "forward_candidate.json"


def _active_spec() -> dict[str, Any]:
    """The spec being forward-tested. LINKED to research: if the meta-loop has
    written a forward_candidate.json (its current best lead), use that; else fall
    back to the built-in FROZEN_SPEC. This closes the meta-loop -> forward-paper
    link so a newly-discovered lead flows here automatically (on next cycle)."""
    try:
        if CANDIDATE_FILE.exists():
            c = json.loads(CANDIDATE_FILE.read_text(encoding="utf-8"))
            spec = c.get("spec") if isinstance(c, dict) else None
            if isinstance(spec, dict) and spec.get("entry"):
                return spec
    except Exception:
        pass
    return FROZEN_SPEC


def _spec_for(direction: str) -> dict[str, Any]:
    s = json.loads(json.dumps(_active_spec()))
    s["direction"] = direction
    return s


def _load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _rewrite(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, default=str) + "\n" for r in rows), encoding="utf-8")


def _apply_slip(price: float, side: str, bps: float, *, entry: bool) -> float:
    f = float(bps) / 10000.0
    if entry:
        return price * (1 + f) if side == "LONG" else price * (1 - f)
    return price * (1 - f) if side == "LONG" else price * (1 + f)


def detect_and_open(client: Any, symbols: list[str], quote_vols: dict[str, float], now_ms: int,
                    now_iso: str) -> int:
    """Detect the frozen spec firing on the JUST-CLOSED bar; open a paper position
    (one per symbol+direction max). Entry = last close with adverse slippage."""
    import backtest_data_fetcher as bf
    open_pos = _load(POSITIONS)
    open_keys = {(p["symbol"], p["direction"]) for p in open_pos}
    opened = 0
    for sym in symbols:
        try:
            bars = bf.fetch_history(sym, TF, months=CANDLE_LIMIT * 3600 * 1000 / (30 * 24 * 3600 * 1000),
                                    end_ms=now_ms, client=client, sleep_between=0.02)
            if len(bars) < 80:
                continue
            df = cs.compute_indicators(bars)
            i = len(df) - 1
            atr = float(df.iloc[i]["atr"]) if df.iloc[i]["atr"] == df.iloc[i]["atr"] else 0.0
            if atr <= 0:
                continue
            for direction in DIRECTIONS:
                if (sym, direction) in open_keys:
                    continue
                spec = _spec_for(direction)
                mask = sc.compute_mask(spec, df, df)   # spec has no HTF block
                if not bool(mask.iloc[i]):
                    continue
                tier = pcm.liquidity_tier(quote_vols.get(sym, 0.0))
                entry = _apply_slip(float(df.iloc[i]["close"]), direction, pcm.fill_bps(tier), entry=True)
                # SL/TP from the ACTIVE spec's exit (linked to the research candidate)
                ex = spec.get("exit", {}); sl_atr = float(ex.get("sl_atr", 2.5)); tp_atr = float(ex.get("tp_atr", 5.0))
                sl = entry - sl_atr * atr if direction == "LONG" else entry + sl_atr * atr
                tp = entry + tp_atr * atr if direction == "LONG" else entry - tp_atr * atr
                open_pos.append({
                    "symbol": sym, "direction": direction, "decision_cutoff": now_iso,
                    "entry_ts": int(df.iloc[i]["ts_ms"]), "entry": entry, "sl": sl, "tp": tp,
                    "atr": atr, "quote_volume": quote_vols.get(sym, 0.0), "tier": tier,
                    "spec_id": sc.spec_id(spec),
                })
                open_keys.add((sym, direction))
                opened += 1
        except Exception:
            continue
    _rewrite(POSITIONS, open_pos)
    return opened


def resolve_open(client: Any, now_ms: int) -> int:
    """Resolve open paper positions on FUTURE closed bars (strictly after entry),
    pessimistic SL-first, real tiered exit cost. Closed ones move to CLOSED."""
    import backtest_data_fetcher as bf
    open_pos = _load(POSITIONS)
    if not open_pos:
        return 0
    still_open, newly_closed = [], []
    for p in open_pos:
        try:
            bars = bf.fetch_history(p["symbol"], TF, months=(MAX_HOLD_BARS + 5) * 3600 * 1000 / (30 * 24 * 3600 * 1000),
                                    end_ms=now_ms, client=client, sleep_between=0.02)
            fut = [b for b in bars if int(cs.compute_indicators([b]).iloc[0]["ts_ms"]) > int(p["entry_ts"])]
        except Exception:
            still_open.append(p); continue
        side, sl, tp = p["direction"], float(p["sl"]), float(p["tp"])
        tier = p.get("tier") or pcm.liquidity_tier(p.get("quote_volume", 0.0))
        exit_px = reason = None
        for k, b in enumerate(fut):
            hi, lo = float(b["high"]), float(b["low"])
            hit_sl = (lo <= sl) if side == "LONG" else (hi >= sl)
            hit_tp = (hi >= tp) if side == "LONG" else (lo <= tp)
            if hit_sl:   # pessimistic: SL first on an ambiguous bar
                exit_px = _apply_slip(sl, side, pcm.fill_bps(tier, is_stop=True), entry=False); reason = "sl"; break
            if hit_tp:
                exit_px = _apply_slip(tp, side, pcm.fill_bps(tier), entry=False); reason = "tp"; break
            if k + 1 >= MAX_HOLD_BARS:
                exit_px = _apply_slip(float(b["close"]), side, pcm.fill_bps(tier), entry=False); reason = "timeout"; break
        if exit_px is None:
            still_open.append(p); continue
        entry = float(p["entry"]); risk = abs(entry - sl)
        gross = (exit_px - entry) if side == "LONG" else (entry - exit_px)
        fee = (entry + abs(exit_px)) * float(pcm.TAKER_FEE_RATE)
        net = gross - fee
        r = net / risk if risk > 0 else 0.0
        newly_closed.append({**{k: p[k] for k in ("symbol", "direction", "decision_cutoff", "entry_ts", "spec_id")},
                             "exit": exit_px, "reason": reason, "net": net, "r_multiple": r})
    _rewrite(POSITIONS, still_open)
    if newly_closed:
        FS_DIR.mkdir(parents=True, exist_ok=True)
        with open(CLOSED, "a", encoding="utf-8") as fh:
            for c in newly_closed:
                fh.write(json.dumps(c, default=str) + "\n")
    return len(newly_closed)


def summarize() -> dict[str, Any]:
    closed = _load(CLOSED)
    rs = [float(c.get("r_multiple", 0)) for c in closed]
    n = len(rs)
    mean = sum(rs) / n if n else 0.0
    wins = sum(1 for r in rs if r > 0)
    return {"closed": n, "open": len(_load(POSITIONS)), "min_sample": MIN_SAMPLE,
            "mean_r": round(mean, 4), "win_rate": round(wins / n, 4) if n else None,
            "verdict": "insufficient_sample_still_accruing" if n < MIN_SAMPLE else "readable",
            "note": "Forward out-of-sample; a positive mean here after MIN_SAMPLE is a CANDIDATE "
                    "edge to consider — still not automatic live money."}


def run_once() -> dict[str, Any]:
    import time as _t
    from timebase import utc_now
    from tradingagents.binance.client import spot_client
    client = spot_client()
    now_ms = int(_t.time() * 1000)
    uni = us.select_universe(client, end_ms=now_ms, months=9.0, timeframe="1h",
                             min_daily_quote_volume=50_000_000.0, max_symbols=9)
    symbols = uni["selected"]
    quote_vols = {s: uni["detail"].get(s, 0.0) for s in symbols}
    resolved = resolve_open(client, now_ms)
    opened = detect_and_open(client, symbols, quote_vols, now_ms, utc_now())
    s = summarize()
    return {"opened": opened, "resolved": resolved, **s}


def _write_heartbeat(last: dict[str, Any], status: str = "running") -> None:
    from atomic_state import write_json_atomic
    from timebase import utc_now
    write_json_atomic(HEARTBEAT_PATH, {"agent": "forward_strategy_paper", "pid": os.getpid(),
                                       "ts": utc_now(), "updated_at": utc_now(), "status": status, "last_run": last})


def _interruptible_sleep(seconds: float) -> bool:
    import time as _t
    deadline = _t.time() + max(0.0, seconds)
    while _t.time() < deadline:
        if STOP_FILE.exists():
            return False
        _t.sleep(min(1.0, max(0.0, deadline - _t.time())))
    return not STOP_FILE.exists()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Forward-paper the meta-loop lead (paper-only)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval-seconds", type=float, default=1800.0)   # every 30m (1h bars)
    args = ap.parse_args()
    FS_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    if args.once:
        print(json.dumps(run_once(), default=str))
    else:
        while not STOP_FILE.exists():
            try:
                res = run_once()
            except Exception as exc:
                res = {"error": str(exc)[:200]}
            _write_heartbeat(res)
            if not _interruptible_sleep(args.interval_seconds):
                break
        _write_heartbeat({}, status="stopped")
