"""LANE FARM — 10 parallel paper channels, $100 each, different configs
(owner: 'mở 10 kênh trade, mỗi kênh 100u, xong rút tổng hợp bài học — chứ 1 cái lâu quá').

Each lane = an EXPERIMENT: own ledger, own config (method set + sizing), fills
exactly like forward_test (closed-bar walk, SL-before-TP, timeout, same fee).
All closes feed trade_autopsy (src='laneN') -> lesson mining pools the evidence;
ACTIVE lesson promotion still requires the MISSION cohort, so loose lanes can
only ever enrich, never gate. Lane 10 is a RANDOM-ENTRY control: it measures the
exit-engine's alpha floor — any lane not beating it has no entry edge.
Paper only. The mission account is untouched. Live stays LOCKED.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import time
from pathlib import Path

os.environ.setdefault("INGEST_DECISION_CANDLES", "0")

import method_lab as ml
from method_seeds import SEED_METHODS

ROOT = Path(__file__).resolve().parent
LDIR = ROOT / "state" / "lanes"
HB = ROOT / "state" / "lane_farm_heartbeat.json"
TF = "15m"
FEE = ml.FEE_RT
START_EQ = 100.0
BUST_EQ = 10.0
MIN_QVOL = 50e6
UNIV_N = 40
LEV = int(os.environ.get("MECH_LEV", "10"))
# mission-parity gap-gate multiplier (same env var the executor + method_matrix read)
GAP_LIQ_ATR_MULT = float(os.environ.get("MECH_GAP_LIQ_ATR_MULT", "3.0"))

# ---- lane configs. Owner: 'KHÔNG bỏ con nào — giữ hết + tạo lane mới test'. One lane
# per method (seeds + full pool + armed), plus a RANDOM control. Uniform 10% margin so
# the comparison isolates the METHOD's edge, not sizing. Each method keeps its own
# sl/tp/to. NO expectancy cull — every method (incl. backtest-negative ones) gets a lane,
# because backtest != live and the disciplined funnel (n>=50 + Šidák + persistence) is
# what gates the mission, not the lane roster. Ranking only orders the display; the cap
# is a runaway-safety ceiling — if it ever bites we LOG it (no silent drop).
MAX_LANES = int(os.environ.get("LANE_FARM_MAX", "400"))
MARGIN_PCT = 10
MAX_OPEN = 3


def _all_method_defs() -> dict:
    by_id = {m["id"]: dict(m) for m in SEED_METHODS}
    pool = ROOT / "state" / "method_lab" / "methods_pool.jsonl"
    if pool.exists():
        for l in pool.read_text(encoding="utf-8").splitlines():
            if l.strip():
                try:
                    x = json.loads(l); by_id[x["id"]] = x
                except Exception:
                    pass
    armed = ROOT / "state" / "method_lab" / "armed_methods.json"
    if armed.exists():
        try:
            d = json.loads(armed.read_text(encoding="utf-8"))
            for m in (d if isinstance(d, list) else d.get("methods", [])):
                by_id[m["id"]] = {**by_id.get(m["id"], {}), **m}
        except Exception:
            pass
    return by_id


def _safe_key(mid: str) -> str:
    return "".join(c if (c.isalnum() or c in "_-") else "_" for c in str(mid))[:48]


def lane_configs():
    defs = _all_method_defs()
    prio = {}                                        # rank slots by backtest expectancy
    try:
        mm = json.loads((ROOT / "state" / "memory" / "method_matrix_stats.json").read_text(encoding="utf-8"))
        prio = {k: (v.get("exp_net") if v.get("exp_net") is not None else -9)
                for k, v in (mm.get("methods") or {}).items()}
    except Exception:
        pass
    ids = sorted(defs.keys(), key=lambda i: -prio.get(i, -9))
    lanes = [{"k": "L00_random", "desc": "RANDOM control (sàn alpha)", "family": "control",
              "side": "NA", "margin": MARGIN_PCT, "sl": 1.0, "tp": 6.0, "to": 48,
              "max_open": MAX_OPEN, "methods": "RANDOM"}]
    seen = {"L00_random"}
    evaluable = 0
    for mid in ids:
        m = defs.get(mid) or {}
        if not (m.get("when") or m.get("conds")):    # need an evaluable DSL rule
            continue
        evaluable += 1
        if len(lanes) >= MAX_LANES:
            continue                                  # count the drop (logged below), don't break
        k = _safe_key(mid)
        if k in seen:
            continue
        seen.add(k)
        lanes.append({"k": k, "mid": mid,
                      "desc": (m.get("desc") or m.get("family") or mid)[:44],
                      "family": m.get("family") or "?", "side": m.get("side", "LONG"),
                      "margin": MARGIN_PCT, "sl": float(m.get("sl_pct") or 1.5),
                      "tp": float(m.get("tp_pct") or 3.0), "to": int(m.get("timeout") or 16),
                      "max_open": MAX_OPEN, "methods": [m]})
    dropped = evaluable - (len(lanes) - 1)            # -1 for the random control
    if dropped > 0:                                    # NO silent cull (owner: don't drop any)
        print(json.dumps({"warn": "lane cap hit", "evaluable": evaluable,
                          "lanes": len(lanes), "dropped": dropped, "cap": MAX_LANES}))
    return lanes

def _lp(k): return LDIR / k
def _load(p):
    try:
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        return []
def _append(p, r):
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(r) + "\n")
def _acct(k):
    p = _lp(k) / "account.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"equity": START_EQ, "trades": 0, "wins": 0, "busted": False}
def _save_acct(k, a):
    p = _lp(k) / "account.json"; p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(a), encoding="utf-8")

def run_once(client):
    import orderflow_data as of
    now = int(time.time() * 1000)
    ticks = client.futures_ticker()
    uni = sorted([(t["symbol"], float(t.get("quoteVolume", 0) or 0)) for t in ticks
                  if t.get("symbol", "").endswith("USDT") and "_" not in t["symbol"]
                  and float(t.get("quoteVolume", 0) or 0) >= MIN_QVOL], key=lambda x: -x[1])
    syms = [s for s, _ in uni[:UNIV_N]]
    frames = {}
    bar_ms = of._TF_MS[TF]
    for s in syms:                                  # ONE fetch pass shared by all 10 lanes
        try:
            bars = [b for b in of.fetch_klines_with_flow(s, TF, months=0.12, end_ms=now,
                                                         client=client, sleep_between=0.02)
                    if int(b["ts_ms"]) + 0 <= now]
            bars = [b for b in bars if b.get("is_final", True)]
            if len(bars) < 220:                  # same gate as forward_test
                continue
            rows = ml.feature_frame(bars)
            if rows:
                frames[s] = (bars, rows)
        except Exception:
            continue
    summary = {}
    rng = random.Random(now // bar_ms)              # deterministic per bar
    for cfg in lane_configs():
        k = cfg["k"]; a = _acct(k)
        cur_bar = now // bar_ms
        if a.get("busted"):
            summary[k] = {"equity": a["equity"], "busted": True}
            continue
        opens = _load(_lp(k) / "open.jsonl")
        closed_ids = {c.get("pos_id") for c in _load(_lp(k) / "closed.jsonl") if c.get("pos_id")}
        still, closed = [], 0
        for p in opens:
            if p.get("pos_id") and p["pos_id"] in closed_ids:
                continue                             # crash-replay guard: already booked (Codex #4)                              # resolve (same walk as forward_test)
            bt = frames.get(p["symbol"])
            if not bt:
                still.append(p); continue
            bars, _ = bt
            after = [b for b in bars if int(b["ts_ms"]) > int(p["entry_ts_ms"])]
            side, entry, sl, tp = p["side"], p["entry"], p["sl"], p["tp"]
            to = int(p["timeout"]); ex = rs = None; used = 0
            for i, b in enumerate(after[:to]):
                used = i + 1
                lo, hi = float(b["low"]), float(b["high"])
                if side == "LONG":
                    if lo <= sl: ex, rs = sl, "sl"; break
                    if hi >= tp: ex, rs = tp, "tp"; break
                else:
                    if hi >= sl: ex, rs = sl, "sl"; break
                    if lo <= tp: ex, rs = tp, "tp"; break
            if ex is None and len(after) >= to:
                ex, rs, used = float(after[to - 1]["close"]), "timeout", to
            if ex is None:
                still.append(p); continue
            gross = (ex / entry - 1) if side == "LONG" else (entry - ex) / entry
            netp = gross - FEE
            pnl = round(netp * p["margin"] * LEV, 4)
            a["equity"] = round(a["equity"] + pnl, 4)
            a["trades"] += 1; a["wins"] += 1 if pnl > 0 else 0
            rec = {**p, "exit": ex, "reason": rs, "net": netp, "pnl": pnl,
                   "r": round(netp / (abs(entry - sl) / entry), 3), "bars_held": used,
                   "closed_ts_ms": now, "method": p.get("method")}
            _append(_lp(k) / "closed.jsonl", rec)
            closed += 1
            try:
                import brain
                brain._insert_autopsy(brain._autopsy_row(
                    {**rec, "entry_feats": p.get("entry_feats")}, f"lane_{k}", None), "lane_close")
            except Exception:
                pass
        if a["equity"] <= BUST_EQ:                   # 'hết 100' -> lane halts, lesson recorded
            a["busted"] = True
        open_syms = {p["symbol"] for p in still}
        if not a.get("busted") and len(still) < cfg["max_open"]:
            for s in syms:
                if s in open_syms or len(still) >= cfg["max_open"]:
                    continue
                bars, rows = frames.get(s, (None, None))
                if not bars:
                    continue
                row, last = rows[-1], bars[-1]
                fire_m = None
                if cfg["methods"] == "RANDOM":
                    if a.get("rand_bar") == cur_bar:
                        continue                     # sample ONCE per bar (Codex: re-scan bias)
                    if rng.random() < 0.01:
                        fire_m = {"id": "random_ctl", "side": "LONG" if rng.random() < 0.5 else "SHORT"}
                else:
                    for m in cfg["methods"]:
                        if not m:
                            continue
                        if ml.method_fires(row, m):
                            fire_m = m; break
                if not fire_m:
                    continue
                # MISSION-PARITY gap-gate: the mission refuses fires where liquidation is
                # < GAP_LIQ_ATR_MULT(3) ATRs away (atr_pct > ~100/lev/3 ≈ 3.33% at x10),
                # so the lane MUST too — else lane edge harvested from high-vol coins the
                # mission would never touch inflates the promotion signal. Applied to the
                # RANDOM control as well so the alpha floor is measured on the same coins.
                atrp = row.get("atr_pct")             # fail-CLOSED on missing atr (Codex): a coin
                if atrp is None or float(atrp) <= 0:  # we can't risk-assess must be skipped, not fired
                    continue
                if float(atrp) * GAP_LIQ_ATR_MULT > 100.0 / max(1, LEV):
                    continue
                side = fire_m.get("side", "LONG")
                entry = float(last["close"])
                slp, tpp = cfg["sl"] / 100, cfg["tp"] / 100
                margin = round(a["equity"] * cfg["margin"] / 100, 4)
                if margin < 1:
                    continue
                feats = {f: row.get(f) for f in
                         ("rsi14", "ret20", "ret5", "vol_ratio", "funding_z", "dd96_pct",
                          "px_vs_ema200", "atr_pct", "close_pos", "ema_stack") if row.get(f) is not None}
                if cfg["methods"] == "RANDOM":
                    a["rand_bar"] = cur_bar
                still.append({"symbol": s, "method": fire_m["id"], "side": side, "entry": entry,
                              "sl": entry * (1 - slp) if side == "LONG" else entry * (1 + slp),
                              "tp": entry * (1 + tpp) if side == "LONG" else entry * (1 - tpp),
                              "timeout": cfg["to"], "margin": margin, "entry_feats": feats,
                              "entry_ts_ms": int(last["ts_ms"]), "opened_ts_ms": now,
                              "pos_id": f"{s}_{int(last['ts_ms'])}_{k}"})
                open_syms.add(s)
        op = _lp(k) / "open.jsonl"
        op.parent.mkdir(parents=True, exist_ok=True)
        tmp = op.with_suffix(".tmp")                  # atomic replace (Codex #4)
        tmp.write_text("\n".join(json.dumps(x) for x in still) + ("\n" if still else ""), encoding="utf-8")
        os.replace(tmp, op)
        _save_acct(k, a)
        summary[k] = {"equity": a["equity"], "trades": a["trades"],
                      "win": round(a["wins"] / a["trades"], 3) if a["trades"] else None,
                      "open": len(still), "closed_now": closed, "busted": a.get("busted", False),
                      "desc": cfg.get("desc", ""), "family": cfg.get("family", "?"),
                      "side": cfg.get("side", "LONG"), "mid": cfg.get("mid", k)}
    LDIR.mkdir(parents=True, exist_ok=True)
    (LDIR / "summary.json").write_text(json.dumps(
        {"ts": now, "lanes": summary}, indent=1), encoding="utf-8")
    HB.write_text(json.dumps({"agent": "lane_farm", "pid": os.getpid(),
                              "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                              "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                              "status": "running", "lanes": summary}), encoding="utf-8")
    return summary

def main():
    ap = argparse.ArgumentParser(description="10-lane paper farm (paper-only)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=300.0)
    args = ap.parse_args()
    # single-instance: hold an EXCLUSIVE file lock for the whole process lifetime
    # (atomic — no pid probe races; Codex #5)
    lock_f = open(ROOT / "state" / "lane_farm.lock", "a+b")
    try:
        import msvcrt
        msvcrt.locking(lock_f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print(json.dumps({"exit": "another lane_farm holds the lock"})); return
    except ImportError:
        pass
    (ROOT / "state" / "lane_farm.pid").write_text(str(os.getpid()), encoding="utf-8")
    from tradingagents.binance.client import spot_client
    client = spot_client()
    while True:
        try:
            s = run_once(client)
            print(json.dumps({"lanes": {k: v.get("equity") for k, v in s.items()}}))
        except Exception as e:
            print(json.dumps({"error": repr(e)[:160]}))
        if args.once:
            break
        time.sleep(max(120.0, args.interval))

if __name__ == "__main__":
    main()
