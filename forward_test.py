"""Forward-test below-proven-bar candidate methods on LIVE bars (true out-of-sample).

Paper/offline: keeps its OWN shadow ledger, NEVER touches the mission account and
NEVER places an order. Replicates method_lab.backtest_method fill/exit EXACTLY
(entry at the firing bar's close, SL-checked-before-TP pessimistic walk, 16-bar
timeout, same round-trip fee) so the ONLY difference vs the backtest that produced
the candidate's edge is that these bars are fresh and unseen. If the edge persists
on this live data, the candidate earns promotion to the real survivors.

Owner (2026-07-05): 'cam forward-test um_reclaim_06 di'.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

os.environ.setdefault("INGEST_DECISION_CANDLES", "0")

import method_lab as ml
from method_seeds import SEED_METHODS

ROOT = Path(__file__).resolve().parent
FT = ROOT / "state" / "forward_test"
POSN = FT / "shadow_positions.jsonl"
CLOSED = FT / "shadow_closed.jsonl"
STATS = FT / "shadow_stats.json"
HB = ROOT / "state" / "forward_test_heartbeat.json"
WATCH = ROOT / "state" / "method_lab" / "forward_watch.json"

TF = "15m"
FEE_RT = ml.FEE_RT
TIMEOUT_BARS = ml.TIMEOUT_BARS
MIN_QVOL = 50_000_000.0          # same liquid universe the candidate was validated on
UNIV_MAX = 60
DEFAULT_IDS = ["um_reclaim_06"]


def _load(p: Path) -> list[dict]:
    out = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def _rewrite(p: Path, rows: list[dict]) -> None:
    FT.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _append(p: Path, row: dict) -> None:
    FT.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def watch_ids() -> list[str]:
    try:
        w = json.loads(WATCH.read_text(encoding="utf-8"))
        ids = [c["id"] for c in w.get("candidates", [])]
        return ids or DEFAULT_IDS
    except Exception:
        return DEFAULT_IDS


def watch_params() -> dict[str, dict]:
    """Per-candidate deep-optimal exit params {id: {sl_pct, tp_pct, timeout}} so the
    forward test replicates the DEEP-VALIDATED version, not the pool default."""
    out = {}
    try:
        w = json.loads(WATCH.read_text(encoding="utf-8"))
        for c in w.get("candidates", []):
            p = {}
            if c.get("sl_pct") is not None:
                p["sl_pct"] = float(c["sl_pct"])
            if c.get("tp_pct") is not None:
                p["tp_pct"] = float(c["tp_pct"])
            if c.get("timeout") is not None:
                p["timeout"] = int(c["timeout"])
            if p:
                out[c["id"]] = p
    except Exception:
        pass
    return out


def load_methods(ids: list[str]) -> list[dict]:
    defs = {m["id"]: m for m in SEED_METHODS}
    pool = ROOT / "state" / "method_lab" / "methods_pool.jsonl"
    if pool.exists():
        for line in pool.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    m = json.loads(line)
                    defs[m["id"]] = m
                except Exception:
                    pass
    return [defs[i] for i in ids if i in defs]


def universe(client) -> list[str]:
    ticks = client.futures_ticker()
    rows = sorted(
        [(t["symbol"], float(t.get("quoteVolume", 0) or 0)) for t in ticks
         if t.get("symbol", "").endswith("USDT") and "_" not in t["symbol"]
         and float(t.get("quoteVolume", 0) or 0) >= MIN_QVOL],
        key=lambda x: -x[1])
    return [s for s, _ in rows[:UNIV_MAX]]


def _closed_bars(client, sym: str, now_ms: int) -> list[dict]:
    import orderflow_data as of
    bars = of.fetch_klines_with_flow(sym, TF, months=0.12, end_ms=now_ms,
                                     client=client, sleep_between=0.02)
    return [b for b in bars if b.get("is_final", True)]


def resolve_open(client, now_ms: int) -> int:
    open_pos = _load(POSN)
    still, closed_n = [], 0
    for p in open_pos:
        try:
            bars = _closed_bars(client, p["symbol"], now_ms)
            after = [b for b in bars if int(b["ts_ms"]) > int(p["entry_ts_ms"])]
            side, entry, sl, tp = p["side"], p["entry"], p["sl"], p["tp"]
            timeout = int(p.get("timeout", TIMEOUT_BARS))     # per-method (deep-optimal) hold cap
            exit_px, reason, used = None, None, 0
            for k, b in enumerate(after[:timeout]):
                used = k + 1
                hi, low = float(b["high"]), float(b["low"])
                if side == "LONG":
                    if low <= sl:
                        exit_px, reason = sl, "sl"; break
                    if hi >= tp:
                        exit_px, reason = tp, "tp"; break
                else:
                    if hi >= sl:
                        exit_px, reason = sl, "sl"; break
                    if low <= tp:
                        exit_px, reason = tp, "tp"; break
            if exit_px is None and len(after) >= timeout:
                exit_px, reason, used = float(after[timeout - 1]["close"]), "timeout", timeout
            if exit_px is None:            # not enough bars elapsed yet -> keep open
                still.append(p); continue
            gross = (exit_px / entry - 1) if side == "LONG" else (entry - exit_px) / entry
            net = gross - FEE_RT
            slp = abs(entry - sl) / entry
            _append(CLOSED, {**p, "exit": exit_px, "reason": reason, "net": round(net, 6),
                             "r": round(net / slp, 4) if slp else 0.0,
                             "bars_held": used, "closed_ts_ms": now_ms})
            closed_n += 1
        except Exception:
            still.append(p)               # transient error -> retry next cycle
    _rewrite(POSN, still)
    return closed_n


def scan_open(client, methods: list[dict], params: dict[str, dict], now_ms: int) -> int:
    open_syms = {p["symbol"] for p in _load(POSN)}
    opened = 0
    for sym in universe(client):
        if sym in open_syms:
            continue
        try:
            bars = _closed_bars(client, sym, now_ms)
            if len(bars) < 220:
                continue
            rows = ml.feature_frame(bars)
            if not rows:
                continue
            row, last = rows[-1], bars[-1]
            for m in methods:
                if ml.method_fires(row, m):
                    side = m.get("side", "LONG")
                    entry = float(last["close"])
                    pp = params.get(m["id"], {})   # deep-optimal exits override the pool default
                    slp = float(pp.get("sl_pct", m["sl_pct"])) / 100.0
                    tpp = float(pp.get("tp_pct", m["tp_pct"])) / 100.0
                    to = int(pp.get("timeout", TIMEOUT_BARS))
                    sl = entry * (1 - slp) if side == "LONG" else entry * (1 + slp)
                    tp = entry * (1 + tpp) if side == "LONG" else entry * (1 - tpp)
                    _append(POSN, {"symbol": sym, "method": m["id"], "side": side,
                                   "entry": entry, "sl": sl, "tp": tp, "timeout": to,
                                   "entry_ts_ms": int(last["ts_ms"]), "opened_ts_ms": now_ms})
                    opened += 1
                    break
        except Exception:
            continue
    return opened


def write_stats(methods: list[dict]) -> dict:
    from timebase import utc_now
    closed = _load(CLOSED)
    openp = _load(POSN)
    per: dict[str, dict] = {}
    for t in closed:
        d = per.setdefault(t.get("method", "?"), {"n": 0, "wins": 0, "net": 0.0, "rs": []})
        d["n"] += 1
        d["net"] += t.get("net", 0.0)
        d["rs"].append(t.get("r", 0.0))
        if t.get("net", 0.0) > 0:
            d["wins"] += 1
    out = {}
    for m in methods:
        mid = m["id"]
        d = per.get(mid, {"n": 0, "wins": 0, "net": 0.0, "rs": []})
        n = d["n"]
        row = {"n": n, "open": len([p for p in openp if p.get("method") == mid]),
               "win_rate": round(d["wins"] / n, 3) if n else None,
               "mean_r": round(sum(d["rs"]) / n, 4) if n else None,
               "net_pct": round(d["net"] * 100, 2)}
        # once enough fresh trades, run the same scorecard for a live p-value
        if n >= 30:
            try:
                import llm_trader_scorecard as ls
                card = ls.scorecard([{"net": t["net"], "r": t["r"],
                                      "reason": t.get("reason", "")} for t in closed
                                     if t.get("method") == mid])
                row["live_pvalue"] = card.get("pvalue")
            except Exception:
                pass
        out[mid] = row
    STATS.write_text(json.dumps({"updated": utc_now(), "methods": out}, indent=1), encoding="utf-8")
    return out


def run_once(client) -> dict:
    from timebase import utc_now
    now_ms = int(time.time() * 1000)
    methods = load_methods(watch_ids())
    params = watch_params()
    closed_n = resolve_open(client, now_ms)
    opened = scan_open(client, methods, params, now_ms)
    stats = write_stats(methods)
    HB.write_text(json.dumps({"agent": "forward_test", "pid": os.getpid(), "ts": utc_now(),
                              "updated_at": utc_now(), "status": "running",
                              "watch": [m["id"] for m in methods],
                              "opened": opened, "closed": closed_n, "stats": stats}),
                  encoding="utf-8")
    return {"opened": opened, "closed": closed_n, "stats": stats}


def main() -> None:
    ap = argparse.ArgumentParser(description="Forward-test candidate methods (paper shadow ledger)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=300.0)
    a = ap.parse_args()
    from tradingagents.binance.client import spot_client
    try:
        (ROOT / "state" / "forward_test.pid").write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass
    client = spot_client()
    while True:
        try:
            r = run_once(client)
            print(json.dumps({"forward_test": r}, default=str)[:400])
        except Exception as e:
            print(json.dumps({"error": repr(e)[:200]}))
        if a.once:
            break
        time.sleep(max(120.0, a.interval))


if __name__ == "__main__":
    main()
