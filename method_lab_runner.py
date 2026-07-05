"""Method Lab runner — the 24/7 self-expanding research loop.

Each round it (1) asks the LLM to PROPOSE new candidate methods encoding how people
actually trade (in the safe DSL), (2) strictly validates them, (3) merges them with
the seed + accumulated pool, (4) fetches fresh real klines and re-curates the WHOLE
pool with method_lab.run_lab. Survivors feed the live bot; killed methods are
remembered so the proposer stops re-suggesting failures. No method is hardcoded as
true — the data curates. Paper/offline: never places an order.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

import method_lab as ml
from method_seeds import SEED_METHODS

ROOT = Path(__file__).resolve().parent
POOL = ml.LAB_DIR / "methods_pool.jsonl"
HEARTBEAT = ROOT / "state" / "method_lab_heartbeat.json"

# owner (2026-07-05): scope must be BROAD — universe is now DYNAMIC: top-N USDT
# perps by 24h quote volume (fallback to a fixed core list if the ticker fails).
UNIVERSE_TOP_N = int(os.environ.get("LAB_UNIVERSE_TOP_N", "100"))
LAB_MONTHS = float(os.environ.get("LAB_MONTHS", "2.0"))
_FALLBACK_COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "AVAXUSDT",
                   "LINKUSDT", "DOGEUSDT", "LTCUSDT", "ATOMUSDT", "APTUSDT", "ARBUSDT", "INJUSDT",
                   "NEARUSDT", "OPUSDT", "SUIUSDT", "TIAUSDT"]


def lab_universe(client) -> list[str]:
    try:
        ticks = client.futures_ticker()
        rows = [(t["symbol"], float(t.get("quoteVolume", 0) or 0)) for t in ticks
                if t.get("symbol", "").endswith("USDT") and "_" not in t["symbol"]]
        rows.sort(key=lambda x: -x[1])
        uni = [s for s, v in rows[:UNIVERSE_TOP_N] if v >= 5_000_000]
        return uni or _FALLBACK_COINS
    except Exception:
        return _FALLBACK_COINS

# whitelist for validating proposed methods (must match feature_frame keys)
FEATS = {"rsi14", "px_vs_ema20", "px_vs_ema50", "px_vs_ema200", "ema_stack",
         "vol_ratio", "ret5", "ret20", "close", "ema4h_state", "ema4h_cross",
         "hour_utc", "range20_pct", "brk20_pct", "brkdn20_pct",
         "streak_down", "streak_up", "dd96_pct", "rally96_pct", "atr_pct", "dow",
         "streak", "bar_z", "close_pos", "funding_rate_bps", "funding_z", "dd_from_high96_pct"}
OPS = {"<", "<=", ">", ">=", "=="}


def validate_method(m: dict[str, Any]) -> dict[str, Any] | None:
    """Accept only a well-formed method over whitelisted features with sane risk —
    a proposed method is untrusted input."""
    try:
        mid = str(m["id"]).strip()[:40]
        side = m.get("side")
        conds = m.get("when")
        if side not in ("LONG", "SHORT") or not mid or not isinstance(conds, list) or not (1 <= len(conds) <= 5):
            return None
        clean = []
        for c in conds:
            if c.get("feat") not in FEATS or c.get("op") not in OPS:
                return None
            clean.append({"feat": c["feat"], "op": c["op"], "val": float(c["val"])})
        sl = float(m.get("sl_pct", 1.5))
        tp = float(m.get("tp_pct", 2.5))
        if not (0.3 <= sl <= 6 and 0.3 <= tp <= 12):
            return None
        # strip markup chars — proposed text reaches the dashboard via innerHTML
        _cln = lambda x: str(x).replace("<", "").replace(">", "").replace("&", " ")
        return {"id": _cln(mid), "name": _cln(m.get("name", mid))[:60], "desc": _cln(m.get("desc", ""))[:160],
                "side": side, "when": clean, "sl_pct": round(sl, 2), "tp_pct": round(tp, 2)}
    except Exception:
        return None


def _load_pool() -> list[dict[str, Any]]:
    if not POOL.exists():
        return []
    out = []
    for line in POOL.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def _save_pool(methods: list[dict[str, Any]]) -> None:
    ml.LAB_DIR.mkdir(parents=True, exist_ok=True)
    # evict oldest beyond a cap — an ever-growing pool re-tests dead ideas forever
    # and (pre-BH) was tightening the significance bar toward impossibility.
    if len(methods) > 150:
        methods = methods[-150:]
    with POOL.open("w", encoding="utf-8") as fh:
        for m in methods:
            fh.write(json.dumps(m) + "\n")


def propose_methods(existing_ids: set[str], killed_descs: list[str], k: int = 6,
                    existing_hashes: set[str] | None = None) -> list[dict[str, Any]]:
    """Ask the LLM for k NEW candidate methods encoding real trading approaches, as
    DSL JSON. Best-effort: returns [] on any failure (the loop still re-tests the
    pool). This is the 'learn from how others trade' channel — data still decides.

    Second brain (P2): every candidate passes the deterministic NOVELTY GATE
    before it can cost any compute. The LLM can rephrase prose; it cannot
    rephrase a canonical hash — REJECT_EXACT (same side+conditions+sl/tp as any
    prior trial / seed / pool entry) is dropped; FLAG_NEAR (bucketed threshold
    twin) is accepted but logged. Every verdict lands verbatim in the
    quarantined `proposals` table (audit trail; feeds no gate)."""
    try:
        import llm_trader as lt
        base, key = lt._env_llm()
        if not base or not key:
            return []
        sys = (
            "You are a quant researcher proposing CANDIDATE crypto 15m futures methods to be BACKTESTED "
            "(you are NOT trading). Encode real, commonly-shared trading ideas (trend-following, mean-reversion, "
            "breakout, pullback, divergence-proxy, volume-confirmation, regime filters) as a strict JSON DSL. "
            f"Return ONLY a JSON array of {k} method objects. Each: {{id, name, desc, side:'LONG'|'SHORT', "
            "when:[{feat,op,val}...], sl_pct, tp_pct}}. Allowed feat: rsi14, px_vs_ema20, px_vs_ema50, "
            "px_vs_ema200 (percent above/below EMA), ema_stack (-1 bear / 0 / 1 bull), vol_ratio (x vs 20-bar avg), "
            "ret5, ret20 (percent). Allowed op: <,<=,>,>=,==. Use 1-4 conditions, sl_pct 0.5-4, tp_pct 1-8, and "
            "aim for tp_pct >= 1.5*sl_pct. Be creative and DIVERSE — combine features across families. Do NOT "
            "re-propose these already-FAILED ideas: " + "; ".join(killed_descs[:16]))
        body = json.dumps({"model": lt.MODEL, "max_tokens": 8000, "temperature": 0.8,
                           "reasoning_effort": getattr(lt, "REASONING_EFFORT", "high"),
                           "messages": [{"role": "system", "content": sys},
                                        {"role": "user", "content": "Propose the methods as a JSON array now."}]}).encode()
        req = urllib.request.Request(base + "/chat/completions", data=body,
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + key}, method="POST")
        with urllib.request.urlopen(req, timeout=120) as r:
            txt = json.loads(r.read().decode())["choices"][0]["message"]["content"]
        s, e = txt.find("["), txt.rfind("]")
        raw = json.loads(txt[s:e + 1]) if s >= 0 and e > s else []
        out = []
        try:
            import brain
        except Exception:
            brain = None
        for m in raw:
            v = validate_method(m)
            if not v or v["id"] in existing_ids:
                continue
            if brain is not None:
                try:
                    gate, _rows = brain.novelty_gate(v, extra_hashes=existing_hashes)
                    brain.record_proposal(v, gate)
                    if gate == "REJECT_EXACT":
                        continue                      # already tried — never re-spend compute
                except Exception:
                    pass                              # gate failure must not kill proposals
            out.append(v)
        return out
    except Exception:
        return []


def run_once(client: Any, propose: bool = True) -> dict[str, Any]:
    pool = _load_pool()
    by_id = {m["id"]: m for m in SEED_METHODS}
    for m in pool:                       # accumulated proposals override/extend seeds
        by_id[m["id"]] = m
    killed_descs = []
    try:
        killed_descs = [json.loads(l).get("desc", "") for l in
                        (ml.KILLED.read_text(encoding="utf-8").splitlines() if ml.KILLED.exists() else [])]
    except Exception:
        pass
    if propose:
        try:
            from method_canonical import method_hash
            existing_hashes = {method_hash(m) for m in by_id.values()}
        except Exception:
            existing_hashes = None
        new = propose_methods(set(by_id), [d for d in killed_descs if d], k=6,
                              existing_hashes=existing_hashes)
        for m in new:
            by_id[m["id"]] = m
        # persist the growing pool (everything beyond the built-in seeds)
        _save_pool([m for mid, m in by_id.items() if mid not in {s["id"] for s in SEED_METHODS}])

    methods = list(by_id.values())
    # fetch fresh frames
    now = int(time.time() * 1000)
    frames = {}
    import orderflow_data as of
    for c in lab_universe(client):
        try:
            bars = of.fetch_klines_with_flow(c, "15m", months=LAB_MONTHS, end_ms=now, client=client, sleep_between=0.02)
            try:
                fund = of.fetch_funding_series(c, months=LAB_MONTHS, end_ms=now, client=client)
            except Exception:
                fund = None
            rows = ml.feature_frame(bars, funding=fund)
            if len(rows) >= 260:
                frames[c] = rows
        except Exception:
            continue
    out = ml.run_lab(methods, frames)
    # Second brain (P2): register FIRST-EVER evaluations as trials (the DSR trial
    # count must include every idea ever tested). Re-evaluations of registered
    # methods are 3-hourly screening repeats on overlapping data — recording each
    # would spam the registry; deep_validation runs are the authoritative re-trials.
    try:
        import brain
        from method_canonical import method_hash
        known = brain.known_hashes()
        fresh_rows, fresh_defs = [], {}
        for r in out.get("results", []):
            d = by_id.get(r.get("id"))
            if not d or method_hash(d) in known:
                continue
            fresh_rows.append({**r, "oos_win": r.get("oos_win_rate"),
                               "oos_net_pct": r.get("oos_total_net_pct"),
                               "opt_sl": d.get("sl_pct"), "opt_tp": d.get("tp_pct")})
            fresh_defs[r["id"]] = d
        if fresh_rows:
            brain.record_trials(fresh_rows, fresh_defs, source="lab_round",
                                universe=f"lab_top{UNIVERSE_TOP_N}", timeframe="15m",
                                months=LAB_MONTHS)
    except Exception:
        pass
    HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    HEARTBEAT.write_text(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                     "ts_ms": now, "tested": len(methods), "coins": len(frames),
                                     **out["ledger"]}, indent=1), encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=10800.0)   # 3h default
    ap.add_argument("--no-propose", action="store_true")
    args = ap.parse_args()
    os.environ.setdefault("INGEST_DECISION_CANDLES", "0")
    from tradingagents.binance.client import spot_client
    client = spot_client()
    while True:
        try:
            out = run_once(client, propose=not args.no_propose)
            print(json.dumps({"lab": out["ledger"]}, default=str))
        except Exception as e:
            print(json.dumps({"error": repr(e)[:200]}))
        if args.once:
            break
        time.sleep(max(600.0, args.interval))


if __name__ == "__main__":
    main()
