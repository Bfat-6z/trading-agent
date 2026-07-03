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

COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "AVAXUSDT",
         "LINKUSDT", "DOGEUSDT", "LTCUSDT", "ATOMUSDT", "APTUSDT", "ARBUSDT", "INJUSDT",
         "NEARUSDT", "OPUSDT", "SUIUSDT", "TIAUSDT"]

# whitelist for validating proposed methods (must match feature_frame keys)
FEATS = {"rsi14", "px_vs_ema20", "px_vs_ema50", "px_vs_ema200", "ema_stack",
         "vol_ratio", "ret5", "ret20", "close"}
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
        return {"id": mid, "name": str(m.get("name", mid))[:60], "desc": str(m.get("desc", ""))[:160],
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
    with POOL.open("w", encoding="utf-8") as fh:
        for m in methods:
            fh.write(json.dumps(m) + "\n")


def propose_methods(existing_ids: set[str], killed_descs: list[str], k: int = 6) -> list[dict[str, Any]]:
    """Ask the LLM for k NEW candidate methods encoding real trading approaches, as
    DSL JSON. Best-effort: returns [] on any failure (the loop still re-tests the
    pool). This is the 'learn from how others trade' channel — data still decides."""
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
        for m in raw:
            v = validate_method(m)
            if v and v["id"] not in existing_ids:
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
        new = propose_methods(set(by_id), [d for d in killed_descs if d], k=6)
        for m in new:
            by_id[m["id"]] = m
        # persist the growing pool (everything beyond the built-in seeds)
        _save_pool([m for mid, m in by_id.items() if mid not in {s["id"] for s in SEED_METHODS}])

    methods = list(by_id.values())
    # fetch fresh frames
    now = int(time.time() * 1000)
    frames = {}
    import orderflow_data as of
    for c in COINS:
        try:
            bars = of.fetch_klines_with_flow(c, "15m", months=1.0, end_ms=now, client=client, sleep_between=0.02)
            rows = ml.feature_frame(bars)
            if len(rows) >= 260:
                frames[c] = rows
        except Exception:
            continue
    out = ml.run_lab(methods, frames)
    HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
    HEARTBEAT.write_text(json.dumps({"ts": now, "tested": len(methods), "coins": len(frames),
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
